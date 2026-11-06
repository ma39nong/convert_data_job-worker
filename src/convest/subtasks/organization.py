"""Paired episode compaction with verified staging and recoverable server swaps."""
import base64
import copy
import hashlib
import json
import math
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .core import Episode, atomic_json, sha
from .vocabulary import english_document


def registry(ws):
    p=ws.root/'organized.json'
    return json.loads(p.read_text()) if p.exists() else {'retired':{},'entries':{},'operations':[]}


def remote_path(entry):
    p=Path(entry['dataset'])/'cache-source.json'
    return json.loads(p.read_text())['manifest']['dataset'] if p.exists() else entry['dataset']


def remote(script,body):
    r=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','28',shlex.join(['python3','-c',script])],input=json.dumps(body),text=True,capture_output=True,timeout=3600)
    if r.returncode:raise ValueError('整理未完成：'+r.stderr[-1200:])
    return json.loads(r.stdout)


READ_META=r'''
import json,base64,hashlib
from pathlib import Path
body=json.load(__import__('sys').stdin);out={}
for source in body['sources']:
 root=Path(source);rows={}
 for rel in ('meta/episodes_stats.jsonl','processing/summary.json','processing/normal.json'):
  p=root/rel
  if p.exists():
   data=p.read_bytes();rows[rel]={'sha256':hashlib.sha256(data).hexdigest(),'data':base64.b64encode(data).decode()}
 out[source]=rows
print(json.dumps(out))
'''


def stats_for(table):
    stats={}
    for key in table.column_names:
        try:a=np.asarray(table[key].to_pylist(),dtype=np.float64)
        except (ValueError,TypeError):continue
        if a.ndim==1:a=a[:,None]
        if a.ndim!=2 or not np.isfinite(a).all():continue
        stats[key]={k:v.tolist() for k,v in [('min',a.min(0)),('max',a.max(0)),('mean',a.mean(0)),('std',a.std(0))]}
        stats[key]['count']=[len(a)]
    return stats


def image_stats(path):
    import av
    sums=np.zeros(3);squares=np.zeros(3);low=np.full(3,np.inf);high=-low;pixels=0;frames=0
    with av.open(str(path)) as video:
        for frame in video.decode(video=0):
            a=frame.to_ndarray(format='rgb24').astype(np.float64).reshape(-1,3)/255
            sums+=a.sum(0);squares+=(a*a).sum(0);low=np.minimum(low,a.min(0));high=np.maximum(high,a.max(0));pixels+=len(a);frames+=1
    if not frames:raise ValueError('视频为空')
    mean=sums/pixels
    return {k:v[:,None,None].tolist() for k,v in [('min',low),('max',high),('mean',mean),('std',np.sqrt(np.maximum(0,squares/pixels-mean*mean)))]}|{'count':[frames]}


def aggregate(rows):
    if not rows:return {}
    result={}
    for key in set.intersection(*(set(row) for row in rows)):
        items=[row[key] for row in rows];weights=np.asarray([x['count'][0] for x in items],float);total=weights.sum()
        means=np.asarray([x['mean'] for x in items]);stds=np.asarray([x['std'] for x in items]);w=weights.reshape((-1,)+(1,)*(means.ndim-1));mean=(means*w).sum(0)/total
        var=((stds**2+(means-mean)**2)*w).sum(0)/total
        result[key]={'min':np.min([x['min'] for x in items],axis=0).tolist(),'max':np.max([x['max'] for x in items],axis=0).tolist(),'mean':mean.tolist(),'std':np.sqrt(np.maximum(var,0)).tolist(),'count':[int(total)]}
    return result


def build_package(ws,rows,destination,remote_destination,operation,metadata):
    """Rebuild only episode/index fields; all physical columns and video bytes stay identical."""
    destination.mkdir(parents=True)
    seed=ws.store.episode(rows[0]['id']) if rows else None
    if seed is None:raise ValueError('空分组不生成LeRobot包')
    info=copy.deepcopy(seed.info);chunk=int(info['chunks_size']);entries=[];statistics=[];mapping=[];sources={};video_copies=[];offset=0
    for row in rows:
        ep=ws.store.episode(row['id']);old=row['source_episode_index'];new=row['new_episode_index'];identity=ep.identity();server_source=remote_path(ws.store.entries[ep.id])
        source_files=sources.setdefault(server_source,{})
        for rel,digest in identity['sha256'].items():
            if rel in source_files and source_files[rel]!=digest:raise ValueError('同包缓存元数据不一致')
            source_files[rel]=digest
        table=ep.table
        for key,values in [('episode_index',[new]*ep.length),('index',range(offset,offset+ep.length))]:
            if key not in table.column_names:raise ValueError('缺少LeRobot索引字段')
            table=table.set_column(table.schema.get_field_index(key),key,pa.array(values,type=table.schema.field(key).type))
        keep=[k for k in table.column_names if k not in ('episode_index','index')]
        if not table.select(keep).equals(ep.table.select(keep)):raise ValueError('物理数据发生变化')
        fields={'episode_chunk':new//chunk,'episode_index':new};rel=info['data_path'].format(**fields);p=destination/rel;p.parent.mkdir(parents=True,exist_ok=True);pq.write_table(table,p,compression='zstd')
        if ep.info['features']!=seed.info['features'] or ep.fps!=seed.fps:raise ValueError('同包机器人字段/视频规格不一致')
        per=stats_for(table)
        episode_metadata=metadata[server_source]
        original_stats={}
        if 'meta/episodes_stats.jsonl' in episode_metadata:
            for line in base64.b64decode(episode_metadata['meta/episodes_stats.jsonl']['data']).decode().splitlines():
                obj=json.loads(line)
                if obj['episode_index']==old:original_stats=obj.get('stats',{});break
        for camera,source in ep.cameras.items():
            target=destination/info['video_path'].format(**fields,video_key=camera);target.parent.mkdir(parents=True,exist_ok=True)
            subprocess.run(['cp','--reflink=auto','--',str(source),str(target)],check=True)
            if sha(target)!=sha(source):raise ValueError('视频副本SHA不一致')
            per[camera]=original_stats.get(camera) or image_stats(source)
            video_copies.append({'source':str(Path(server_source)/source.relative_to(ep.root)),'relative':str(target.relative_to(destination))})
        processing=ep.root/f'processing/episode_{old:06d}.json'
        if processing.exists():
            value=json.loads(processing.read_text());value['episode_index']=new
            if 'source_episode_index' in value:value['source_episode_index']=new
            atomic_json(destination/f'processing/episode_{new:06d}.json',value)
        entries.append({**ep.entry,'episode_index':new,'subtask_original_episode_index':ep.entry.get('subtask_original_episode_index',old)});statistics.append(per);offset+=ep.length
        mapping.append({'old_id':ep.id,'source_dataset':server_source,'source_episode_index':old,'episode_index':new,'source_recording_id':ep.entry['source_recording_id'],'source_sha256':identity['sha256']})
    info.update(total_episodes=len(rows),total_frames=offset,total_videos=len(rows)*len(seed.cameras),total_chunks=math.ceil(len(rows)/chunk),splits={'train':f'0:{len(rows)}'})
    info['subtask_partition']='quarantine' if Path(remote_destination).parent.name=='C' else 'retained'
    atomic_json(destination/'meta/info.json',info)
    (destination/'meta/episodes.jsonl').write_text(''.join(json.dumps(e,ensure_ascii=False)+'\n' for e in entries))
    shutil.copyfile(seed.root/'meta/tasks.jsonl',destination/'meta/tasks.jsonl')
    atomic_json(destination/'meta/stats.json',aggregate(statistics))
    (destination/'meta/episodes_stats.jsonl').write_text(''.join(json.dumps({'episode_index':i,'stats':s})+'\n' for i,s in enumerate(statistics)))
    modality=seed.root/'meta/modality.json'
    if modality.exists():shutil.copyfile(modality,destination/'meta/modality.json')
    for server_source in list(sources):
        for rel,data in metadata[server_source].items():sources[server_source][rel]=data['sha256']
    for rel,data in metadata[remote_path(ws.store.entries[seed.id])].items():
        if rel.startswith('processing/'):
            obj=json.loads(base64.b64decode(data['data']));obj.pop('validation',None)
            if 'episodes' in obj:obj['episodes']=len(rows)
            if 'output_frames' in obj:obj['output_frames']=offset
            if 'input_frames' in obj:obj['input_frames']=sum(json.loads((destination/f"processing/episode_{i:06d}.json").read_text()).get('source_length',entries[i]['length']) for i in range(len(rows)))
            obj['validation']={'episodes':len(rows),'frames':offset,'videos':len(rows)*len(seed.cameras),'indices_contiguous':True,'physical_columns_and_video_sha256_preserved':True}
            atomic_json(destination/rel,obj)
    atomic_json(destination/'meta/reorganization.json',{'operation':operation,'source_dataset':remote_path(ws.store.entries[seed.id]),'destination':remote_destination,'episodes':mapping,'review_status_preserved':True})
    return {'local':str(destination),'destination':remote_destination,'rows':rows,'mapping':mapping,'sources':sources,'videos':video_copies}


# Uses only the server standard library; Parquets were validated locally with PyArrow.
TRANSACTION=r'''
import json,hashlib,os,shutil,subprocess,fcntl
from pathlib import Path
body=json.load(__import__('sys').stdin);allowed=Path(body.get('allowed_root','/mnt/data/train_platform/datasets')).resolve();op=body['operation']
def h(p):
 d=hashlib.sha256()
 with p.open('rb') as f:
  for block in iter(lambda:f.read(8*1024*1024),b''):d.update(block)
 return d.hexdigest()
def safe(value):
 p=Path(value).resolve();assert p.is_relative_to(allowed) and p!=allowed;return p
journal=safe(body['journal']);journal.parent.mkdir(parents=True,exist_ok=True)
lock=open(journal.parent.parent/'organize.lock','a');fcntl.flock(lock,fcntl.LOCK_EX)
def save(state,**extra):
 p=journal.with_suffix('.tmp');p.write_text(json.dumps({'operation':op,'state':state,**extra}));os.replace(p,journal)
def source_check():
 for source,files in body['sources'].items():
  root=safe(source)
  for rel,digest in files.items():
   p=(root/rel).resolve();assert p.is_relative_to(root) and h(p)==digest, 'Source changed: '+str(p)
if body['action']=='status':
 print(journal.read_text() if journal.exists() else '{}');raise SystemExit()
if journal.exists() and json.loads(journal.read_text()).get('state')=='committed':
 print(journal.read_text());raise SystemExit()
if body['action']=='recover':
 if journal.exists() and json.loads(journal.read_text()).get('state')=='committing':
  for p in reversed(body['packages']):
   stage=safe(p['staging']);dest=safe(p['destination']);backup=safe(p['backup']) if p.get('backup') else None
   if backup is not None and backup.exists():
    if dest.exists():
     assert not stage.exists() and all(h(dest/rel)==digest for rel,digest in p['files'].items());dest.rename(stage)
    backup.rename(dest)
   elif not p['replace'] and dest.exists() and not stage.exists():
    assert all(h(dest/rel)==digest for rel,digest in p['files'].items());dest.rename(stage)
  save('rolled_back')
 print(journal.read_text() if journal.exists() else '{}');raise SystemExit()
if body['action']=='prepare':
 source_check()
 for p in body['packages']:
  stage=safe(p['staging']);dest=safe(p['destination'])
  assert not stage.exists(), 'Staging exists; retry needs recovery'
  if not p['replace']:assert not dest.exists(), 'C output already exists'
  stage.mkdir(parents=True)
  for video in p['videos']:
   source=safe(video['source']);target=stage/video['relative'];assert target.resolve().is_relative_to(stage);target.parent.mkdir(parents=True,exist_ok=True)
   subprocess.run(['cp','--reflink=auto','--',str(source),str(target)],check=True)
 save('prepared');print(json.dumps({'state':'prepared'}));raise SystemExit()
assert body['action']=='commit'
source_check()
for p in body['packages']:
 stage=safe(p['staging'])
 actual={str(q.relative_to(stage)) for q in stage.rglob('*') if q.is_file()}
 assert actual==set(p['files']), 'Unexpected/missing staged files'
 for rel,digest in p['files'].items():assert h(stage/rel)==digest, 'Staged checksum mismatch'
save('committing');swaps=[];published=[]
try:
 for i,p in enumerate(body['packages']):
  stage=safe(p['staging']);dest=safe(p['destination']);dest.parent.mkdir(parents=True,exist_ok=True)
  if p['replace']:
   backup=safe(p['backup']);assert not backup.exists();backup.parent.mkdir(parents=True,exist_ok=True)
   dest.rename(backup);swaps.append((dest,backup))
  else:assert not dest.exists()
  stage.rename(dest);published.append((stage,dest))
  if body.get('test_fail_after')==i+1:raise RuntimeError('Injected rollback test')
 save('committed',packages=[{'destination':p['destination'],'backup':p.get('backup')} for p in body['packages']]);print(journal.read_text())
except BaseException:
 for stage,dest in reversed(published):
  if dest.exists():dest.rename(stage)
 for dest,backup in reversed(swaps):
  if backup.exists():backup.rename(dest)
 save('rolled_back');raise
'''


def install(ws,state):
    """Install identities only after both remote versions committed; safe to repeat after restart."""
    from .quarantine import read
    with ws.lock,ws.store.lock:
        reg=registry(ws)
        if state['operation'] in reg['operations']:return state['result']
        packages=state['packages'];snapshots=state['snapshots'];mapping={};episodes={}
        for pack in packages:
            path=Path(pack['local'])
            if path not in ws.store.roots:ws.store.roots.append(path)
            for row in pack['rows']:
                ep=Episode(path,row['new_episode_index']);mapping[row['id']]=ep.id;episodes[ep.id]=ep
        ws.store.scan()
        # Rewrite cross-version projection identities while preserving annotation boundaries/review flags.
        for pack in packages:
            for row in pack['rows']:
                old=row['id'];new=mapping[old];ep=episodes[new];snap=snapshots[old];identity=ep.identity()
                legacy=copy.deepcopy(snap['annotation']);legacy['source']=identity
                legacy['provenance']={**legacy.get('provenance',{}),'reorganization':{'old_id':old,'source':snap['annotation']['source'],'operation':state['operation']}}
                atomic_json(ws.store.path(new),legacy)
                for stage,doc in snap['stages'].items():
                    if not doc['saved_at']:continue
                    updated=copy.deepcopy(doc);updated['source']=identity
                    p=updated.get('projection')
                    if p:
                        p['normal_id']=mapping.get(p.get('normal_id'),p.get('normal_id'))
                        if p.get('normal_id') in episodes:p['normal_source']=episodes[p['normal_id']].identity()
                        mapping_file=ep.root/f'processing/episode_{ep.index:06d}.json'
                        if mapping_file.exists():p['mapping_sha256']=sha(mapping_file)
                    atomic_json(ws.stage_path(new,stage),updated)
                for stage,doc in snap['stages'].items():
                    file=ep.root/f'annotations/subtasks/{stage}/episode_{ep.index:06d}.json'
                    if not doc['saved_at'] or not file.exists():continue
                    record={'kind':'json','host':'28','stage':stage,'revision':doc['revision'],'saved_at':time.time(),'sha256':sha(file),'path':str(Path(pack['destination'])/file.relative_to(ep.root)),'episode_id':new,'reorganization':state['operation']}
                    atomic_json(ws.root/'deliveries'/new/(stage+'.json'),record)
                reg['retired'][old]=new
                reg['entries'][new]={'directory_override':pack['review_directory'],'archived':pack['archived'],'original_episode_index':reg['entries'].get(old,{}).get('original_episode_index',row['source_episode_index']),'operation':state['operation']}
                ws.queue[new]={'state':'ready' if all(r['label']!='unknown' for r in legacy['segments']) else 'pending'}
        ledger=read(ws)
        for old,new in mapping.items():
            if old in ledger['entries']:ledger['entries'][new]=copy.deepcopy(ledger['entries'][old])
        atomic_json(ws.root/'quarantine.json',ledger)
        batch=ws.root/'batch.json'
        if batch.exists():
            data=json.loads(batch.read_text());data['reference_ids']=[mapping.get(i,i) for i in data.get('reference_ids',[])];atomic_json(batch,data)
        for old in reg['retired']:
            new=reg['retired'][old];seen={old}
            while new in reg['retired'] and new not in seen:
                seen.add(new);new=reg['retired'][new]
            reg['retired'][old]=new
        reg['roots']=list(dict.fromkeys(reg.get('roots',[])+[p['local'] for p in packages]));reg['operations'].append(state['operation'])
        atomic_json(ws.root/'organized.json',reg);ws._checkpoint()
        state['state']='completed';atomic_json(Path(state['state_path']),state)
        return state['result']


def recover(ws):
    reg=registry(ws)
    for path in reg.get('roots',[]):
        if Path(path) not in ws.store.roots:ws.store.roots.append(Path(path))
    if reg.get('roots'):ws.store.scan()
    for file in sorted((ws.root/'organizations').glob('*/state.json')):
        state=json.loads(file.read_text())
        if state['state']=='remote_committed':install(ws,state)
        elif state['state']=='ready_commit':
            result=remote(TRANSACTION,{**state['body'],'action':'status'})
            if result.get('state')=='committed':install(ws,state)
            elif result.get('state')=='committing':
                remote(TRANSACTION,{**state['body'],'action':'recover'})
                state['state']='rolled_back';atomic_json(file,state)


def execute(ws,id_,plan_hash):
    from .quarantine import plan
    with ws.lock,ws.store.lock:
        preview=plan(ws,id_)
        if preview['plan_hash']!=plan_hash:raise ValueError('整理范围或标注已变化，请重新预览')
        if not preview['ready']:raise ValueError('尚不能整理：'+'；'.join(preview['blockers']))
        if any(not pack['included'] for pack in preview['packages']):raise ValueError('本包没有保留数据，不能生成训练候选包')
        ids=[r['id'] for p in preview['packages'] for r in p['included']+p['rejected']]
        if getattr(ws,'organizing_ids',set()) & set(ids):raise ValueError('本包正在整理')
        ws.organizing_ids.update(ids)
        snapshots={i:{'annotation':ws.store.load(ws.store.episode(i)),'stages':ws.stages(ws.store.episode(i))} for i in ids}
    operation=time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]
    work=ws.root/'organizations'/operation;work.mkdir(parents=True);state_path=work/'state.json';state={'state':'preparing','operation':operation,'state_path':str(state_path),'snapshots':snapshots,'packages':[]}
    atomic_json(state_path,state)
    try:
        metadata=remote(READ_META,{'sources':sorted({remote_path(ws.store.entries[i]) for i in ids})})
        packages=[];source_hashes={};body_packages=[]
        for pack in preview['packages']:
            source=Path(pack['remote_source']);server_work=source.parent.parent/'.subtask_organize'/operation
            for role,rows in [('kept',pack['included']),('rejected',pack['rejected'])]:
                if not rows:continue
                destination=pack['remote_source'] if role=='kept' else pack['c_target']
                local=work/'cache'/(pack['variant']+'-'+role)
                built=build_package(ws,rows,local,destination,operation,metadata)
                built.update(archived=role=='rejected',review_directory=pack['remote_source'])
                for root,files in built['sources'].items():
                    target=source_hashes.setdefault(root,{})
                    for rel,digest in files.items():
                        if rel in target and target[rel]!=digest:raise ValueError('缓存源SHA不一致')
                        target[rel]=digest
                packages.append(built)
                body_packages.append({'destination':destination,'staging':str(server_work/role),'replace':role=='kept' or any(remote_path(ws.store.entries[r['id']])==destination for r in rows),'backup':str(source.parent.parent/'.subtask_backups'/operation/(source.name if role=='kept' else 'C_'+Path(destination).name)),'videos':built['videos']})
        # Add complete verified cache receipts before producing the public annotation JSONs.
        identities={};new_ids={}
        for pack in packages:
            local=Path(pack['local']);files={str(p.relative_to(local)):{'sha256':sha(p),'size':p.stat().st_size} for p in local.rglob('*') if p.is_file()}
            atomic_json(local/'cache-source.json',{'host':'28','manifest':{'dataset':pack['destination'],'files':files},'all_selected_sha256_verified':True,'reorganization':operation})
            for row in pack['rows']:
                ep=Episode(local,row['new_episode_index']);identities[row['id']]=ep.identity();new_ids[row['id']]=ep.id
        for pack,remote_pack in zip(packages,body_packages):
            local=Path(pack['local'])
            for row in pack['rows']:
                old=row['id'];identity=identities[old]
                for stage,doc in snapshots[old]['stages'].items():
                    if not doc['saved_at']:continue
                    doc=copy.deepcopy(doc);doc['source']=identity
                    if 'projection' in doc:
                        p=doc['projection'];old_normal=p.get('normal_id');p['normal_id']=new_ids.get(old_normal,old_normal)
                        if old_normal in identities:p['normal_source']=identities[old_normal]
                        mp=local/f"processing/episode_{row['new_episode_index']:06d}.json"
                        if mp.exists():p['mapping_sha256']=sha(mp)
                    atomic_json(local/f"annotations/subtasks/{stage}/episode_{row['new_episode_index']:06d}.json",english_document(doc))
            files={str(p.relative_to(local)):sha(p) for p in local.rglob('*') if p.is_file() and p.name!='cache-source.json'}
            remote_pack['files']=files
        journal=str(Path(preview['packages'][0]['remote_source']).parent.parent/'.subtask_organize'/operation/'transaction.json')
        body={'operation':operation,'journal':journal,'sources':source_hashes,'packages':body_packages}
        state.update(packages=packages,body=body)
        atomic_json(state_path,state)
        remote(TRANSACTION,{**body,'action':'prepare'})
        for pack,remote_pack in zip(packages,body_packages):
            command=['rsync','-rt','--protect-args','--exclude=videos/','--exclude=cache-source.json','-e','ssh -o BatchMode=yes -o ConnectTimeout=10',pack['local']+'/','28:'+remote_pack['staging']+'/']
            r=subprocess.run(command,text=True,capture_output=True,timeout=3600)
            if r.returncode:raise ValueError('整理上传失败：'+r.stderr[-500:])
        with ws.lock,ws.store.lock:
            for i,snap in snapshots.items():
                ep=ws.store.episode(i)
                if ws.stages(ep)!=snap['stages'] or ws.store.load(ep)!=snap['annotation']:raise ValueError('整理期间标注发生变化，未替换数据包')
            selected=new_ids[id_];state['result']={'operation':operation,'selected_id':selected,'packages':[{'variant':p['variant'],'backup':str(Path(p['remote_source']).parent.parent/'.subtask_backups'/operation/Path(p['remote_source']).name),'source':p['remote_source'],'kept':len(p['included']),'rejected':len(p['rejected']),'c_target':p['c_target'],'mapping':p['included']} for p in preview['packages']],'review_warnings':preview['review_warnings']}
            state['state']='ready_commit';atomic_json(state_path,state)
            remote(TRANSACTION,{**body,'action':'commit'})
            state['state']='remote_committed';atomic_json(state_path,state)
            result=install(ws,state)
        return result
    finally:
        with ws.lock:ws.organizing_ids.difference_update(ids)
