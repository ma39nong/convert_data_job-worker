"""Standard-library SSH endpoint. Requests arrive as JSON on stdin, never shell interpolation."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time


def absolute(value):
    p = Path(value)
    if not p.is_absolute() or '..' in p.parts or str(p) == '/' or any(c in str(p) for c in '\n\r\x00'):
        raise ValueError('请选择绝对目录，不能使用根目录或 ..')
    return p


def safe(path):
    p = absolute(str(path))
    for q in (p, *p.parents):
        if q.is_symlink():
            raise ValueError('写入路径不能包含软链接：' + str(q))
    return p


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = safe(path.with_name(path.name + '.tmp'))
    with tmp.open('w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush(); os.fsync(f.fileno())
    tmp.replace(path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def run(q):
    op = q['operation']
    if op == 'check':
        import shutil
        return {'status': 'ok', 'home': str(Path.home()), 'python': sys.executable,
                'rsync': shutil.which('rsync')}
    if op == 'list':
        p = Path(q.get('path') or str(Path.home()))
        if not p.is_absolute() or '..' in p.parts: raise ValueError('目录必须是绝对路径')
        p = p.resolve(strict=True)
        entries = []
        for child in p.iterdir():
            try:
                if child.is_dir():
                    entries.append({'name': child.name, 'path': str(child), 'symlink': child.is_symlink()})
            except OSError: pass
        return {'path': str(p), 'parent': str(p.parent), 'entries': sorted(entries, key=lambda x: x['name'])[:1000]}
    if op == 'mkdir':
        parent = safe(q['parent']); name = q['name'].strip()
        if not name or name in ('.', '..') or '/' in name or '\\' in name or any(c in name for c in '\n\r\x00') or len(name) > 100:
            raise ValueError('请输入有效的单层目录名称')
        p = safe(parent / name); p.mkdir(exist_ok=False)
        if q.get('with_ab', True):
            (p / 'A').mkdir(); (p / 'B').mkdir()
        return {'path': str(p), 'created': True}
    destination = safe(q['destination']); token = q['token']
    if not re.fullmatch('[a-f0-9]{64}', token): raise ValueError('Invalid transfer identity')
    files = q['files']; names = set()
    for r in files:
        rel = Path(r['path'])
        if rel.is_absolute() or '..' in rel.parts or not rel.parts or r['path'] in names:
            raise ValueError('Invalid transfer file path')
        if not re.fullmatch('[a-f0-9]{64}', r['sha256']) or type(r['size']) is not int or r['size'] < 0:
            raise ValueError('Invalid transfer manifest')
        names.add(r['path'])
    def verify(root):
        found = {}
        for p in root.rglob('*'):
            safe(p)
            if p.is_file(): found[p.relative_to(root).as_posix()] = p.stat().st_size
            elif not p.is_dir(): raise ValueError('Unexpected remote special file')
        if found != {r['path']: r['size'] for r in files}:
            raise ValueError('远端文件清单冲突，保留已有数据')
        for r in files:
            if sha(root / r['path']) != r['sha256']:
                raise ValueError('远端 SHA256 不一致：' + r['path'])
    journal = safe(destination.parent / '.workflow-transfer')
    journal.mkdir(parents=True, exist_ok=True)
    receipt = safe(journal / (token + '.json'))
    stage = safe(destination.parent / '.uploading' / token)
    with safe(journal / (token + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if op == 'prepare':
            if destination.exists():
                verify(destination)
                return {'status': 'existing', 'destination': str(destination)}
            stage.mkdir(parents=True, exist_ok=True)
            for p in stage.rglob('*'):
                safe(p)
                if p.is_file() and p.relative_to(stage).as_posix() not in names:
                    raise ValueError('暂存目录存在未知文件')
            return {'status': 'staging', 'staging': str(stage)}
        if op not in ('commit', 'verify'): raise ValueError('Unknown operation')
        if not destination.exists():
            if op == 'verify': raise ValueError('远端备份不存在')
            verify(stage)
            for r in files:
                with (stage / r['path']).open('rb') as f: os.fsync(f.fileno())
            stage.rename(destination)
            fd = os.open(destination.parent, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        else: verify(destination)
        content = {'status': 'completed', 'destination': str(destination), 'token': token,
                   'files': files, 'completed_ns': time.time_ns()}
        if receipt.exists():
            old = json.loads(receipt.read_text())
            if any(old.get(k) != content[k] for k in ('status', 'destination', 'token', 'files')):
                raise ValueError('远端凭据与本次数据不符')
            content = old
        else: atomic(receipt, content)
        assert json.loads(receipt.read_text()) == content
        try: stage.parent.rmdir()
        except OSError: pass
        return {'status': 'completed', 'destination': str(destination), 'receipt': str(receipt),
                'completed_ns': content['completed_ns'], 'files': len(files)}


if __name__ == '__main__':
    try: print(json.dumps(run(json.load(sys.stdin)), ensure_ascii=False))
    except Exception as e:
        print(json.dumps({'status': 'error', 'error': str(e)}, ensure_ascii=False))
        sys.exit(2)
