"""Explicit per-stage JSON publication and single-episode training copies.

Never rewrites source Parquet, tasks or media. JSON exports update a fixed output file.
"""
import base64
import copy
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .core import atomic_json
from .remote import DATA_ROOT,dataset_path

from .vocabulary import DEFAULT_TASK_PROMPT, PROMPT_PROFILE, STAGE_PROMPTS, stage_prompt, english_document


def original_tasks(ep):
    if ep.entry.get('tasks'):return ep.entry['tasks']
    tasks={row['task_index']:row['task'] for line in (ep.root/'meta/tasks.jsonl').read_text().splitlines() if line.strip() for row in [json.loads(line)]}
    return list(dict.fromkeys(tasks[int(i)] for i in ep.table['task_index'].to_pylist()))


# Same standalone writer is exercised locally in tests and remotely on alias 28.
WRITER=r'''
import base64,hashlib,json,os,pathlib,shutil,subprocess,sys,tempfile
body=json.load(sys.stdin);base=pathlib.Path(sys.argv[1]).resolve(strict=True)
source=pathlib.Path(body['source']).resolve(strict=True);dest=pathlib.Path(body['destination']).resolve()
if not source.is_relative_to(base) or not dest.is_relative_to(base):raise ValueError('Destination outside allowed root')
if dest==source or dest in source.parents:raise ValueError('Invalid destination')
for rel,digest in body['source_sha256'].items():
 p=(source/rel).resolve(strict=True)
 if pathlib.PurePosixPath(rel).is_absolute() or '..' in pathlib.PurePosixPath(rel).parts or not p.is_relative_to(source):raise ValueError('Invalid source path')
 h=hashlib.sha256()
 with p.open('rb') as f:
  for block in iter(lambda:f.read(8*1024*1024),b''):h.update(block)
 if h.hexdigest()!=digest:raise ValueError('Source changed: '+rel)
def digest_file(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for block in iter(lambda:f.read(8*1024*1024),b''):h.update(block)
 return h.hexdigest()
def canonical(obj):return json.dumps(obj,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()
def publish_file(path,data):
 path.parent.mkdir(parents=True,exist_ok=True)
 if not path.parent.resolve().is_relative_to(base):raise ValueError('Unsafe output symlink')
 if path.exists():
  if path.read_bytes()==data:return
  old=json.loads(path.read_bytes());new=json.loads(data)
  if old.get('stage')!=new.get('stage') or any(old.get('source',{}).get(k)!=new.get('source',{}).get(k) for k in ('episode_index','source_recording_id')):raise ValueError('Existing output belongs to another source')
 fd,name=tempfile.mkstemp(prefix='.subtask-',dir=path.parent)
 try:
  with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
  os.replace(name,path)
 finally:
  if os.path.exists(name):os.unlink(name)
if body['kind']=='json':
 data=canonical(body['document']);publish_file(dest,data)
 result={'path':str(dest),'sha256':hashlib.sha256(data).hexdigest(),'kind':'json'}
else:
 if dest.is_relative_to(source/'data') or dest.is_relative_to(source/'meta') or dest.is_relative_to(source/'videos'):raise ValueError('Cannot write source data tree')
 if dest.exists():
  receipt=json.loads((dest/'subtask_export.json').read_text())
  if receipt['export_digest']!=body['export_digest']:raise ValueError('Existing training copy differs')
  for rel,digest in receipt['files'].items():
   if digest_file(dest/rel)!=digest:raise ValueError('Training copy changed')
  result={'path':str(dest),'kind':'training','sha256':body['export_digest']}
 else:
  dest.parent.mkdir(parents=True,exist_ok=True)
  stage=pathlib.Path(tempfile.mkdtemp(prefix='.subtask-training-',dir=dest.parent))
  try:
   for rel,value in body['files'].items():
    p=stage/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(base64.b64decode(value))
   for rel,target in body['videos'].items():
    p=(source/rel).resolve(strict=True);q=stage/target;q.parent.mkdir(parents=True,exist_ok=True)
    subprocess.run(['cp','--reflink=auto','--',str(p),str(q)],check=True,stdout=subprocess.DEVNULL)
   # Sensor image statistics remain valid: no source frames are removed.
   stats=json.loads((stage/'meta/stats.json').read_text())
   old=source/'meta/episodes_stats.jsonl'
   if old.is_file():
    for line in old.read_text().splitlines():
     row=json.loads(line)
     if row['episode_index']==body['source_episode']:
      for key,value in row.get('stats',{}).items():
       if key in body['cameras']:stats[key]=value
      break
   (stage/'meta/stats.json').write_bytes(canonical(stats))
   (stage/'meta/episodes_stats.jsonl').write_bytes(canonical({'episode_index':0,'stats':stats})+b'\n')
   if (source/'meta/modality.json').is_file():shutil.copy2(source/'meta/modality.json',stage/'meta/modality.json')
   hashes={str(p.relative_to(stage)):digest_file(p) for p in stage.rglob('*') if p.is_file()}
   (stage/'subtask_export.json').write_bytes(canonical({'export_digest':body['export_digest'],'source':str(source),'source_episode_index':body['source_episode'],'stage':body['stage'],'files':hashes}))
   if dest.exists():raise ValueError('Destination appeared during export')
   stage.rename(dest)
  finally:
   if stage.exists():shutil.rmtree(stage)
  result={'path':str(dest),'kind':'training','sha256':body['export_digest']}
print(json.dumps(result))
'''


def locations(ws,ep):
    doc=ws.store.load(ep);source=doc['source'];remote=source.get('remote_source')
    records={}
    for stage in ('coarse','fine'):
        path=ws.root/'deliveries'/ep.id/(stage+'.json')
        records[stage]=json.loads(path.read_text()) if path.exists() else None
    return {'workspace':str(ws.root/'stages'/ep.id),'source_dataset':remote['dataset'] if remote else str(ep.root),
            'host':remote['host'] if remote else 'local','records':records,'original_tasks':original_tasks(ep),
            'default_json_root':(remote['dataset'] if remote else str(ep.root))+'/annotations/subtasks',
            'default_training_root':(remote['dataset'] if remote else str(ws.store.output/'training-copies'))+'/subtask_training'}


def training_files(ep,doc,*,prompt_mode='goal',task_prompt=DEFAULT_TASK_PROMPT):
    rows=doc['segments']
    if any(r['label']=='unknown' or not r.get('reviewed') for r in rows):raise ValueError('生成训练副本前，请完成人工确认：这一层全部分段且不含待标注')
    ends=np.asarray([r['end_frame'] for r in rows]);ids=np.searchsorted(ends,np.arange(ep.length),side='right')
    if prompt_mode not in {'goal','stage'} or not isinstance(task_prompt,str) or not task_prompt.strip() or len(task_prompt)>1000:raise ValueError('请选择有效任务prompt，最长1000字')
    stage_prompts=[stage_prompt(doc['stage'],rows,i) for i in range(len(rows))]
    prompts=[task_prompt.strip()]*len(rows) if prompt_mode=='goal' else stage_prompts
    texts=list(dict.fromkeys(prompts));lookup={text:i for i,text in enumerate(texts)}
    table=ep.table
    updates={'episode_index':np.zeros(ep.length,dtype=np.int64),'index':np.arange(ep.length,dtype=np.int64),
        'task_index':np.asarray([lookup[prompts[int(i)]] for i in ids],dtype=np.int64),doc['stage']+'_stage_index':ids+1}
    for key,val in updates.items():
        array=pa.array(val,type=table.schema.field(key).type if key in table.column_names else pa.int64())
        table=table.set_column(table.schema.get_field_index(key),key,array) if key in table.column_names else table.append_column(key,array)
    sink=pa.BufferOutputStream();pq.write_table(table,sink,compression='zstd')
    info=copy.deepcopy(ep.info);info.update(total_episodes=1,total_frames=ep.length,total_tasks=len(texts),total_videos=len(ep.cameras),total_chunks=1,splits={'train':'0:1'})
    info['data_path']='data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet'
    info['video_path']='videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4'
    info['features'][doc['stage']+'_stage_index']={'dtype':'int64','shape':[1],'names':None}
    entry={**ep.entry,'episode_index':0,'length':ep.length,'tasks':texts};entry.pop('task_index',None)
    stats={}
    for key in table.column_names:
        try:a=np.asarray(table[key].to_pylist(),dtype=np.float64)
        except (TypeError,ValueError):continue
        if a.ndim==1:a=a[:,None]
        if a.ndim!=2 or not np.isfinite(a).all():continue
        stats[key]={name:value.tolist() for name,value in [('min',a.min(0)),('max',a.max(0)),('mean',a.mean(0)),('std',a.std(0))]};stats[key]['count']=[len(a)]
    raw={'data/chunk-000/episode_000000.parquet':sink.getvalue().to_pybytes()}
    documents={'meta/info.json':info,'meta/episodes.jsonl':entry,'meta/stats.json':stats,'stage_annotations.json':english_document(doc),
        'meta/subtask_prompts.json':{'profile':PROMPT_PROFILE,'prompt_mode':prompt_mode,'task_prompt':task_prompt.strip(),'original_task_prompts':original_tasks(ep),'training_task_field':'task_index -> meta/tasks.jsonl','stage':doc['stage'],'stages':[{'label':r['label'],'task_en':prompt} for r,prompt in zip(rows,stage_prompts)]}}
    for name,obj in documents.items():raw[name]=(json.dumps(obj,ensure_ascii=False,allow_nan=False)+'\n').encode()
    raw['meta/tasks.jsonl']=''.join(json.dumps({'task_index':i,'task':text},ensure_ascii=False)+'\n' for i,text in enumerate(texts)).encode()
    videos={str(p.relative_to(ep.root)):f'videos/chunk-000/{camera}/episode_000000.mp4' for camera,p in ep.cameras.items()}
    return {key:base64.b64encode(value).decode() for key,value in raw.items()},videos


def publish(ws,ep,stage,*,target='dataset',root=None,kind='json',revision=None,prompt_mode='goal',task_prompt=DEFAULT_TASK_PROMPT):
    if stage not in {'coarse','fine'} or target not in {'dataset','remote','local'} or kind not in {'json','training'}:raise ValueError('无效导出选项')
    with ws.lock,ws.store.lock:
        if kind=='training':
            from .quarantine import assert_included
            assert_included(ws,ep.id)
        doc=ws.stages(ep)[stage]
        if not doc['saved_at'] or doc['revision']!=revision:raise ValueError('请先保存当前阶段，并刷新导出版本')
        if doc['source']!=ep.identity():raise ValueError('来源已变化，拒绝导出')
        doc=copy.deepcopy(doc)
    loc=locations(ws,ep);remote=target in {'dataset','remote'}
    if remote and loc['host']!='28':raise ValueError('这个片段没有28服务器来源，不能发布到服务器')
    selected=loc['default_json_root' if kind=='json' else 'default_training_root'] if target=='dataset' else root
    if not isinstance(selected,str) or not selected.strip():raise ValueError('请选择导出绝对路径')
    if remote:selected=dataset_path(selected)
    else:
        p=Path(selected).expanduser()
        if not p.is_absolute() or '..' in p.parts:raise ValueError('本机导出目录必须是绝对路径')
        p=p.resolve()
        if any(p==r or p.is_relative_to(r) for r in ws.store.roots):raise ValueError('本机导出不能覆盖输入数据目录')
        selected=str(p)
    if kind=='json':doc=english_document(doc)
    digest=hashlib.sha256(json.dumps(doc,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    export_digest=hashlib.sha256(json.dumps([digest,PROMPT_PROFILE,prompt_mode,task_prompt],ensure_ascii=False).encode()).hexdigest() if kind=='training' else digest
    name=f'episode_{ep.index:06d}'
    if kind=='training':name+=f'_r{doc["revision"]}_'+export_digest[:16]
    # Custom roots retain source-package identity, avoiding collisions across batches.
    if target!='dataset':selected=str(Path(selected)/(Path(loc['source_dataset']).name+'_'+hashlib.sha256(loc['source_dataset'].encode()).hexdigest()[:8]))
    dest=str(Path(selected)/stage/(name+'.json' if kind=='json' else name))
    source=loc['source_dataset'] if remote else str(ep.root)
    body={'kind':kind,'source':source,'destination':dest,'source_sha256':doc['source']['sha256'],'document':doc}
    if kind=='training':
        files,videos=training_files(ep,doc,prompt_mode=prompt_mode,task_prompt=task_prompt)
        body.update(files=files,videos=videos,export_digest=export_digest,source_episode=ep.index,stage=stage,cameras=list(ep.cameras))
    command=['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','28',shlex.join(['python3','-c',WRITER,DATA_ROOT])] if remote else [sys.executable,'-c',WRITER,str(Path(selected).anchor)]
    result=subprocess.run(command,input=json.dumps(body,ensure_ascii=False),capture_output=True,text=True,timeout=3600)
    if result.returncode:raise ValueError('导出失败，未覆盖原数据：'+result.stderr[-700:])
    record={**json.loads(result.stdout),'host':'28' if remote else 'local','stage':stage,'revision':doc['revision'],'saved_at':time.time(),'document_sha256':digest,'episode_id':ep.id}
    atomic_json(ws.root/'deliveries'/ep.id/(stage+'.json'),record)
    atomic_json(ws.root/'deliveries'/ep.id/'history'/f'{time.time_ns()}.json',record)
    return record
