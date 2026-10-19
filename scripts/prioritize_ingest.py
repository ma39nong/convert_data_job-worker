"""Move currently queued robot inputs forward without interrupting active work."""
import argparse
import json
from pathlib import Path
import sqlite3
import subprocess
import time

from convest.config import atomic_json
from convest.ingest.config import load_service_config
from convest.ingest.publish import OWNER,read


def prioritize(config,robot,apply=False):
    state=Path(config['state_root'])
    if state.is_symlink() or read(state/'owner.json')!={'owner':OWNER,'kind':'state','fingerprint':config['fingerprint']}:
        raise ValueError('Queue ownership/configuration mismatch')
    database=state/'queue.sqlite3'
    routes={r['id']:r for r in config['routes'] if r['robot']==robot}
    changed=[];now=time.time_ns()
    mode='rw' if apply else 'ro'
    # Open the native server database, never an SSHFS view of a live WAL queue.
    with sqlite3.connect(database.as_uri()+'?mode='+mode,uri=True,timeout=30) as db:
        if apply:db.execute('BEGIN IMMEDIATE')
        for route,episode,status in db.execute('SELECT route,episode,state FROM jobs').fetchall():
            if route not in routes or status not in ('waiting_stable','waiting_upload','retry','queued'):continue
            source=Path(routes[route]['source'])/episode
            if source.is_symlink() or not source.is_dir():continue
            if not db.execute('SELECT 1 FROM observations WHERE route=? AND episode=?',(route,episode)).fetchone():continue
            changed.append({'route':route,'episode':episode,'state':status})
            if apply:
                db.execute('UPDATE observations SET priority=max(priority,?) WHERE route=? AND episode=?',(now,route,episode))
    result={'robot':robot,'applied':apply,'requested_ns':now,'queued_count':len(changed),'queued':changed,
            'scope':'currently queued inputs only; new arrivals retain normal scheduling',
            'active_job_interrupted':False,'readiness_bypassed':False}
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
    print(json.dumps(prioritize(config,args.robot,args.apply),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
