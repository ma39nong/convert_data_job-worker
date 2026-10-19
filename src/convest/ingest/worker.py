"""Discover -> observe -> structurally validate -> freeze -> convert -> publish."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import json
import re
import shutil
import time
from convest.config import atomic_json,digest
from convest.jobs import run
from .inputs import snapshot,check_input,stage_input,monitor_input,InputChanged
from .publish import OWNER,read
from .batches import BatchPublisher as Publisher
from .queue import Queue

EPISODE=re.compile(r'episode[0-9]+\Z')


def own_directory(path,kind,fingerprint):
    path=Path(path)
    if path.is_symlink():raise ValueError('Refuse state/work symlink')
    path.mkdir(parents=True,exist_ok=True);marker=path/'owner.json'
    expected={'owner':OWNER,'kind':kind,'fingerprint':fingerprint}
    if marker.exists():
        if read(marker)!=expected:raise ValueError('State/work ownership or config changed')
    else:
        if any(path.iterdir()):raise ValueError('Refuse nonempty unowned state/work directory')
        atomic_json(marker,expected)


@contextmanager
def worker_context(config):
    own_directory(config['state_root'],'state',config['fingerprint'])
    own_directory(config['work_root'],'work',config['fingerprint'])
    with (Path(config['state_root'])/'worker.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        queue=Queue(Path(config['state_root'])/'queue.sqlite3')
        try:yield queue
        finally:queue.close()


def clean_slot(slot,work):
    if slot.is_symlink() or slot.parent.resolve()!=Path(work).resolve():raise ValueError('Unsafe work cleanup')
    if slot.exists():shutil.rmtree(slot)


def archive_reports(job,archive):
    if not job.exists():return
    archive.mkdir(parents=True,exist_ok=True)
    for src in job.rglob('*.json'):
        relative=src.relative_to(job)
        if 'cleaned' in relative.parts or 'staging' in relative.parts:continue
        dst=archive/relative;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dst)


def process(config,queue,route,bag,proof):
    started_ns=time.time_ns()
    key=digest([route['id'],bag.name]);slot=Path(config['work_root'])/key
    clean_slot(slot,config['work_root']);slot.mkdir()
    job=slot/'job';identity=proof['content_identity'];old=queue.get(route['id'],bag.name);attempt=old['attempts']+1
    queue.put(route['id'],bag.name,identity,'staging',attempts=attempt,proof=proof,error=None)
    archive=Path(config['state_root'])/'attempts'/key/f'{attempt:04d}'
    try:
        size=sum(f['size'] for f in proof['files']);reserve=route['conversion']['min_free_gb']*10**9
        if shutil.disk_usage(config['work_root']).free<size*4+reserve:raise OSError('Insufficient staging disk reserve')
        target_parent=Path(route['output']).parent
        while not target_parent.exists():target_parent=target_parent.parent
        if shutil.disk_usage(target_parent).free<size*2+reserve:raise OSError('Insufficient output disk reserve')
        with monitor_input(bag,proof['snapshot'],min(1,config['poll_seconds'])) as guard:
            staged=slot/'raw'/bag.name;stage_input(bag,staged,proof);guard()
            queue.put(route['id'],bag.name,identity,'processing',proof=proof,error=None)
            cfg={**route['conversion'],'source_root':str(staged.parent)}
            run(cfg,job,episodes=[bag.name]);guard()
            summary=read(job/'summary.json');outcome=summary['jobs'][0]
            if outcome['state']!='completed':
                if outcome['state']=='excluded':
                    queue.put(route['id'],bag.name,identity,'excluded',error=json.dumps(outcome.get('quality_rejection')))
                    return
                raise ValueError(outcome.get('error') or 'Conversion/source verification failed')
            # Full hashes plus structural checks again, not only a quiet-directory heuristic.
            after=check_input(bag,route,proof['snapshot']);guard()
            if after!={k:v for k,v in proof.items() if k!='stability'}:raise InputChanged('Input changed while processing')
            archive_reports(job,archive)
            shutil.copyfile(job/'cleaned'/bag.name/'trim_report.json',archive/'trim_report.json')
            generation=Publisher(route,config['fingerprint']).publish(job/'dataset',proof,bag.name,archive,precommit=guard,started_ns=started_ns)
            queue.put(route['id'],bag.name,identity,'published',error=None,proof={**proof,'generation':str(generation)})
    except InputChanged as exc:
        queue.put(route['id'],bag.name,None,'waiting_stable',attempts=0,proof=None,error=str(exc))
        queue.observe(route['id'],bag.name,None,time.time_ns(),config['poll_seconds']*10**9)
    except Exception as exc:
        delay=config['retry_seconds']*min(2**(attempt-1),60)
        queue.put(route['id'],bag.name,identity,'retry' if attempt<config['max_attempts'] else 'failed',
                  error=f'{type(exc).__name__}: {exc}',next_ns=time.time_ns()+delay*10**9)
    finally:
        try:archive_reports(job,archive)
        finally:clean_slot(slot,config['work_root'])


def scan(config,queue,now_ns=None):
    now=time.time_ns() if now_ns is None else now_ns
    counts={};route_errors={};candidates=[];batches={}
    for route in config['routes']:
        source=Path(route['source'])
        try:
            publisher=Publisher(route,config['fingerprint']);published=publisher.published(now_ns=now)
            batches[route['id']]=publisher.status()
        except Exception as exc:route_errors[route['id']]=str(exc);continue
        if not source.is_dir() or source.is_symlink():route_errors[route['id']]='Input directory missing or symlink';continue
        bags=sorted((p for p in source.iterdir() if EPISODE.fullmatch(p.name) and p.is_dir() and not p.is_symlink()),key=lambda p:(int(p.name[7:]),p.name))
        try:skipped=queue.initialize_history(route['id'],[p.name for p in bags],config['initial_history_by_route'][route['id']],now)
        except ValueError as exc:route_errors[route['id']]=str(exc);continue
        bags=[p for p in bags if p.name not in skipped]
        new=[];observed=[]
        for bag in bags:
            old=queue.get(route['id'],bag.name)
            if old is None:
                new.append(bag.name);queue.put(route['id'],bag.name,None,'waiting_stable')
            try:
                snap=snapshot(bag);signature=digest(snap)
                obs=queue.observe(route['id'],bag.name,signature,now,config['poll_seconds']*10**9)
                observed.append((bag,old,snap,signature,obs))
            except Exception as exc:
                queue.put(route['id'],bag.name,None,'waiting_upload',error=str(exc));continue
        for name in new:
            index=next(i for i,p in enumerate(bags) if p.name==name)
            if index>0:queue.prioritize(route['id'],bags[index-1].name,now)
        priority_names={bags[i-1].name for i,p in enumerate(bags) if i>0 and p.name in new}
        for bag,old,snap,signature,obs in observed:
            # Publication reconciliation is independent of a possibly interrupted queue commit.
            if bag.name in published:
                record=published[bag.name];saved=record['ingest'];same=snap==saved['snapshot']
                if same and old and old['state']=='published' and old['identity']==saved['content_identity']:
                    continue
                queue.put(route['id'],bag.name,saved['content_identity'],'published' if same else 'conflict',
                          proof=saved,error=None if same else 'Published input changed; existing dataset retained')
                clean_slot(Path(config['work_root'])/digest([route['id'],bag.name]),config['work_root']);continue
            if old and old['state']=='published':
                route_errors[route['id']]='Previously published source missing from output generation';continue
            stable=obs['stable_count']>=config['stable_scans'] and now-obs['stable_since_ns']>=config['stable_seconds']*10**9
            if not stable:
                queue.put(route['id'],bag.name,None,'waiting_stable',attempts=0,proof={'signature':signature},error=None);continue
            previous=json.loads(old['proof']) if old and old.get('proof') else {}
            unchanged=previous.get('signature')==signature or digest(previous.get('snapshot',[]))==signature
            if old and unchanged:
                if old['state'] in ('failed','excluded','conflict'):continue
                if old['state'] in ('retry','waiting_upload') and old['next_ns']>now:continue
            if old and old['state'] in ('processing','staging') and old['attempts']>=config['max_attempts']:
                queue.put(route['id'],bag.name,old['identity'],'failed',error='Retry limit reached after interruption');continue
            candidates.append((-max(obs['priority'],now if bag.name in priority_names else 0),old['updated_ns'] if old else 0,route,bag,snap,obs))
    # New successors prioritize predecessors, but do not bypass any readiness condition.
    candidates.sort(key=lambda x:(x[0],x[1],int(x[3].name[7:]),x[2]['id']))
    checks=0;processed=0
    for _,_,route,bag,snap,observation in candidates:
        if checks>=config['max_checks_per_scan'] or processed>=config['max_jobs_per_scan']:break
        checks+=1
        old=queue.get(route['id'],bag.name)
        queue.put(route['id'],bag.name,old['identity'],'checking',error=None)
        try:proof=check_input(bag,route,snap)
        except Exception as exc:
            queue.put(route['id'],bag.name,None,'waiting_upload',proof={'signature':digest(snap)},
                error=f'{type(exc).__name__}: {exc}',next_ns=now+config['retry_seconds']*10**9);continue
        proof['stability']={**observation,'required_scans':config['stable_scans'],'required_seconds':config['stable_seconds']}
        if old['identity']!=proof['content_identity']:queue.put(route['id'],bag.name,proof['content_identity'],'queued',attempts=0,proof=proof,error=None)
        process(config,queue,route,bag,proof);processed+=1
    for row in queue.rows():counts[row['state']]=counts.get(row['state'],0)+1
    for route in config['routes']:
        if route['id'] not in route_errors:batches[route['id']]=Publisher(route,config['fingerprint']).status()
    result={'updated_ns':time.time_ns(),'counts':counts,'route_errors':route_errors,'initial_history':queue.history_rows(),'batches':batches,
            'jobs':[{k:v for k,v in row.items() if k!='proof'} for row in queue.rows()]}
    atomic_json(Path(config['state_root'])/'status.json',result);return result
