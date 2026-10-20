"""Local bag discovery/quality processing; uploading never owns this worker lock."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import json
import shutil
import time
from convest.config import atomic_json, digest
from convest.jobs import run, complete_source_verification
from convest.ingest.worker import own_directory, EPISODE
from convest.ingest.queue import Queue
from convest.ingest.inputs import snapshot, check_input, direct_input, monitor_input, verify_input_content, InputChanged
from .transfer import enqueue, init_uploads, manifest


@contextmanager
def context(cfg, kind='processing'):
    own_directory(cfg['state_root'], 'capture-state', cfg['fingerprint'])
    own_directory(cfg['work_root'], 'capture-work', cfg['fingerprint'])
    with (Path(cfg['state_root']) / (kind+'.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        queue = Queue(Path(cfg['state_root'])/'queue.sqlite3')
        init_uploads(queue)
        try:
            yield queue
        finally:
            queue.close()


def reporter(cfg, route, episode):
    def report(phase, **detail):
        atomic_json(Path(cfg['state_root'])/'progress.json', {'route':route['id'],'episode':episode,
                    'phase':phase,'updated_ns':time.time_ns(),**detail})
        print(json.dumps({'route':route['id'],'episode':episode,'phase':phase,**detail},ensure_ascii=False),flush=True)
    return report


def payload_from(cfg, route, bag, proof, job):
    return {'route':route['id'],'robot':route['robot'],'group':route['group'],'episode':bag.name,
            'identity':proof['content_identity'],'source':str(bag),'source_snapshot':proof['snapshot'],
            'raw_files':proof['files'],'dataset':str(job/'dataset'),'dataset_files':manifest(job/'dataset')}


def process(cfg, queue, route, bag, proof):
    old = queue.get(route['id'], bag.name)
    attempt = old['attempts']+1
    # Retry interrupted/failed processing in a new attempt. Successful completed
    # output has its own durable local manifest and is reconciled before any run.
    root = Path(cfg['work_root']) / route['id'] / bag.name / proof['content_identity']
    root.mkdir(parents=True,exist_ok=True)
    complete = root/'local_complete.json'
    progress = reporter(cfg,route,bag.name)
    queue.put(route['id'],bag.name,proof['content_identity'],'processing',attempts=attempt,proof=proof,error=None)
    try:
        if complete.exists():
            payload = json.loads(complete.read_text())
            if payload['identity'] != proof['content_identity'] or payload['source_snapshot'] != proof['snapshot']:
                raise InputChanged('Completed source snapshot changed; retained existing local data')
            from .transfer import verify_tree
            verify_tree(payload['dataset'],payload['dataset_files'])
        else:
            job = root / f'attempt-{attempt:04d}'
            # A crash after creating an attempt cannot overwrite that attempt.
            while job.exists():
                attempt += 1
                job = root / f'attempt-{attempt:04d}'
            size = sum(row['size'] for row in proof['files'])
            if shutil.disk_usage(root).free < size*3 + route['conversion']['min_free_gb']*10**9:
                raise OSError('Insufficient local disk reserve')
            with monitor_input(bag,proof['snapshot'],min(1,cfg['poll_seconds'])) as guard:
                receipt = direct_input(bag,proof)
                run(route['conversion'],job,episodes=[bag.name],validated_inputs={bag.name:receipt},
                    progress=progress,defer_source_final_hash=True)
                guard()
                outcome=json.loads((job/'summary.json').read_text())['jobs'][0]
                if outcome['state']=='excluded':
                    queue.put(route['id'],bag.name,proof['content_identity'],'excluded',proof=proof,
                              error=json.dumps(outcome.get('quality_rejection')))
                    return
                if outcome['state']!='awaiting_source_verification':
                    raise ValueError(outcome.get('error') or 'Local conversion did not pass')
                progress('original_final_hash')
                verify_input_content(bag,proof)
                guard()
                complete_source_verification(job,bag.name)
                progress('upload_manifest')
                payload=payload_from(cfg,route,bag,proof,job)
                guard()
                atomic_json(complete,payload)
        # Idempotent enqueue recovers a crash between the journal and local job
        # completion. Conversion and source verification are never repeated for
        # a successfully preserved local_complete.json.
        enqueue(queue,payload)
        queue.put(route['id'],bag.name,proof['content_identity'],'converted',proof=proof,error=None)
        progress('queued_for_upload')
    except InputChanged as exc:
        queue.put(route['id'],bag.name,None,'waiting_stable',attempts=0,proof=None,error=str(exc))
        queue.observe(route['id'],bag.name,None,time.time_ns(),cfg['poll_seconds']*10**9)
    except Exception as exc:
        queue.put(route['id'],bag.name,proof['content_identity'],'retry' if attempt<cfg['max_attempts'] else 'failed',
                  next_ns=time.time_ns()+cfg['retry_seconds']*10**9,error=f'{type(exc).__name__}: {exc}')
    # Raw data, validated dataset, reports and intermediates are retained in this
    # first version. Nothing is deleted following an upload.


def scan(cfg, queue, now_ns=None, should_stop=lambda:False):
    now=time.time_ns() if now_ns is None else now_ns
    candidates=[];errors={}
    for route in cfg['routes']:
        source=Path(route['source'])
        if not source.is_dir() or source.is_symlink():
            errors[route['id']]='Input directory missing or symlink'
            continue
        bags=sorted((p for p in source.iterdir() if EPISODE.fullmatch(p.name) and p.is_dir() and not p.is_symlink()),key=lambda p:int(p.name[7:]))
        skipped=queue.initialize_history(route['id'],[p.name for p in bags],route['history'],now)
        for bag in bags:
            if bag.name in skipped:
                continue
            old=queue.get(route['id'],bag.name)
            if old is None:
                old=queue.put(route['id'],bag.name,None,'waiting_stable')
            try:
                snap=snapshot(bag);signature=digest(snap)
                obs=queue.observe(route['id'],bag.name,signature,now,cfg['poll_seconds']*10**9)
            except Exception as exc:
                queue.put(route['id'],bag.name,None,'waiting_input',error=str(exc))
                continue
            previous=json.loads(old['proof']) if old.get('proof') else {}
            unchanged=digest(previous.get('snapshot',[]))==signature
            if old['state']=='converted':
                if not unchanged:
                    queue.put(route['id'],bag.name,old['identity'],'conflict',error='Converted original changed; existing output preserved')
                continue
            if old['state'] in ('conflict',) or (unchanged and old['state'] in ('excluded','failed')):
                continue
            if old['next_ns']>now:
                continue
            stable=obs['stable_count']>=cfg['stable_scans'] and now-obs['stable_since_ns']>=cfg['stable_seconds']*10**9
            if not stable:
                continue
            candidates.append((old['updated_ns'],route['id'],int(bag.name[7:]),route,bag,snap))
    processed=0
    for *_,route,bag,snap in sorted(candidates,key=lambda x:x[:3]):
        if should_stop() or processed>=cfg['max_jobs_per_scan']:
            break
        old=queue.get(route['id'],bag.name)
        queue.put(route['id'],bag.name,old['identity'],'checking',error=None)
        progress=reporter(cfg,route,bag.name)
        try:
            proof=check_input(bag,route,snap,lambda phase,**kw:progress('readiness.'+phase,**kw))
        except Exception as exc:
            queue.put(route['id'],bag.name,None,'waiting_input',error=f'{type(exc).__name__}: {exc}',
                      next_ns=time.time_ns()+cfg['retry_seconds']*10**9)
            continue
        if should_stop():
            queue.put(route['id'],bag.name,None,'waiting_stable',error=None)
            break
        process(cfg,queue,route,bag,proof)
        processed+=1
    counts={}
    for row in queue.rows():
        counts[row['state']]=counts.get(row['state'],0)+1
    report={'updated_ns':time.time_ns(),'counts':counts,'route_errors':errors}
    atomic_json(Path(cfg['state_root'])/'status.json',report)
    return report
