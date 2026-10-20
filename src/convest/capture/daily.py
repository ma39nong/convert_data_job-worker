"""24-hour local LeRobot batches, immediate raw backup, and verified raw release."""
from pathlib import Path
import json
import os
import shutil
import sqlite3
import time
from convest.config import atomic_json,digest
from convest.ingest.batches import BatchPublisher,durable_json
from convest.ingest.worker import archive_reports
from convest.ingest.inputs import require_same,InputChanged,snapshot
from . import ledger
from .transfer import enqueue,manifest,copy_tree,verify_tree,check_mount,ensure_real_path,TransferConflict
from .retention import delete_original


def publisher(cfg,route):
    return BatchPublisher(route,cfg['fingerprint'])


def payload_for(cfg,route,bag,proof,generation):
    batch=next(b for b in publisher(cfg,route).status() if bag.name in b.get('sources',[]))
    return {'kind':'raw','route':route['id'],'robot':route['robot'],'group':route['group'],
            'episode':bag.name,'identity':proof['content_identity'],'recording_id':proof['recording_id'],
            'source':str(bag),'source_snapshot':proof['snapshot'],'raw_files':proof['files'],
            'quality_verified':True,'batch_id':batch['id'],'batch_name':batch['name'],'dataset':str(generation)}


def recover(cfg,route,bag,proof):
    pub=publisher(cfg,route);known=pub.published()
    if bag.name not in known:return None
    if known[bag.name]['ingest']['content_identity']!=proof['content_identity']:
        raise TransferConflict('Local batch already contains a different recording with this name')
    batch=next(b for b in pub.status() if bag.name in b.get('sources',[]))
    return payload_for(cfg,route,bag,proof,Path(batch['generation']))


def commit(cfg,queue,route,bag,proof,job,guard,started_ns):
    archive=Path(cfg['state_root'])/'audits'/route['id']/bag.name/proof['content_identity']
    archive_reports(job,archive)
    shutil.copyfile(job/'cleaned'/bag.name/'trim_report.json',archive/'trim_report.json')
    generation=publisher(cfg,route).publish(job/'dataset',proof,bag.name,archive,precommit=guard,started_ns=started_ns)
    return payload_for(cfg,route,bag,proof,generation)


def handoff(cfg,queue,payload):
    ledger.converted(queue,payload)
    task=enqueue(queue,payload)
    ledger.export(cfg,queue)
    return task


def cleanup_intermediates(cfg,route,bag,proof,root):
    # Every training artifact and JSON audit is already held in an immutable
    # local batch generation. Keep compact job summaries, discard reconstructible
    # cleaned bags and per-source dataset duplicates only under our owned job.
    if not cfg.get('cleanup_intermediates',True):return
    known=publisher(cfg,route).published()
    if known.get(bag.name,{}).get('ingest',{}).get('content_identity')!=proof['content_identity']:
        raise ValueError('Cannot release work before local batch commit')
    for job in root.glob('attempt-*'):
        if job.is_symlink() or not job.is_dir():raise ValueError('Unsafe job cleanup')
        marker=job/'manifest.json'
        if not marker.exists() or json.loads(marker.read_text()).get('owner')!='convert-data-job-worker':
            raise ValueError('Unowned job cleanup refused')
        archive_reports(job,Path(cfg['state_root'])/'audits'/route['id']/bag.name/proof['content_identity']/'work-attempts'/job.name)
        for folder in ('cleaned','dataset'):
            path=job/folder
            if path.is_symlink():raise ValueError('Cleanup symlink refused')
            if path.exists():shutil.rmtree(path)


def maintain(cfg,queue,now_ns=None):
    now=time.time_ns() if now_ns is None else now_ns
    errors={}
    for route in cfg['routes']:
        pub=publisher(cfg,route)
        try:
            pub.published(now_ns=now)  # seals due batches even without new bags
            for batch in pub.status():
                old=queue.db.execute('SELECT state FROM capture_batches WHERE route=? AND batch_id=?',(route['id'],batch['id'])).fetchone()
                state='uploaded' if old and old[0]=='uploaded' else batch['state']
                with queue.db:
                    queue.db.execute('''INSERT INTO capture_batches(route,batch_id,name,generation,state,opened_ns,deadline_ns,frames,sources)
                      VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(route,batch_id) DO UPDATE SET
                      name=excluded.name,generation=excluded.generation,state=CASE WHEN capture_batches.uploaded_ns IS NOT NULL THEN 'uploaded' ELSE excluded.state END,opened_ns=excluded.opened_ns,
                      deadline_ns=excluded.deadline_ns,frames=excluded.frames,sources=excluded.sources''',
                      (route['id'],batch['id'],batch.get('name'),batch.get('generation'),state,batch.get('opened_ns'),
                       batch.get('deadline_ns'),batch.get('frames'),json.dumps(batch.get('sources',[]))))
                if batch['state']!='sealed':continue
                identity=digest([route['id'],batch['id'],batch['generation'],batch['deadline_ns']])
                taskid=digest([route['id'],batch['id'],identity])
                if queue.db.execute('SELECT 1 FROM uploads WHERE id=?',(taskid,)).fetchone():continue
                root=Path(batch['generation'])
                files=manifest(root)
                payload={'kind':'batch','route':route['id'],'robot':route['robot'],'group':route['group'],
                         'episode':batch['id'],'identity':identity,'batch_id':batch['id'],'batch_name':batch['name'],
                         'dataset':str(root),'dataset_files':files,'dataset_snapshot':snapshot(root),
                         'sources':batch['sources'],'opened_ns':batch['opened_ns'],'deadline_ns':batch['deadline_ns'],
                         'seal_verification':batch['verification']}
                enqueue(queue,payload)
                with queue.db:ledger.event(queue,route['id'],batch['id'],'batch_sealed',{'name':batch['name']})
        except Exception as exc:
            errors[route['id']]=f'{type(exc).__name__}: {exc}'
    ledger.export(cfg,queue)
    return errors


def acknowledge_receipt(path,content):
    ensure_real_path(path)
    atomic_json(path,content)
    with path.open('rb') as f:os.fsync(f.fileno())
    if json.loads(path.read_text())!=content:
        raise ValueError('Remote completion receipt read-back differs')


def upload_once(cfg,queue):
    queue.db.row_factory=sqlite3.Row
    candidates=[dict(r) for r in queue.db.execute("SELECT * FROM uploads WHERE state IN ('pending','retry','uploading') AND next_ns<=? ORDER BY updated_ns,id",(time.time_ns(),))]
    if not candidates:
        try:ledger.mirror(cfg,queue)
        except Exception as exc:print('Ledger backup pending: '+str(exc),flush=True)
        return False
    raw=[r for r in candidates if json.loads(r['payload'])['kind']=='raw']
    batches=[r for r in candidates if json.loads(r['payload'])['kind']=='batch']
    # Raw backups have priority, but a sealed 24h batch cannot starve behind an
    # unlimited stream of new originals. Persist the alternation across restart.
    streak=queue.db.execute('SELECT raw_streak FROM capture_schedule WHERE id=1').fetchone()[0]
    row=(batches[0] if batches and (not raw or streak>=1) else raw[0])
    payload=json.loads(row['payload']);kind=payload['kind']
    def update(**values):
        values['updated_ns']=time.time_ns()
        with queue.db:
            queue.db.execute('UPDATE uploads SET '+','.join(k+'=?' for k in values)+' WHERE id=?',[*values.values(),row['id']])
    def progress(phase,**detail):
        atomic_json(Path(cfg['state_root'])/'upload_progress.json',{'route':row['route'],'episode':row['episode'],
                    'kind':kind,'phase':phase,'updated_ns':time.time_ns(),**detail})
    try:
        check_mount(cfg)
        update(state='uploading',attempts=row['attempts']+1,error=None)
        if kind=='raw':
            destination=Path(cfg['remote_raw_root'])/payload['robot']/payload['group']/payload['episode']
            receipt=Path(cfg['remote_raw_root'])/'.capture-transfer'/cfg['fingerprint']/(row['id']+'.json')
            if not row['raw_done']:
                def guard():check_mount(cfg);require_same(payload['source'],payload['source_snapshot'])
                progress('uploading_raw')
                copy_tree(payload['source'],destination,payload['raw_files'],digest([cfg['fingerprint'],row['id']])+'-raw',guard,
                          lambda **kw:progress('uploading_raw',**kw))
                record={'kind':'raw','status':'completed','route':row['route'],'episode':row['episode'],
                        'identity':payload['identity'],'raw':str(destination),'files':payload['raw_files'],
                        'quality_verified':True,'completed_ns':time.time_ns()}
                acknowledge_receipt(receipt,record)
                update(raw_done=1)
            else:
                record=json.loads(receipt.read_text())
                if record.get('identity')!=payload['identity'] or record.get('files')!=payload['raw_files'] or record.get('status')!='completed' or record.get('raw')!=str(destination) or record.get('kind')!='raw' or not record.get('quality_verified'):
                    raise TransferConflict('Raw deletion recovery receipt differs')
                # Following a crash, reverify the existing remote backup before
                # resuming an irreversible deletion; normal first pass is once.
                try:verify_tree(destination,payload['raw_files'],lambda **kw:progress('rechecking_backup',**kw))
                except (ValueError,InputChanged) as exc:raise TransferConflict(str(exc)) from exc
            stored=queue.db.execute('SELECT raw_uploaded_ns FROM recordings WHERE route=? AND episode=?',(row['route'],row['episode'])).fetchone()
            if not stored or not stored[0]:
                ledger.change(queue,payload,{'raw_uploaded_ns':record['completed_ns'],'raw_receipt':str(receipt)},'raw_uploaded')
                ledger.export(cfg,queue)
            if cfg['delete_raw_after_upload']:
                progress('deleting_verified_original')
                delete_original(cfg,queue,payload,row['id'])
            update(state='completed',next_ns=0,error=None)
            with queue.db:queue.db.execute('UPDATE capture_schedule SET raw_streak=raw_streak+1 WHERE id=1')
            progress('raw_completed',raw_deleted=cfg['delete_raw_after_upload'],receipt=str(receipt))
        else:
            destination=Path(cfg['remote_dataset_root'])/payload['robot']/payload['group']/payload['batch_name']
            def guard():check_mount(cfg);require_same(payload['dataset'],payload['dataset_snapshot'])
            progress('uploading_24h_batch')
            copy_tree(payload['dataset'],destination,payload['dataset_files'],digest([cfg['fingerprint'],row['id']])+'-batch',guard,
                      lambda **kw:progress('uploading_24h_batch',**kw))
            receipt=Path(cfg['remote_dataset_root'])/'.capture-transfer'/cfg['fingerprint']/(row['id']+'.json')
            acknowledge_receipt(receipt,{'kind':'batch','status':'completed','route':row['route'],'batch_id':payload['batch_id'],
                                'identity':payload['identity'],'dataset':str(destination),'sources':payload['sources'],
                                'files':payload['dataset_files'],'seal_verification':payload['seal_verification'],'completed_ns':time.time_ns()})
            with queue.db:
                queue.db.execute('UPDATE capture_batches SET state=?,uploaded_ns=?,remote_path=? WHERE route=? AND batch_id=?',
                                 ('uploaded',time.time_ns(),str(destination),row['route'],payload['batch_id']))
                ledger.event(queue,row['route'],payload['batch_id'],'batch_uploaded',{'destination':str(destination)})
                queue.db.execute('UPDATE capture_schedule SET raw_streak=0 WHERE id=1')
            update(state='completed',dataset_done=1,next_ns=0,error=None)
            progress('batch_completed',receipt=str(receipt))
        ledger.export(cfg,queue)
        try:ledger.mirror(cfg,queue)
        except Exception as exc:print('Ledger backup pending: '+str(exc),flush=True)
    except Exception as exc:
        state='conflict' if isinstance(exc,(TransferConflict,InputChanged)) else 'retry'
        update(state=state,attempts=row['attempts']+1,next_ns=time.time_ns()+cfg['retry_seconds']*min(2**min(row['attempts'],6),60)*10**9,
               error=f'{type(exc).__name__}: {exc}')
        progress(state,error=str(exc))
        ledger.export(cfg,queue)
    return True
