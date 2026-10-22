"""Standalone stdlib-only helper sent through SSH; no server installation required."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time


class Conflict(ValueError):
    pass


def run(request):
    root = Path(request['root']).resolve(strict=True)
    if not root.is_dir() or root.parts[-2:] != ('workspace', 'data'):
        raise ValueError('Training workspace/data root is unavailable')
    robot, group, name, task, fp = [request[k] for k in ('robot', 'group', 'name', 'task', 'fingerprint')]
    if (robot not in ('sharpa', 'wuji') or group not in ('A', 'B')
            or not re.fullmatch(r'[0-9]+_[0-9]+_data_lerobot--batch-[0-9]{6,}--[0-9a-f]{16}', name)
            or not re.fullmatch(r'[0-9a-f]{64}', task) or not re.fullmatch(r'[0-9a-f]{64}', fp)):
        raise ValueError('Invalid training identity')
    files = request['files']
    names = set()
    for row in files:
        p = Path(row['path'])
        if (p.is_absolute() or '..' in p.parts or not p.parts or row['path'] in names
                or not re.fullmatch(r'[0-9a-f]{64}', row['sha256'])
                or type(row['size']) is not int or row['size'] < 0):
            raise ValueError('Invalid transfer manifest')
        names.add(row['path'])
    def safe(path):
        p = Path(path)
        if not p.is_relative_to(root):
            raise ValueError('Transfer path escapes training root')
        for part in [p, *p.parents]:
            if part == root:
                break
            if part.is_symlink():
                raise Conflict('Symlink below training root')
        return p
    destination = safe(root / robot / group / name)
    staging = safe(destination.parent / '.uploading' / task)
    receipts = safe(root / '.training-transfer' / fp)
    locks = safe(root / '.training-transfer/.locks')
    locks.mkdir(parents=True, exist_ok=True)
    safe(locks / (task + '.lock'))
    def verify(folder):
        found = {}
        for p in folder.rglob('*'):
            safe(p)
            if p.is_file():
                found[p.relative_to(folder).as_posix()] = p.stat().st_size
            elif not p.is_dir():
                raise Conflict('Unexpected special transfer file')
        if found != {r['path']: r['size'] for r in files}:
            raise Conflict('Remote file inventory differs')
        for row in files:
            h = hashlib.sha256()
            with (folder / row['path']).open('rb') as f:
                for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
                    h.update(block)
            if h.hexdigest() != row['sha256']:
                raise Conflict('Remote SHA256 differs: ' + row['path'])
    with (locks / (task + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if destination.exists():
            verify(destination)
            if request['operation'] == 'prepare':
                return {'status': 'existing'}
        elif request['operation'] == 'prepare':
            staging.mkdir(parents=True, exist_ok=True)
            for p in staging.rglob('*'):
                safe(p)
                if p.is_file() and p.relative_to(staging).as_posix() not in names:
                    raise Conflict('Unexpected staging file')
            return {'status': 'staging', 'staging': str(staging)}
        elif request['operation'] == 'commit':
            verify(staging)
            # Flush uploaded data before making the pair visible to training.
            for row in files:
                with (staging / row['path']).open('rb') as f:
                    os.fsync(f.fileno())
            staging.rename(destination)
            fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            raise ValueError('Invalid remote operation')
        receipts.mkdir(parents=True, exist_ok=True)
        receipt = safe(receipts / (task + '.json'))
        content = {'status': 'completed', 'task': task, 'dataset': str(destination),
                   'normal': str(destination / 'normal'), 'speedup': str(destination / 'speedup'),
                   'files': files, 'completed_ns': time.time_ns()}
        tmp = safe(receipt.with_suffix('.tmp'))
        with tmp.open('w') as f:
            json.dump(content, f)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(receipt)
        if json.loads(receipt.read_text()) != content:
            raise OSError('Receipt read-back failed')
        fd = os.open(receipts, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return {'status': 'completed', 'destination': str(destination), 'receipt': str(receipt)}


if __name__ == '__main__':
    try:
        response = run(json.load(sys.stdin))
    except Exception as exc:
        response = {'status': 'conflict' if isinstance(exc, Conflict) else 'error',
                    'error': f'{type(exc).__name__}: {exc}'}
    print(json.dumps(response))
