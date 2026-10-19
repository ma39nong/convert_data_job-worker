"""Local preparation commands. No uploads, network transport or service install."""
import argparse
import json
from pathlib import Path
import signal
import sqlite3
import sys
import time
from .config import load_service_config
from .worker import worker_context,scan
from .batches import BatchPublisher


def main(argv=None):
    p=argparse.ArgumentParser(description='Automatically observe, validate, clean and append incoming ROS bags')
    sub=p.add_subparsers(dest='command',required=True)
    for cmd in ('check-config','once','run','status'):
        c=sub.add_parser(cmd);c.add_argument('--config',type=Path,required=True)
    args=p.parse_args(argv)
    try:
        config=load_service_config(args.config)
        if args.command=='check-config':
            print(json.dumps({'status':'valid','writes_performed':False,'initial_history':config['initial_history_by_route'],
                              'routes':[{k:v for k,v in r.items() if k!='conversion'} for r in config['routes']]},ensure_ascii=False,indent=2));return 0
        if args.command=='status':
            database=Path(config['state_root'])/'queue.sqlite3'
            if not database.exists():print(json.dumps({'state':'not_started'}));return 0
            with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
                db.row_factory=sqlite3.Row
                rows=[dict(row) for row in db.execute('SELECT route,episode,state,attempts,error,updated_ns FROM jobs ORDER BY route,episode')]
                exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='initial_history'").fetchone()
                history=[{**dict(row),'policy':json.loads(row['policy']),'skipped':json.loads(row['skipped'])}
                         for row in db.execute('SELECT * FROM initial_history ORDER BY route')] if exists else []
            batches={r['id']:BatchPublisher(r,config['fingerprint']).status() for r in config['routes']}
            print(json.dumps({'jobs':rows,'initial_history':history,'batches':batches},ensure_ascii=False,indent=2));return 0
        stopping=False
        def stop(signum,frame):
            nonlocal stopping
            stopping=True
        if args.command=='run':signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
        with worker_context(config) as queue:
            while True:
                print(json.dumps(scan(config,queue),ensure_ascii=False),flush=True)
                if args.command=='once' or stopping:break
                until=time.monotonic()+config['poll_seconds']
                while not stopping and time.monotonic()<until:time.sleep(min(.5,max(0,until-time.monotonic())))
        return 0
    except Exception as exc:print(f'{type(exc).__name__}: {exc}',file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
