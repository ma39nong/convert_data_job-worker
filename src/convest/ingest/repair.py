"""Selective capture-time backfill; preserve successful generations and maintenance."""
from pathlib import Path
import argparse
import json
import os
import signal
import sqlite3
import subprocess
import time

from convest.config import WORKSPACE,digest,atomic_json
from convest.pipeline import load_records
from .batches import BatchPublisher
from .control import maintenance_requested
from .inputs import snapshot,check_input
from .publish import read
from .worker import worker_context,process,scan


def eligible(row):
    if row['state']!='excluded' or row['route'] not in ('wuji-A','wuji-B'):
        return False
    try:
        error=json.loads(row.get('error') or '{}')
        if not isinstance(error,dict):return False
        details=error.get('details',{})
    except (ValueError,TypeError):return False
    if not isinstance(error,dict) or not isinstance(details,dict):return False
    return (details.get('policy')=='continuous_export_range' and details.get('export_range')=='full-all'
            and bool(details.get('gaps')) and 'internal invalid interval' in error.get('reason',''))


def published_ro(config):
    """No sync, sealing, video reads or catalogue writes for the dry-run inventory."""
    found={}
    for route in config['routes']:
        pub=BatchPublisher(route,config['fingerprint'])
        if not pub._exists():continue
        for batch in pub._catalog()['batches']:
            root=pub._publisher(batch).current()
            if root is None:continue
            for record in load_records(root):
                key=(route['id'],record['ingest']['episode'])
                if key in found:raise ValueError('Source present in multiple batches')
                found[key]=(record,root)
    return found


def legacy_snapshot(config):
    result={}
    for key,(record,root) in published_ro(config).items():
        files={}
        for rel,expected in record['ingest']['artifacts'].items():
            path=root/rel;st=path.stat()
            if path.is_symlink() or st.st_size!=expected['bytes'] or st.st_mtime_ns!=expected['mtime_ns'] or st.st_mode&0o222:
                raise ValueError('Existing immutable artifact changed')
            files[rel]=(st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,expected['sha256'])
        result[key]={'record_digest':digest(record),'artifacts':files}
    return result


def assert_preserved(config,before):
    after=legacy_snapshot(config)
    for key,value in before.items():
        if after.get(key)!=value:
            raise ValueError(f'Previously published source changed: {key}')
    return len(before)


def backfill(config,queue,report,should_stop=lambda:False):
    if not maintenance_requested(config):raise ValueError('Pause receiver before selective backfill')
    if any(r['conversion'].get('image_time_basis','capture')!='capture' for r in config['routes']):
        raise ValueError('Capture-time cleaning must be enabled')
    report=Path(report);report.mkdir(parents=True,exist_ok=False)
    with sqlite3.connect(report/'queue.before.sqlite3') as backup:queue.db.backup(backup)
    before=legacy_snapshot(config);routes={r['id']:r for r in config['routes']}
    selected=[r for r in queue.rows() if eligible(r)]
    atomic_json(report/'selected.json',selected)
    result={'status':'running','selected':len(selected),'results':[],
            'previously_published':len(before),'maintenance_preserved':True}
    atomic_json(report/'summary.json',result)
    for old in selected:
        if should_stop():break
        if not maintenance_requested(config):raise ValueError('Maintenance request changed; abort')
        route=routes[old['route']];bag=Path(route['source'])/old['episode']
        print(f"Backfill {old['route']}/{old['episode']}",flush=True)
        current=published_ro(config).get((old['route'],old['episode']))
        if current:
            record,root=current;saved=record['ingest']
            if saved['content_identity']!=old['identity'] or snapshot(bag)!=saved['snapshot']:
                raise ValueError('Committed backfill identity/source mismatch')
            previous=json.loads(old['proof']) if old.get('proof') else {}
            queue.put(old['route'],old['episode'],old['identity'],'published',error=None,
                      proof={**previous,'generation':str(root)})
            entry={'route':old['route'],'episode':old['episode'],'state':'published','reconciled':True}
        else:
            try:
                snap=snapshot(bag)
                previous=json.loads(old['proof']) if old.get('proof') else {}
                if previous.get('snapshot')!=snap:
                    raise ValueError('Original snapshot changed since exclusion; needs normal stability handling')
                def progress(phase,**detail):
                    atomic_json(report/'progress.json',{'route':old['route'],'episode':old['episode'],
                                'phase':phase,'updated_ns':time.time_ns(),**detail})
                proof=check_input(bag,route,snap,progress)
                if 'stability' in previous:proof['stability']=previous['stability']
                if proof['content_identity']!=old['identity']:
                    raise ValueError('Original content changed since exclusion; needs normal stability handling')
            except Exception as exc:
                entry={'route':old['route'],'episode':old['episode'],'state':'input_not_repaired',
                       'error':f'{type(exc).__name__}: {exc}'}
            else:
                if not maintenance_requested(config):raise ValueError('Maintenance changed after readiness')
                # Ordinary processing keeps all existing quality/source/publication guards.
                process(config,queue,route,bag,proof)
                new=queue.get(old['route'],old['episode'])
                entry={'route':old['route'],'episode':old['episode'],'state':new['state'],'error':new['error']}
        assert_preserved(config,before)
        result['results'].append(entry)
        atomic_json(report/'summary.json',result)
    scan(config,queue)  # Drain branch only: refresh counts, never discover or process other bags.
    result.update(status='cancelled' if should_stop() else 'completed',
                  published=sum(r['state']=='published' for r in result['results']),
                  still_excluded=sum(r['state']=='excluded' for r in result['results']),
                  old_sources_preserved=assert_preserved(config,before),
                  maintenance_preserved=maintenance_requested(config))
    result['all_repaired']=(len(result['results'])==len(selected) and
                            all(r['state']=='published' for r in result['results']))
    atomic_json(report/'summary.json',result)
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,required=True);p.add_argument('--apply',action='store_true')
    args=p.parse_args(argv)
    from .config import load_service_config
    cfg=load_service_config(args.config);state=Path(cfg['state_root'])
    with sqlite3.connect((state/'queue.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row;rows=[dict(r) for r in db.execute('SELECT * FROM jobs')]
    chosen=[{'route':r['route'],'episode':r['episode']} for r in rows if eligible(r)]
    print(json.dumps({'writes_performed':False,'selected':chosen,'count':len(chosen),
                     'maintenance_requested':maintenance_requested(cfg)},ensure_ascii=False),flush=True)
    if not args.apply:return 0
    if not maintenance_requested(cfg):raise ValueError('Existing maintenance pause required; do not resume')
    kind=subprocess.check_output(['findmnt','-n','-o','FSTYPE','-T',str(state)],text=True).strip()
    if kind=='fuse.sshfs':raise ValueError('Run on the native server, never over SSHFS')
    import fcntl
    coordinator=(WORKSPACE.parent/'operations/.nvme-trial.lock').open('a')
    fcntl.flock(coordinator,fcntl.LOCK_EX|fcntl.LOCK_NB)
    unit='auto-clean-convert.service'
    def service():
        text=subprocess.check_output(['systemctl','show',unit,'-p','MainPID','-p','ActiveState',
                                      '-p','Result','-p','WorkingDirectory'],text=True)
        return dict(line.split('=',1) for line in text.splitlines() if '=' in line)
    s=service()
    if Path(s.get('WorkingDirectory','/')).resolve()!=WORKSPACE.resolve():
        raise ValueError('Service project does not match this native checkout')
    subprocess.run(['sudo','-v'],check=True)
    pid=s['MainPID']
    if int(pid):
        subprocess.run(['sudo','systemctl','kill','--kill-who=main','--signal=SIGTERM',unit],check=True)
        print('Waiting for the receiver to finish safely; maintenance stays enabled.',flush=True)
        while service()['MainPID']==pid:time.sleep(2)
    stopped=service()
    if stopped['ActiveState']!='inactive' or stopped['Result']!='success':
        raise ValueError('Receiver did not stop normally; do not take over')
    cancelled=False
    def stop(signum,frame):
        nonlocal cancelled
        cancelled=True;print('Finish current bag, then stop backfill; maintenance remains enabled.',flush=True)
    signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
    report=WORKSPACE.parent/'operations/capture-time-backfill/reports'/str(time.time_ns())
    with worker_context(cfg) as queue:result=backfill(cfg,queue,report,lambda:cancelled)
    # Restore only the idle receiver; never clear the user's persistent pause.
    if not maintenance_requested(cfg):raise ValueError('Pause changed; inspect before restart')
    subprocess.run(['sudo','systemctl','start',unit],check=True)
    print(json.dumps(result,ensure_ascii=False,indent=2),flush=True);print('REPORT',report/'summary.json',flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
