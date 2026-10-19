"""Move currently queued robot inputs forward without interrupting active work."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from convest.config import atomic_json
from convest.ingest.config import load_service_config
from convest.ingest.publish import OWNER,read


def prioritize(config,robot,apply=False,*,wait_seconds=600,report=None):
    state=Path(config['state_root'])
    if state.is_symlink() or read(state/'owner.json')!={'owner':OWNER,'kind':'state','fingerprint':config['fingerprint']}:
        raise ValueError('Queue ownership/configuration mismatch')
    database=state/'queue.sqlite3'
    routes={r['id']:r for r in config['routes'] if r['robot']==robot}
    changed=[];now=time.time_ns();started=time.monotonic()
    mode='rw' if apply else 'ro'
    # Open the native server database, never an SSHFS view of a live WAL queue.
    with closing(sqlite3.connect(database.as_uri()+'?mode='+mode,uri=True,
                                timeout=min(.5,wait_seconds),isolation_level=None)) as db:
        rows=db.execute('''SELECT j.route,j.episode,j.state FROM jobs j
                           JOIN observations o USING(route,episode)''').fetchall()
        for route,episode,status in rows:
            if route not in routes or status not in ('waiting_stable','waiting_upload','retry','queued'):continue
            source=Path(routes[route]['source'])/episode
            if source.is_symlink() or not source.is_dir():continue
            changed.append({'route':route,'episode':episode,'state':status})
        if apply and changed:
            # Do all filesystem reads BEFORE acquiring the write lock. A single
            # autocommit statement owns the lock only for the actual update;
            # RETURNING is fully consumed so the statement commits immediately.
            values=','.join('(?,?)' for _ in changed)
            args=[v for r in changed for v in (r['route'],r['episode'])]
            sql=f'''UPDATE observations SET priority=max(priority,?)
                    WHERE (route,episode) IN (VALUES {values}) AND EXISTS(
                      SELECT 1 FROM jobs j WHERE j.route=observations.route
                      AND j.episode=observations.episode
                      AND j.state IN ('waiting_stable','waiting_upload','retry','queued'))
                    RETURNING route,episode'''
            deadline=started+wait_seconds;last_report=started-5
            while True:
                now=time.time_ns()
                try:
                    updated=set(db.execute(sql,[now,*args]).fetchall())
                    changed=[r for r in changed if (r['route'],r['episode']) in updated]
                    break
                except sqlite3.OperationalError as exc:
                    if getattr(exc,'sqlite_errorcode',0)&255 not in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED):raise
                    if db.in_transaction:db.rollback()
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise TimeoutError('Queue still busy; priority was not changed; worker left running') from exc
                    if report and time.monotonic()-last_report>=5:
                        report(f'Queue busy; waiting for a brief write slot ({time.monotonic()-started:.0f}s elapsed); worker left running.')
                        last_report=time.monotonic()
                    time.sleep(min(1,remaining))
    result={'robot':robot,'applied':apply,'requested_ns':now,'queued_count':len(changed),'queued':changed,
            'scope':'currently queued inputs only; new arrivals retain normal scheduling',
            'active_job_interrupted':False,'readiness_bypassed':False,
            'elapsed_seconds':round(time.monotonic()-started,3)}
    if apply:atomic_json(state/f'priority_request_{now}.json',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--robot',choices=['sharpa','wuji'],required=True)
    p.add_argument('--apply',action='store_true')
    args=p.parse_args();config=load_service_config(args.config)
    if args.apply:
        fs=subprocess.check_output(['findmnt','-n','-o','FSTYPE','-T',config['state_root']],text=True).strip()
        if fs=='fuse.sshfs':raise SystemExit('Run on the native server; do not write a live SQLite queue through SSHFS')
    print(json.dumps(prioritize(config,args.robot,args.apply,
        report=lambda message:print(message,file=sys.stderr,flush=True)),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
