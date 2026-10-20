"""Resumable file transfer, independent read-back hashes, atomic directory commit."""
from pathlib import Path
import json
import os
import time
from convest.config import atomic_json, digest
from convest.jobs import checksum
from convest.ingest.inputs import snapshot, require_same, InputChanged


class TransferConflict(ValueError):
    """Published destination differs; never overwrite or retry blindly."""


def manifest(root):
    root = Path(root)
    snap = snapshot(root)
    files = [{'path': row['path'], 'size': row['size'], 'sha256': checksum(root / row['path'])}
             for row in snap if not row['directory']]
    require_same(root, snap)
    return files


def verify_tree(root, files, progress=lambda **kw: None):
    root = Path(root)
    snap = snapshot(root)
    found = {row['path']: row['size'] for row in snap if not row['directory']}
    expected = {row['path']: row['size'] for row in files}
    if found != expected:
        raise ValueError('Transfer file inventory differs: ' + str(root))
    total = sum(row['size'] for row in files)
    done = 0
    for row in files:
        def report_hash(phase, **detail):
            progress(operation='hashing', file=row['path'], bytes_done=done+detail['bytes_done'], bytes_total=total)
        report_hash('hashing', bytes_done=0)
        if checksum(root / row['path'], report_hash) != row['sha256']:
            raise ValueError('Transfer read-back hash differs: ' + str(root / row['path']))
        done += row['size']
    require_same(root, snap)


def ensure_real_path(path):
    for p in [Path(path), *Path(path).parents]:
        if p.is_symlink():
            raise ValueError('Transfer path contains symlink: ' + str(p))


def check_mount(cfg):
    mount = cfg.get('require_mount')
    if mount and not os.path.ismount(mount):
        raise OSError('Required server mount is absent; refusing local fallback: ' + mount)
    for key in ('remote_raw_root', 'remote_dataset_root'):
        root = Path(cfg[key])
        ensure_real_path(root)
        if not root.is_dir():
            raise OSError('Remote root unavailable: ' + str(root))


def copy_tree(source, destination, files, token, guard=lambda: None, progress=lambda **kw: None):
    source, destination = Path(source), Path(destination)
    # Manifest paths must never escape either root, even if a journal is damaged.
    names = set()
    for row in files:
        path = Path(row['path'])
        if path.is_absolute() or '..' in path.parts or not path.parts or row['path'] in names:
            raise ValueError('Unsafe or duplicate transfer manifest path')
        names.add(row['path'])
    ensure_real_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        try:
            verify_tree(destination, files, progress)
        except (ValueError, InputChanged) as exc:
            raise TransferConflict(str(exc)) from exc
        guard()
        return  # Recover an interrupted local commit without overwriting.
    staging = destination.parent / '.uploading' / token
    ensure_real_path(staging)
    staging.mkdir(parents=True, exist_ok=True)
    expected_dirs = {str(p) for name in names for p in Path(name).parents if str(p) != '.'}
    for p in staging.rglob('*'):
        rel = p.relative_to(staging).as_posix()
        if p.is_symlink() or (not p.is_file() and not p.is_dir()):
            raise ValueError('Unsafe staging entry')
        if p.is_file() and rel not in names:
            raise ValueError('Unexpected staging file; preserved: ' + str(p))
        if p.is_dir() and rel not in expected_dirs:
            raise ValueError('Unexpected staging directory; preserved: ' + str(p))
    total = sum(row['size'] for row in files)
    done, last = 0, 0.0
    for row in files:
        guard()
        src, dst = source / row['path'], staging / row['path']
        ensure_real_path(src)
        ensure_real_path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        # Finished files survive retries. For a partial file validate its prefix
        # against the source, then continue from that byte offset.
        offset = 0
        if dst.exists():
            if dst.stat().st_size <= row['size']:
                with src.open('rb') as original, dst.open('rb') as partial:
                    while True:
                        block = partial.read(8 * 1024 * 1024)
                        if not block:
                            break
                        if block != original.read(len(block)):
                            offset = -1
                            break
                        offset += len(block)
            else:
                offset = -1
        if offset < 0:
            dst.unlink()
            offset = 0
        with src.open('rb') as reader, dst.open('ab' if offset else 'wb') as writer:
            reader.seek(offset)
            copied = offset
            while True:
                block = reader.read(8 * 1024 * 1024)
                if not block:
                    break
                writer.write(block)
                copied += len(block)
                if time.monotonic() - last > 5:
                    progress(file=row['path'], bytes_done=done+copied, bytes_total=total)
                    last = time.monotonic()
            progress(operation='flushing', file=row['path'], bytes_done=done+copied, bytes_total=total)
            writer.flush()
            os.fsync(writer.fileno())
        if dst.stat().st_size != row['size']:
            raise ValueError('Transferred file checksum mismatch: ' + str(dst))
        done += row['size']
        progress(file=row['path'], bytes_done=done, bytes_total=total)
    verify_tree(staging, files, progress)
    guard()
    if destination.exists():
        raise FileExistsError('Destination appeared during transfer; staging preserved')
    staging.rename(destination)
    # A receipt is written only after both raw and dataset commits. A consumer
    # requiring the pair must use the receipt, not directory existence alone.


def init_uploads(queue):
    queue.db.execute('''CREATE TABLE IF NOT EXISTS uploads (
        id TEXT PRIMARY KEY,route TEXT,episode TEXT,payload TEXT,state TEXT,
        attempts INTEGER DEFAULT 0,next_ns INTEGER DEFAULT 0,error TEXT,
        raw_done INTEGER DEFAULT 0,dataset_done INTEGER DEFAULT 0,updated_ns INTEGER)''')
    queue.db.commit()


def enqueue(queue, payload):
    identity = digest([payload['route'], payload['episode'], payload['identity']])
    with queue.db:
        queue.db.execute('INSERT OR IGNORE INTO uploads(id,route,episode,payload,state,updated_ns) VALUES(?,?,?,?,?,?)',
                         (identity, payload['route'], payload['episode'], json.dumps(payload), 'pending', time.time_ns()))
    return identity


def upload_once(cfg, queue):
    if cfg.get('delivery_mode')=='daily':
        from .daily import upload_once as daily_upload
        return daily_upload(cfg,queue)
    init_uploads(queue)
    queue.db.row_factory = __import__('sqlite3').Row
    rows = queue.db.execute("SELECT * FROM uploads WHERE state IN ('pending','retry','uploading') AND next_ns <= ? ORDER BY updated_ns,id", (time.time_ns(),)).fetchall()
    if not rows:
        return False
    row = dict(rows[0]); payload = json.loads(row['payload'])
    def update(**values):
        values['updated_ns'] = time.time_ns()
        with queue.db:
            queue.db.execute('UPDATE uploads SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=?', [*values.values(), row['id']])
    def progress(**detail):
        atomic_json(Path(cfg['state_root'])/'upload_progress.json', {'route': row['route'], 'episode':row['episode'], 'updated_ns':time.time_ns(), **detail})
    try:
        check_mount(cfg)
        raw = Path(payload['source'])
        require_same(raw, payload['source_snapshot'])
        target_raw = Path(cfg['remote_raw_root']) / payload['robot'] / payload['group'] / payload['episode']
        target_data = Path(cfg['remote_dataset_root']) / payload['robot'] / payload['group'] / (payload['episode']+'_data_lerobot')
        def guard():
            check_mount(cfg)
            require_same(raw, payload['source_snapshot'])
        progress(phase='uploading_raw')
        update(state='uploading', attempts=row['attempts']+1, error=None)
        copy_tree(raw, target_raw, payload['raw_files'], digest([cfg['fingerprint'], row['id']])+'-raw', guard,
                  lambda **kw: progress(phase='uploading_raw', **kw))
        update(raw_done=1)
        progress(phase='uploading_lerobot')
        copy_tree(payload['dataset'], target_data, payload['dataset_files'], digest([cfg['fingerprint'], row['id']])+'-dataset', guard,
                  lambda **kw: progress(phase='uploading_lerobot', **kw))
        receipt = Path(cfg['remote_dataset_root'])/'.capture-transfer'/cfg['fingerprint']/f"{row['id']}.json"
        ensure_real_path(receipt)
        atomic_json(receipt, {'status':'completed','identity':payload['identity'], 'route':row['route'], 'episode':row['episode'],
                             'raw':str(target_raw),'dataset':str(target_data),'raw_files':payload['raw_files'],
                             'dataset_files':payload['dataset_files'],'completed_ns':time.time_ns()})
        update(state='completed', dataset_done=1, next_ns=0, error=None)
        progress(phase='completed', receipt=str(receipt))
        print(json.dumps({'upload':'completed','route':row['route'],'episode':row['episode'],'receipt':str(receipt)}),flush=True)
    except Exception as exc:
        delay = cfg['retry_seconds'] * min(2 ** min(row['attempts'], 6), 60)
        outcome = 'conflict' if isinstance(exc,(TransferConflict,InputChanged)) else 'retry'
        update(state=outcome, attempts=row['attempts']+1, next_ns=time.time_ns()+delay*10**9, error=f'{type(exc).__name__}: {exc}')
        progress(phase=outcome,error=str(exc))
        print(json.dumps({'upload':outcome,'route':row['route'],'episode':row['episode'],'error':str(exc)}),flush=True)
    return True
