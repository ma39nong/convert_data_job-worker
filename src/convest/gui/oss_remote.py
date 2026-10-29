"""OSS transit endpoint, appended to remote_agent.py before SSH execution.

Locks, intents and receipts live on CPFS, never on the object-storage mount.
Only files in an owned manifest may be removed, after final publication verifies.
"""
import shutil
import fcntl
import hashlib
import json
from pathlib import Path
import sys
import time

# Imported for local protocol tests; SSH execution already has these definitions.
if __package__:
    from .remote_agent import safe, sha, atomic, run as publish
else:
    publish = run


def mount_type(root):
    matches = []
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        mount = Path(fields[4].replace('\\040', ' '))
        if root.is_relative_to(mount):
            matches.append((len(mount.parts), fields[fields.index('-') + 1]))
    return max(matches)[1] if matches else None


def require_oss_mount(root):
    if mount_type(root) != 'fuse.ossfs2':
        raise ValueError('OSS 中转挂载未就绪；保留本机数据，请检查 /mnt/public_data')


def require_final_mount(root):
    if mount_type(root) not in ('nfs', 'nfs4'):
        raise ValueError('训练 CPFS 挂载未就绪；保留中转和本机数据')


def tree(root):
    found = {}
    if not root.exists(): return found
    safe(root)
    if not root.is_dir(): raise ValueError('中转路径不是目录')
    for p in root.rglob('*'):
        safe(p)
        if p.is_file(): found[p.relative_to(root).as_posix()] = p
        elif not p.is_dir(): raise ValueError('中转目录含特殊文件，保留数据')
    return found


def check(root, files, *, partial=False, hashes=True):
    expected = {r['path']: r for r in files}
    found = tree(root)
    directories = {str(p) for name in expected for p in Path(name).parents if str(p) != '.'}
    if root.exists():
        for p in root.rglob('*'):
            if p.is_dir() and p.relative_to(root).as_posix() not in directories:
                raise ValueError('中转含未知子目录，保留数据')
    if (not partial and set(found) != set(expected)) or set(found) - set(expected):
        raise ValueError('中转文件清单冲突，保留数据')
    if hashes:
        for name, p in found.items():
            if p.stat().st_size != expected[name]['size'] or sha(p) != expected[name]['sha256']:
                raise ValueError('中转 SHA256 不一致，保留数据：' + name)
    return found


def run(q):
    destination = safe(q['destination'])
    final_root = safe(q['final_root'])
    transit_root = safe(q['transit_root'])
    relative = destination.relative_to(final_root)
    # At least project / variant / scene / group / batch (also supports pair reports).
    if (len(relative.parts) < 5 or relative.parts[1] not in ('normal', 'speedup')
            or relative.parts[3] not in ('A', 'B')):
        raise ValueError('训练目标必须在 datasets 的 项目/版本/场景/A或B/批次 下')
    if final_root.is_relative_to(transit_root) or transit_root.is_relative_to(final_root):
        raise ValueError('OSS 中转和最终目录不能重叠')
    transit = safe(transit_root / relative)
    require_oss_mount(transit_root)
    require_final_mount(final_root)
    # Validate manifest/token and final conflict via the existing publication protocol.
    final_q = {k: q[k] for k in ('destination', 'token', 'files')}
    prepared = publish({**final_q, 'operation': 'prepare'})
    journal = safe(destination.parent / '.workflow-transfer')
    intent_path = safe(journal / (q['token'] + '.oss.json'))
    expected = {'protocol': 'gui-oss-transit-v1', **final_q, 'transit': str(transit)}
    owner_key = hashlib.sha256(str(destination).encode()).hexdigest()
    owner_path = safe(journal / (owner_key + '.oss-owner.json'))
    with safe(journal / (owner_key + '.oss.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owner = json.loads(owner_path.read_text()) if owner_path.exists() else None
        if owner and owner.get('token') != q['token']:
            raise ValueError('中转目录属于另一传输，拒绝覆盖或清理')
        intent = json.loads(intent_path.read_text()) if intent_path.exists() else None
        if intent and any(intent.get(k) != v for k, v in expected.items()):
            raise ValueError('中转凭据与当前目标不符，保留数据')
        if not intent:
            if transit.exists():
                raise ValueError('中转目录已有非本次数据，拒绝覆盖或清理：' + str(transit))
            intent = {**expected, 'state': 'cleaned' if prepared['status'] == 'existing' else 'uploading',
                      'created_ns': time.time_ns()}
            atomic(owner_path, {'token': q['token'], 'destination': str(destination)})
            atomic(intent_path, intent)
        if q['operation'] not in ('prepare', 'commit'):
            raise ValueError('Unknown OSS operation')
        if prepared['status'] != 'existing':
            if intent['state'] != 'uploading':
                raise ValueError('最终目录丢失但中转已进入清理，保留数据并报错')
            if q['operation'] == 'prepare':
                transit.mkdir(parents=True, exist_ok=True)
                check(transit, q['files'], partial=True, hashes=False)
                pending = [r['path'] for r in q['files']
                           if not (transit / r['path']).is_file()
                           or (transit / r['path']).stat().st_size != r['size']
                           or sha(transit / r['path']) != r['sha256']]
                return {'status': 'staging', 'staging': str(transit), 'pending': pending}
            # Closing the upload stream and reopening every mounted object precedes CPFS copy.
            check(transit, q['files'])
            stage = safe(prepared['staging'])
            for r in q['files']:
                source = safe(transit / r['path']); target = safe(stage / r['path'])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            # CPFS publication rechecks exact manifest, SHA, fsyncs and renames.
        result = publish({**final_q, 'operation': 'commit'})
        # Durable final receipt exists and has been read back at this point.
        if intent['state'] == 'cleaned':
            if transit.exists(): raise ValueError('已清理中转目录再次出现，拒绝删除')
            return {**result, 'transit': str(transit), 'transit_cleaned': True}
        # Full check before first delete; subset/hash check allows interrupted cleanup.
        check(transit, q['files'], partial=intent['state'] == 'cleaning')
        intent.update(state='cleaning', final_receipt=result['receipt'])
        atomic(intent_path, intent)
        for p in check(transit, q['files'], partial=True).values():
            p.unlink()
        if transit.exists():
            for p in sorted(transit.rglob('*'), key=lambda x: len(x.parts), reverse=True):
                safe(p)
                if p.is_dir(): p.rmdir()
                else: raise ValueError('中转清理期间出现未知文件，保留数据')
            transit.rmdir()
        if transit.exists(): raise OSError('中转删除尚未可见，稍后重试')
        intent.update(state='cleaned', cleaned_ns=time.time_ns())
        atomic(intent_path, intent)
        return {**result, 'transit': str(transit), 'transit_cleaned': True}


def receive(q, stream):
    prepared = run({**q, 'operation': 'prepare'})
    if prepared['status'] != 'staging':
        raise ValueError('最终目录已完成，请重试以核实清理状态')
    transit = safe(prepared['staging'])
    destination = safe(q['destination'])
    journal = safe(destination.parent / '.workflow-transfer')
    owner_key = hashlib.sha256(str(destination).encode()).hexdigest()
    expected = {r['path']: r for r in q['files']}
    names = q['paths']
    if not isinstance(names, list) or len(set(names)) != len(names) or any(n not in expected for n in names):
        raise ValueError('无效的中转文件清单')
    with safe(journal / (owner_key + '.oss.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        intent = json.loads((journal / (q['token'] + '.oss.json')).read_text())
        if intent['state'] != 'uploading': raise ValueError('中转已进入清理，不允许写入')
        check(transit, q['files'], partial=True, hashes=False)
        for name in names:
            path = safe(transit / name)
            # OSS supports sequential creation, but not ftruncate/random updates.
            # Only replace our manifest-owned incomplete object; completed ones skip on retry.
            if path.exists(): path.unlink()
            path.parent.mkdir(parents=True, exist_ok=True)
            remaining = expected[name]['size']; checksum = hashlib.sha256()
            with path.open('wb') as output:
                while remaining:
                    data = stream.read(min(8 * 1024 * 1024, remaining))
                    if not data: raise OSError('上传流中断，保留已写入对象供重试')
                    output.write(data); checksum.update(data); remaining -= len(data)
            if checksum.hexdigest() != expected[name]['sha256']:
                raise ValueError('上传流 SHA256 不一致，保留中转')
        if stream.read(1): raise ValueError('上传流包含多余数据')
    return {'status': 'staged', 'files_received': len(names)}


if __name__ == '__main__':
    try:
        request = json.loads(sys.stdin.buffer.readline())
        result = receive(request, sys.stdin.buffer) if request['operation'] == 'receive' else run(request)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({'status': 'error', 'error': str(e)}, ensure_ascii=False))
        sys.exit(2)
