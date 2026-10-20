"""Capture-side command line; no production server service calls."""
import argparse
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
from .config import load
from .worker import context, scan
from .transfer import upload_once


def status(cfg):
    dbpath=Path(cfg['state_root'])/'queue.sqlite3'
    result={'state':'not_started'}
    if dbpath.exists():
        with sqlite3.connect(dbpath.as_uri()+'?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row
            result={'jobs':[dict(row) for row in db.execute('SELECT route,episode,state,attempts,error FROM jobs ORDER BY route,episode')],
                    'uploads':[dict(row) for row in db.execute('SELECT route,episode,state,attempts,raw_done,dataset_done,error FROM uploads ORDER BY route,episode')]}
        if cfg.get('delivery_mode')=='daily':
            p=Path(cfg['state_root'])/'recordings.json'
            if p.exists():result['ledger']=json.loads(p.read_text())
        for name in ['progress','upload_progress','status']:
            p=Path(cfg['state_root'])/(name+'.json')
            if p.exists():result[name]=json.loads(p.read_text())
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['check-config','once','run','upload-once','upload','status'])
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--until-idle',action='store_true',help='Trial only: exit after observed jobs are converted/excluded and uploads finish; nonzero on local errors')
    args=p.parse_args(argv)
    cfg=load(args.config)
    if args.command=='check-config':
        print(json.dumps({'status':'valid','writes_performed':False,'config':cfg},ensure_ascii=False,indent=2));return 0
    if args.command=='status':
        print(json.dumps(status(cfg),ensure_ascii=False,indent=2));return 0
    stopping=[False]
    def stop(*_):stopping[0]=True
    signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
    uploading=args.command in ('upload','upload-once')
    child=None
    try:
        with context(cfg,'upload' if uploading else 'processing') as queue:
            if args.command=='run':
                child=subprocess.Popen([sys.executable,'-u','-m','convest.capture.cli','upload','--config',str(args.config.resolve())],start_new_session=True)
            while not stopping[0]:
                if child and child.poll() is not None:
                    raise RuntimeError('Uploader exited unexpectedly; local converted jobs remain queued')
                if uploading:upload_once(cfg,queue)
                else:
                    print(json.dumps(scan(cfg,queue,should_stop=lambda:stopping[0]),ensure_ascii=False),flush=True)
                    if args.until_idle:
                        rows=queue.rows()
                        if rows and all(r['state'] in ('converted','excluded','failed','conflict') for r in rows):
                            if any(r['state'] in ('failed','conflict') for r in rows):return 2
                            if queue.db.execute("SELECT count(*) FROM uploads WHERE state='conflict'").fetchone()[0]:return 2
                            pending=queue.db.execute("SELECT count(*) FROM uploads WHERE state!='completed'").fetchone()[0]
                            if not pending:return 0
                if args.command in ('once','upload-once'):return 0
                until=time.monotonic()+cfg['poll_seconds']
                while not stopping[0] and time.monotonic()<until:time.sleep(min(.25,max(0,until-time.monotonic())))
    finally:
        if child and child.poll() is None:
            child.send_signal(signal.SIGTERM)
            child.wait()  # Finish the current transfer before ending the trial.
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}',file=sys.stderr)
        raise SystemExit(2)
