"""Release proven batch work and retired C/D conversions, keeping all journals.

Only persisted capture batch membership and owned job/output paths are accepted.
Legacy C/D runs must hold all worker locks; their unused training tasks are retired.
"""
import json
from convest.recordings import EPISODE
from pathlib import Path
import re
import sqlite3
import time

from convest.config import digest
from convest.ingest.batches import durable_json
from convest.ingest.inputs import snapshot
from convest.capture.transfer import ensure_real_path, manifest
from convest.training import worker as training
from . import retention


def read(path):return json.loads(Path(path).read_text())


def members(spec, src):
    route=next(r for r in spec['capture']['routes'] if r['id']==src['route'])
    if route['id']!=route['robot']+'-'+route['group'] or (route['robot'],route['group'])!=(src['robot'],src['group']):
        raise ValueError('Invalid batch route')
    if not re.fullmatch(r'batch-[0-9]{6,}',src['batch_id']):raise ValueError('Invalid batch identity')
    if not re.fullmatch(r'(?:[0-9]{4}_)?[0-9]+_[0-9]+_data_lerobot',src['name']):
        raise ValueError('Invalid batch name')
    with sqlite3.connect(Path(spec['capture']['state_root']).joinpath('queue.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        batch=db.execute('SELECT * FROM capture_batches WHERE route=? AND batch_id=?',(src['route'],src['batch_id'])).fetchone()
        rows=db.execute('SELECT * FROM recordings WHERE route=? AND batch_id=?',(src['route'],src['batch_id'])).fetchall()
        if not batch or batch['generation']!=src['source']:raise ValueError('批次来源与本机台账不符，保留工作文件')
        names=json.loads(batch['sources'])
        if not names or {r['episode'] for r in rows}!=set(names):raise ValueError('缺少完整的原包来源清单，保留工作文件')
        result=[]
        for row in rows:
            p=json.loads(row['payload'])
            if (p['route'],p['group'],p['batch_id'],p['source'])!=(src['route'],src['group'],src['batch_id'],str(Path(route['source'])/row['episode'])):
                raise ValueError('原包来源证明不匹配')
            if not EPISODE.fullmatch(row['episode']) or not re.fullmatch('[a-f0-9]{64}',p['identity']):
                raise ValueError('原包作业标识无效')
            result.append(p)
    return route,result


def raw_receipt(spec, p, fresh=False):
    """Resolve a legacy raw receipt or a matching new ABCD archival receipt."""
    cap=spec['capture'];dest=str(Path(spec['task']['raw_root'])/p['group']/p['episode'])
    if p.get('mcap_assets'):
        from .mcap_assets import deliver
        deliver(spec,p['source'],dest,p['mcap_assets'],verify_only=True)
    def checked(receipt, identity):
        token=digest({'destination':dest,'identity':identity,'files':p['raw_files']})
        if receipt.get('status')!='completed' or receipt.get('destination')!=dest or receipt.get('token')!=token:
            raise ValueError('原包完成凭据不匹配')
        if fresh:retention.verify_backup(spec['data_server'],dest,p['raw_files'],identity)
        return True
    with sqlite3.connect(Path(cap['state_root']).joinpath('queue.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        for row in db.execute('SELECT * FROM uploads WHERE route=? AND episode=? AND raw_done=1',(p['route'],p['episode'])):
            payload=json.loads(row['payload'])
            if payload.get('kind')!='raw' or payload.get('raw_files')!=p['raw_files']:continue
            receipt=Path(cap['state_root'])/'receipts'/(row['id']+'.json')
            if receipt.exists():return checked(read(receipt),digest([cap['fingerprint'],row['id']]))
    # A new version may have backed up the same original for an older C/D run.
    from . import raw_archive
    run=Path(spec['root']) if spec.get('root') else None
    files=sorted(run.parents[2].joinpath('runs').glob('task-*/*/spec.json')) if run else []
    for file in files:
        ensure_real_path(file);other=read(file)
        if Path(other.get('root',''))!=file.parent:continue
        if other.get('data_server')!=spec['data_server']:continue
        state=Path(raw_archive.settings(other)['state_root']);queue=state/'queue.sqlite3'
        if not queue.exists():continue
        ensure_real_path(queue)
        with sqlite3.connect(queue.as_uri()+'?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row
            row=db.execute("SELECT * FROM jobs WHERE route=? AND episode=? AND state IN ('uploaded','deleting','deleted')",(p['route'],p['episode'])).fetchone()
        if not row or not row['proof']:continue
        proof=json.loads(row['proof'])
        if proof.get('files')!=p['raw_files'] or proof.get('destination')!=dest:continue
        identity=digest([raw_archive.settings(other)['fingerprint'],p['route'],p['episode'],row['identity']])
        receipt=state/'receipts'/(identity+'.json')
        if receipt.exists():return checked(read(receipt)['receipt'],identity)
    raise ValueError('缺少原始ROSbag完整备份凭据：'+p['route']+'/'+p['episode'])


def work_paths(spec, route, recordings, require_complete=True):
    allowed=set()
    for p in recordings:
        if (not EPISODE.fullmatch(p['episode']) or not re.fullmatch('[a-f0-9]{64}',p['identity'])
                or p['route']!=route['id'] or p['source']!=str(Path(route['source'])/p['episode'])):
            raise ValueError('Invalid work source identity')
        root=Path(spec['capture']['work_root'])/route['id']/p['episode']/p['identity']
        ensure_real_path(root)
        if not root.exists():continue
        complete=root/'local_complete.json'
        if require_complete and (not complete.exists() or read(complete)!=p):raise ValueError('缺少匹配的工作目录完成证明：'+str(root))
        for attempt in root.glob('attempt-*'):
            ensure_real_path(attempt)
            marker=attempt/'manifest.json'
            if not marker.exists():raise ValueError('工作目录缺少归属证明：'+str(attempt))
            owner=read(marker)
            if owner.get('owner')!='convert-data-job-worker' or owner.get('config',{}).get('source_root')!=route['source']:
                raise ValueError('工作目录归属不符：'+str(attempt))
            allowed.update(attempt/name for name in ('cleaned','dataset'))
    return allowed


def apply_paths(state, identity, allowed):
    allowed={Path(p) for p in allowed}
    old=read(state) if state.exists() else {}
    if old.get('status')=='released':return
    if not old.get('trees'):
        trees=[]
        for folder in sorted(allowed):
            ensure_real_path(folder)
            if folder.exists():
                trees.append({'path':str(folder),'snapshot':snapshot(folder),'files':manifest(folder),
                              'quarantine':str(folder.parent/('.payload-release-'+identity+'-'+folder.name))})
        old={'status':'releasing','identity':identity,'trees':trees,'verified_ns':time.time_ns()}
        durable_json(state,old)
    if old.get('identity')!=identity:raise ValueError('工作清理意图不匹配')
    if not retention.shutil.rmtree.avoids_symlink_attacks:raise ValueError('Safe deletion unavailable')
    for tree in old['trees']:
        folder=Path(tree['path']);quarantine=Path(tree['quarantine'])
        if folder not in allowed or quarantine!=folder.parent/('.payload-release-'+identity+'-'+folder.name):
            raise ValueError('Unsafe work release path')
        ensure_real_path(folder);ensure_real_path(quarantine)
        if folder.exists():
            if quarantine.exists():raise ValueError('Ambiguous work quarantine')
            retention.check_tree(folder,tree);folder.rename(quarantine)
        if quarantine.exists():
            retention.check_tree(quarantine,tree,partial=True);retention.shutil.rmtree(quarantine)
    old.update(status='released',released_ns=time.time_ns());durable_json(state,old)


def release_work(spec, row):
    state=Path(spec['training']['state_root'])/'work-retention'/(row['id']+'.json')
    if state.exists() and read(state).get('status')=='released':return
    src=json.loads(row['spec']);route,recordings=members(spec,src)
    for p in recordings:raw_receipt(spec,p)
    apply_paths(state,row['id'],work_paths(spec,route,recordings))


def unqueued_cd(spec, tasks):
    """C/D may have a complete capture batch but no training task at all."""
    known={(json.loads(t['spec']).get('route'),json.loads(t['spec']).get('batch_id')) for t in tasks}
    routes={r['id']:r for r in spec['capture'].get('routes',[]) if r['group'] in ('C','D')}
    if not routes:return []
    result=[]
    with sqlite3.connect(Path(spec['capture']['state_root']).joinpath('queue.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        for batch in db.execute('SELECT * FROM capture_batches'):
            if batch['route'] not in routes or (batch['route'],batch['batch_id']) in known:continue
            route=routes[batch['route']]
            src={'route':route['id'],'robot':route['robot'],'group':route['group'],'batch_id':batch['batch_id'],
                 'name':batch['name'],'source':batch['generation'],'recipe':'archive-only-v1'}
            result.append({'id':digest([spec['training']['fingerprint'],'retired-cd',src]),'spec':json.dumps(src),
                           'state':'archive_only','payload':None})
    return result


def unbatched_cd(spec):
    """Older failed/excluded C/D attempts may never have entered any batch."""
    routes={r['id']:r for r in spec['capture'].get('routes',[]) if r['group'] in ('C','D')}
    if not routes:return []
    result=[]
    with sqlite3.connect(Path(spec['capture']['state_root']).joinpath('queue.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        for row in db.execute('SELECT j.* FROM jobs j LEFT JOIN recordings r ON j.route=r.route AND j.episode=r.episode WHERE r.episode IS NULL'):
            if row['route'] not in routes or not row['proof']:continue
            proof=json.loads(row['proof']);route=routes[row['route']]
            if proof.get('content_identity')!=row['identity'] or not proof.get('files') or not proof.get('snapshot'):continue
            if not EPISODE.fullmatch(row['episode']) or not re.fullmatch('[a-f0-9]{64}',row['identity'] or ''):continue
            p={'route':route['id'],'robot':route['robot'],'group':route['group'],'episode':row['episode'],
               'source':str(Path(route['source'])/row['episode']),'identity':row['identity'],'raw_files':proof['files']}
            tid=digest(['cd-unbatched',spec['capture']['fingerprint'],p])
            src={'group':route['group'],'name':route['group']+'/'+row['episode'],'work_only':True,'recording':p}
            result.append({'id':tid,'spec':json.dumps(src),'payload':None,'state':'archive_only'})
    return result


def legacy_cd_work(spec,row,apply=False):
    p=json.loads(row['spec'])['recording'];tid=row['id']
    state=Path(spec['training']['state_root'])/'cd-work-retention'/(tid+'.json')
    if state.exists() and read(state).get('status')=='released':return 'released'
    route=next(r for r in spec['capture']['routes'] if r['id']==p['route'])
    if route['group'] not in ('C','D') or route['id']!=route['robot']+'-'+route['group']:raise ValueError('Invalid C/D work route')
    allowed=work_paths(spec,route,[p],require_complete=False)
    raw_receipt(spec,p,fresh=apply)
    if not apply:return 'candidate'
    apply_paths(state,tid,allowed)
    return 'released'


def legacy_cd(spec, db, row, apply=False):
    src=json.loads(row['spec']);cfg=spec['training'];tid=row['id']
    state=Path(cfg['state_root'])/'cd-retention'/(tid+'.json')
    if state.exists() and read(state).get('status')=='released':return 'released'
    if src['group'] not in ('C','D') or not re.fullmatch('[a-f0-9]{64}',tid):raise ValueError('Invalid legacy C/D task')
    route,recordings=members(spec,src)
    source=Path(src['source']);store=Path(route['output'])/'.ingest'
    generation_store=store/'streams'/src['batch_id']/'.worker-generations/current'
    if source.parent!=generation_store:raise ValueError('Unexpected legacy generation store')
    allowed=work_paths(spec,route,recordings);visited=set();generation=source
    while generation:
        ensure_real_path(generation)
        if generation.parent!=generation_store or generation in visited or not generation.name.startswith('gen-'):
            raise ValueError('Unsafe legacy generation ancestry')
        visited.add(generation);marker=read(generation/'ingest/generation.json')
        if (marker.get('route'),marker.get('fingerprint'),marker.get('state'))!=(route['id'],spec['capture']['fingerprint'],'ready'):
            raise ValueError('Legacy generation ownership mismatch')
        allowed.update(generation/name for name in ('data','videos'))
        generation=Path(marker['parent']) if marker.get('parent') else None
    output=Path(cfg['output_root']);ensure_real_path(output)
    expected={'owner':'capture-native-training-output-v1','fingerprint':cfg['fingerprint']}
    if output.exists() and read(output/'owner.json')!=expected:raise ValueError('Legacy training output ownership mismatch')
    pair=output/src['robot']/src['group']/training.pair_name(src,tid)
    if row['payload'] and Path(json.loads(row['payload'])['source'])!=pair:raise ValueError('Legacy pair path mismatch')
    for root in (pair,output/'.staging'/tid):
        ensure_real_path(root)
        if not root.exists():continue
        if read(root/'source.json')['spec']!=src:raise ValueError('Legacy pair source proof mismatch')
        allowed.update(root/variant/name for variant in ('normal','speedup') for name in ('data','videos'))
    for p in recordings:raw_receipt(spec,p,fresh=apply)
    if not apply:return 'candidate'
    with (store/'publish.lock').open('a') as lock:
        import fcntl
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # Prevent a later restart from trying to convert the retired C/D source.
        with db:db.execute("UPDATE tasks SET state='archived_raw_only',next_ns=0,error=NULL WHERE id=?",(tid,))
        with sqlite3.connect(Path(spec['capture']['state_root'])/'queue.sqlite3') as capture:
            capture.execute('PRAGMA synchronous=FULL')
            for uid,encoded in capture.execute("SELECT id,payload FROM uploads WHERE route=? AND state!='completed'",(src['route'],)).fetchall():
                if json.loads(encoded).get('batch_id')==src['batch_id']:
                    capture.execute("UPDATE uploads SET state='archived_raw_only',next_ns=0,error=NULL,updated_ns=? WHERE id=?",(time.time_ns(),uid))
            capture.execute("UPDATE capture_batches SET state='archived_raw_only' WHERE route=? AND batch_id=? AND state!='uploaded'",(src['route'],src['batch_id']))
        apply_paths(state,tid,allowed)
    return 'released'
