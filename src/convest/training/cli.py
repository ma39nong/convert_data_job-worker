"""Local sealed-batch normal/speedup worker, independent from capture and training."""
import argparse
import json
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

from .config import load
from .worker import context, discover, process_once, upload_once


def status(cfg):
    path = Path(cfg['state_root']) / 'queue.sqlite3'
    if not path.exists():
        return {'state': 'not_started'}
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return {'tasks': [dict(r) for r in db.execute('SELECT id,state,attempts,error,updated_ns FROM tasks')]}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['check-config', 'status', 'once', 'process', 'upload-once', 'upload', 'run'])
    p.add_argument('--config', required=True, type=Path)
    args = p.parse_args(argv)
    cfg = load(args.config)
    if args.command == 'check-config':
        print(json.dumps({'status': 'valid', 'config': cfg}, ensure_ascii=False, indent=2))
        return 0
    if args.command == 'status':
        print(json.dumps(status(cfg), ensure_ascii=False, indent=2))
        return 0
    stopping = [False]
    def stop(*_):
        stopping[0] = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    uploading = args.command in ('upload', 'upload-once')
    child = None
    try:
        with context(cfg, 'upload' if uploading else 'processing') as db:
            if args.command == 'run':
                child = subprocess.Popen([sys.executable, '-u', '-m', 'convest.training.cli',
                                          'upload', '--config', str(args.config.resolve())])
            while not stopping[0]:
                if child and child.poll() is not None:
                    raise RuntimeError('Training uploader exited; local results remain queued')
                if uploading:
                    upload_once(cfg, db)
                else:
                    discover(cfg, db)
                    process_once(cfg, db)
                if args.command in ('once', 'upload-once'):
                    return 0
                until = time.monotonic() + cfg['poll_seconds']
                while not stopping[0] and time.monotonic() < until:
                    time.sleep(min(.25, max(0, until - time.monotonic())))
    finally:
        if child and child.poll() is None:
            child.send_signal(signal.SIGTERM)
            child.wait()  # Complete the current transfer, never force-kill it.
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        raise SystemExit(f'{type(exc).__name__}: {exc}')
