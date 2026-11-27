"""Independent ABCD raw backup. Conversion queues and fingerprints stay unchanged.

C/D never enter conversion. A/B successes are deleted by the existing capture
uploader after safe batch commit; excluded A/B can release after verified backup.
Failed/pending A/B remain available for conversion even when backup succeeds.
"""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import shutil
import time

from convest.config import digest
from convest.ingest.batches import durable_json
from convest.ingest.inputs import snapshot, require_same
from convest.ingest.queue import Queue
from convest.ingest.worker import own_directory
from convest.recordings import EPISODE,number
from convest.jobs import check_raw_ready
from convest.capture.retention import check_remaining
from convest.capture.transfer import ensure_real_path, manifest
from .transport import transfer, verify_backup


def settings(spec):
    task=spec['task'];root=Path(spec['root'])/'raw-archive/state'
    return {'state_root':str(root), 'fingerprint':digest([spec['capture']['fingerprint'],task['source'],
            task['raw_root'],spec['data_server'],'gui-abcd-raw-v1'])}


@contextmanager
def context(spec):
    cfg=settings(spec);root=Path(cfg['state_root'])
    ensure_real_path(root);own_directory(root,'gui-abcd-raw',cfg['fingerprint'])
    with (root/'upload.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        queue=Queue(root/'queue.sqlite3')
        try:yield queue
        finally:queue.close()


def groups(spec):
    source=Path(spec['task']['source'])
    # A direct group selection remains scoped to that group. A scene selection
    # continuously discovers C/D even if those directories are created later.
    return {source.name:source} if source.name in ('A','B','C','D') else {g:source/g for g in 'ABCD'}


def progress(spec,group,episode,**kw):
    durable_json(Path(settings(spec)['state_root'])/'upload_progress.json',
                 {'route':spec['task']['robot']+'-'+group,'episode':episode,'kind':'raw',
                  'updated_ns':time.time_ns(),**kw})


def delete(spec,queue,row,remote_verified=False):
    proof=json.loads(row['proof']);source=Path(proof['source'])
    group=proof['group'];expected=groups(spec)[group]/row['episode']
    taskid=digest([settings(spec)['fingerprint'],row['route'],row['episode'],row['identity']])
    quarantine=source.parent/'.raw-archive-cleanup'/taskid
    if source!=expected or proof.get('quarantine',str(quarantine))!=str(quarantine):
        raise ValueError('Raw deletion ownership mismatch')
    ensure_real_path(source);ensure_real_path(quarantine)
    receipt=proof.get('receipt',{})
    if receipt.get('status')!='completed' or receipt.get('destination')!=proof['destination']:
        raise ValueError('Missing durable raw archive receipt')
    # transfer() has just verified every remote byte and persisted its receipt.
    # Only resumed/deferred deletion needs another full remote read-back.
    if not remote_verified:
        verify_backup(spec['data_server'],proof['destination'],proof['files'],taskid)
    if proof.get('mcap_assets'):
        from .mcap_assets import deliver
        deliver(spec,source,proof['destination'],proof['mcap_assets'],verify_only=True)
    if row['state']!='deleting':
        require_same(source,proof['snapshot'])
        if quarantine.exists():raise ValueError('Unowned quarantine; retained')
        proof['quarantine']=str(quarantine)
        queue.put(row['route'],row['episode'],row['identity'],'deleting',proof=proof,error=None)
    if source.exists() and not quarantine.exists():
        require_same(source,proof['snapshot'])
        quarantine.parent.mkdir(exist_ok=True)
        source.rename(quarantine)
        try:check_remaining(quarantine,proof['snapshot'])
        except BaseException:
            if not source.exists():quarantine.rename(source)
            raise
    if quarantine.exists():
        check_remaining(quarantine,proof['snapshot'])
        if not shutil.rmtree.avoids_symlink_attacks:raise ValueError('Safe deletion unavailable')
        shutil.rmtree(quarantine)
    proof['deleted_ns']=time.time_ns()
    queue.put(row['route'],row['episode'],row['identity'],'deleted',proof=proof,error=None)


def release(spec,queue,capture,row,remote_verified=False):
    proof=json.loads(row['proof']);group=proof['group']
    if group in ('A','B'):
        saved=capture.db.execute('SELECT * FROM recordings WHERE route=? AND episode=?',
                                 (row['route'],row['episode'])).fetchone()
        if saved and saved['raw_deleted_ns']:
            payload=json.loads(saved['payload'])
            if payload['raw_files']!=proof['files']:raise ValueError('Capture/raw archive identity mismatch')
            proof['deleted_ns']=saved['raw_deleted_ns'];proof['deleted_by']='capture'
            queue.put(row['route'],row['episode'],row['identity'],'deleted',proof=proof,error=None)
            return
        job=capture.get(row['route'],row['episode'])
        if not job or job['state']!='excluded':
            # Successful A/B use their existing handoff, ledger and raw-deletion
            # protocol. Never delete a pending/retry/failed conversion's input.
            return
        checked=json.loads(job['proof'] or '{}')
        if checked.get('snapshot')!=proof['snapshot'] or checked.get('files')!=proof['files']:
            raise ValueError('Excluded recording proof changed; retained')
    if spec['task'].get('delete_raw',True):
        delete(spec,queue,row,remote_verified)


def scan(spec,queue,capture,now_ns=None,should_stop=lambda:False):
    now=time.time_ns() if now_ns is None else now_ns
    cfg=spec['capture'];robot=spec['task']['robot'];candidates=[]
    # The original A/B uploader may finish while this independent backup is
    # hashing. Reconcile that journal instead of leaving a spurious retry for
    # a source which was already safely deleted by the original protocol.
    for row in queue.rows():
        if row['route'].rsplit('-',1)[-1] not in ('A','B') or row['state'] in ('deleted','conflict'):continue
        saved=capture.db.execute('SELECT * FROM recordings WHERE route=? AND episode=?',
                                 (row['route'],row['episode'])).fetchone()
        if not saved or not saved['raw_deleted_ns']:continue
        p=json.loads(saved['payload']);old=json.loads(row['proof'] or '{}')
        if old.get('files') is not None and old['files']!=p['raw_files']:continue
        proof={**old,**({'mcap_assets':p['mcap_assets']} if p.get('mcap_assets') else {}),'source':p['source'],'group':p['group'],'files':p['raw_files'],
               'snapshot':p['source_snapshot'],'deleted_ns':saved['raw_deleted_ns'],'deleted_by':'capture'}
        queue.put(row['route'],row['episode'],digest(p['raw_files']),'deleted',proof=proof,error=None,next_ns=0)
    for group,source in groups(spec).items():
        try:ensure_real_path(source)
        except ValueError as exc:
            progress(spec,group,'',phase='retry',error=str(exc));continue
        if not source.is_dir():continue
        rid=robot+'-'+group
        bags=sorted((p for p in source.iterdir() if EPISODE.fullmatch(p.name) and p.is_dir() and not p.is_symlink()),key=lambda p:number(p.name))
        history={'mode':'all' if spec['task'].get('include_existing',True) else 'none','episodes':[]}
        skipped=queue.initialize_history(rid,[p.name for p in bags],history,now)
        for bag in bags:
            if bag.name in skipped:continue
            row=queue.get(rid,bag.name)
            if row and row['state'] in ('uploaded','deleting','deleted','conflict'):continue
            try:snap=snapshot(bag);sig=digest(snap)
            except (OSError,ValueError) as exc:
                queue.put(rid,bag.name,None,'retry',error=str(exc),next_ns=now+cfg['retry_seconds']*10**9);continue
            old=row or queue.put(rid,bag.name,None,'waiting_stable')
            obs=queue.observe(rid,bag.name,sig,now,cfg['poll_seconds']*10**9)
            if old['next_ns']>now:continue
            if obs['stable_count']<cfg['stable_scans'] or now-obs['stable_since_ns']<cfg['stable_seconds']*10**9:continue
            candidates.append((old['updated_ns'],rid,number(bag.name),group,bag,snap))
    for row in queue.rows():
        if row['state']=='deleted':
            proof=json.loads(row['proof'])
            if Path(proof['source']).exists():
                queue.put(row['route'],row['episode'],row['identity'],'conflict',error='Deleted recording name reappeared; preserved')
        if row['state'] not in ('uploaded','deleting') or row['next_ns']>now:continue
        if should_stop():return
        try:release(spec,queue,capture,row)
        except Exception as exc:
            current=queue.get(row['route'],row['episode'])
            queue.put(row['route'],row['episode'],current['identity'],current['state'],error=str(exc),next_ns=now+cfg['retry_seconds']*10**9)
    if should_stop() or not candidates:return
    _,rid,_,group,bag,snap=sorted(candidates,key=lambda r:r[:3])[0]
    old=queue.get(rid,bag.name)
    try:
        report=lambda phase,**kw:progress(spec,group,bag.name,phase=phase,**kw)
        report('raw_integrity_check' if group in ('A','B') else 'uploading_original_files')
        require_same(bag,snap)
        # C/D are archive-only: no bag decoding, metadata/SQLite/topic counts,
        # required streams, idle detection or conversion-quality checks.
        from convest.mcap_input import require_format,is_mcap,lifecycle
        require_format(bag,spec['task'].get('input_format','rosbag'))
        if is_mcap(bag):lifecycle(bag)  # lifecycle only for C/D, no payload decoding
        ready=check_raw_ready(bag,report) if group in ('A','B') else None
        if group in ('C','D') and (bag/'collection_state.json').exists():
            state=json.loads((bag/'collection_state.json').read_text())
            if state.get('state')!='finalized' or state.get('finalized') is not True:
                raise ValueError('录制尚未结束，等待后直接上传原文件')
        require_same(bag,snap)
        # Include every original file, not only metadata-listed SQLite volumes.
        files=manifest(bag)
        require_same(bag,snap)
        identity=digest(files)
        dest=Path(spec['task']['raw_root'])/group/bag.name
        taskid=digest([settings(spec)['fingerprint'],rid,bag.name,identity])
        if should_stop():return
        shared=[]
        if is_mcap(bag):
            from .mcap_assets import deliver
            shared=deliver(spec,bag,dest)
        def guard():
            require_same(bag,snap)
            if shared:
                from convest.mcap_input import check_assets
                check_assets(shared)
        receipt=transfer(spec['data_server'],bag,dest,files,taskid,
                         lambda **kw:progress(spec,group,bag.name,**kw),guard)
        guard()
        proof={'source':str(bag),'group':group,'snapshot':snap,'files':files,
               'mcap_assets':shared,'destination':str(dest),'receipt':receipt,'raw_integrity_verified':ready is not None,
               'content_validation':'raw_integrity' if ready is not None else 'skipped_archive_only',
               'messages':ready['messages'] if ready else None}
        durable_json(Path(settings(spec)['state_root'])/'receipts'/(taskid+'.json'),proof)
        row=queue.put(rid,bag.name,identity,'uploaded',proof=proof,error=None,next_ns=0)
        release(spec,queue,capture,row,remote_verified=True)
        total=sum(f['size'] for f in files)
        progress(spec,group,bag.name,phase='completed',bytes_done=total,bytes_total=total,destination=str(dest))
    except Exception as exc:
        current=queue.get(rid,bag.name)
        state=current['state'] if current['state'] in ('uploaded','deleting') else 'retry'
        queue.put(rid,bag.name,current['identity'],state,attempts=old['attempts']+1,error=str(exc),next_ns=now+cfg['retry_seconds']*10**9)
        progress(spec,group,bag.name,phase='retry',error=str(exc))
