"""Explicit SSH read-only inventory and hash-verified selected-episode caching."""
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile

from .core import atomic_json, sha

REMOTE = r'''
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1]).resolve(strict=True)
info=json.loads((root/'meta/info.json').read_text())
if info.get('codebase_version')!='v2.1':raise ValueError('LeRobot v2.1 required')
entries=[json.loads(s) for s in (root/'meta/episodes.jsonl').read_text().splitlines() if s.strip()]
if sys.argv[2]=='inventory':
 print(json.dumps({'dataset':str(root),'info':info,'episodes':entries}));sys.exit()
episodes=json.loads(sys.argv[2]);files=set(['meta/info.json','meta/episodes.jsonl','meta/tasks.jsonl'])
for ep in episodes:
 if type(ep)!=int or ep<0 or sum(e['episode_index']==ep for e in entries)!=1:raise ValueError('Invalid episode')
 fields={'episode_chunk':ep//info['chunks_size'],'episode_index':ep}
 files.add(info['data_path'].format(**fields))
 for k,f in info['features'].items():
  if f['dtype']=='video':files.add(info['video_path'].format(**fields,video_key=k))
 for name in [f'processing/episode_{ep:06d}.json','processing/normal.json','processing/summary.json']:
  if (root/name).is_file():files.add(name)
manifest={}
for name in sorted(files):
 path=(root/name).resolve(strict=True)
 if not path.is_relative_to(root) or name.startswith('/') or '\n' in name or '\r' in name or '..' in pathlib.PurePosixPath(name).parts:raise ValueError('Unsafe dataset path')
 before=path.stat();h=hashlib.sha256()
 with path.open('rb') as file:
  for block in iter(lambda:file.read(8*1024*1024),b''):h.update(block)
 after=path.stat()
 if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('Source changed while hashing')
 manifest[name]={'sha256':h.hexdigest(),'size':after.st_size,'mtime_ns':after.st_mtime_ns}
print(json.dumps({'dataset':str(root),'episodes':episodes,'files':manifest}))
'''


def query(host, dataset, episodes='inventory'):
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@-]*', host):
        raise ValueError('SSH host 必须是已配置别名或 user@hostname')
    if not dataset.startswith('/') or '\n' in dataset or '\r' in dataset:
        raise ValueError('请指定服务器上的绝对数据集路径')
    arg = episodes if isinstance(episodes, str) else json.dumps(episodes)
    command = shlex.join(['python3', '-c', REMOTE, dataset, arg])
    process = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', host, command],
                             capture_output=True, text=True, check=True, timeout=300)
    return json.loads(process.stdout)


def fetch(host, dataset, episodes, output):
    output = Path(output).expanduser().resolve()
    before = query(host, dataset, episodes)
    if output.exists():
        receipt = output/'cache-source.json'
        if not receipt.exists() or json.loads(receipt.read_text()).get('manifest') != before:
            raise FileExistsError('缓存目录已存在且不匹配；请选择新目录，不覆盖已有内容')
        for name, item in before['files'].items():
            if sha(output/name) != item['sha256']: raise ValueError('已有缓存内容变化')
        return {'status': 'already_verified', 'output': str(output)}
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.'+output.name+'.partial-', dir=output.parent))
    try:
        listing = stage/'.selected-files'
        listing.write_text('\n'.join(before['files'])+'\n')
        subprocess.run(['rsync', '-rt', '--protect-args', '--timeout=120', '--files-from='+str(listing),
                        '-e', 'ssh -o BatchMode=yes -o ConnectTimeout=10', host+':'+before['dataset']+'/', str(stage)+'/'],
                       check=True, timeout=3600)
        listing.unlink()
        for name, item in before['files'].items():
            if (stage/name).stat().st_size != item['size'] or sha(stage/name) != item['sha256']:
                raise ValueError(f'下载内容校验失败: {name}')
        after = query(host, dataset, episodes)
        if before != after: raise ValueError('远端在缓存期间变化，请稍后重新下载')
        atomic_json(stage/'cache-source.json', {'schema_version': 1, 'host': host, 'manifest': before,
                                                'remote_read_only': True, 'all_selected_sha256_verified': True})
        # Never replace an existing cache directory, including one created by a concurrent fetch.
        if output.exists(): raise FileExistsError(output)
        stage.rename(output)
        return {'status': 'verified', 'output': str(output), 'files': len(before['files']),
                'bytes': sum(v['size'] for v in before['files'].values())}
    finally:
        if stage.exists(): shutil.rmtree(stage)

DATA_ROOT = '/mnt/data/train_platform/datasets'


def dataset_path(value):
    """The web importer reads only the configured dataset tree on SSH alias 28."""
    from urllib.parse import urlsplit
    if not isinstance(value,str):raise ValueError('请输入28服务器目录')
    if value.startswith('sftp://'):
        url=urlsplit(value)
        if url.netloc!='28' or url.query or url.fragment:raise ValueError('仅支持28服务器的数据目录')
        value=url.path
    path=Path(value)
    if not path.is_absolute() or '..' in path.parts or any(c in value for c in '\n\r\x00'):
        raise ValueError('请输入数据根目录内的绝对路径')
    if not path.is_relative_to(DATA_ROOT):raise ValueError('仅支持 /mnt/data/train_platform/datasets 下的目录')
    return str(path)


def browse(dataset=DATA_ROOT):
    dataset=dataset_path(dataset)
    script=r'''
import json,pathlib,sys
base=pathlib.Path(sys.argv[1]).resolve(strict=True);p=pathlib.Path(sys.argv[2]).resolve(strict=True)
if not p.is_relative_to(base) or not p.is_dir():raise ValueError('Directory outside dataset tree')
children=[]
for child in sorted(p.iterdir(),key=lambda x:x.name):
 if child.name.startswith('.'):continue
 try:
  resolved=child.resolve(strict=True)
  if child.is_dir() and resolved.is_relative_to(base):children.append({'name':child.name,'path':str(resolved),'is_dataset':(resolved/'meta/info.json').is_file()})
 except OSError:continue
if len(children)>1000:raise ValueError('Too many subdirectories; enter a more specific path')
print(json.dumps({'path':str(p),'parent':str(p.parent) if p!=base else None,'is_dataset':(p/'meta/info.json').is_file(),'directories':children}))
'''
    result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','28',
        shlex.join(['python3','-c',script,DATA_ROOT,dataset])],capture_output=True,text=True,timeout=60)
    if result.returncode:raise ValueError('无法读取28目录，请检查目录是否存在及SSH连接')
    return json.loads(result.stdout)


def web_inventory(value):
    """Resolve inside the allowed tree before reading dataset metadata over SSH."""
    selected=browse(dataset_path(value))
    if not selected['is_dataset']:raise ValueError('请继续打开到一个LeRobot数据目录')
    try:data=query('28',selected['path'])
    except (subprocess.SubprocessError,OSError,ValueError):raise ValueError('读取28数据集失败，请检查目录及SSH连接') from None
    if dataset_path(data['dataset'])!=selected['path']:raise ValueError('远端目录在读取期间发生变化')
    return data


def discover(value=DATA_ROOT):
    """Bounded metadata-only package index; never downloads videos."""
    path=dataset_path(value)
    script=r'''
import json,pathlib,sys
base=pathlib.Path(sys.argv[1]).resolve(strict=True);root=pathlib.Path(sys.argv[2]).resolve(strict=True)
if not root.is_relative_to(base):raise ValueError('Outside dataset tree')
stack=[root];seen=set();items=[]
while stack:
 p=stack.pop().resolve(strict=True)
 if p in seen or not p.is_relative_to(base):continue
 seen.add(p)
 if len(seen)>12000:raise ValueError('目录太大，请选择机器人或批次子目录')
 if (p/'meta/info.json').is_file():
  info=json.loads((p/'meta/info.json').read_text())
  if info.get('codebase_version')!='v2.1':continue
  eps=[json.loads(x) for x in (p/'meta/episodes.jsonl').read_text().splitlines() if x.strip()]
  items.append({'path':str(p),'fps':info['fps'],'episodes':[{k:(str(e[k]) if k in ('source_start_ns','source_end_ns') and e.get(k) is not None else e.get(k)) for k in ('episode_index','length','source_recording_id','source_start_ns','source_end_ns')} for e in eps]})
  if len(items)>1000:raise ValueError('数据包过多，请选择更小目录')
 else:
  stack.extend(c for c in p.iterdir() if c.is_dir() and not c.name.startswith('.') and c.name not in {'annotations','subtask_training'})
print(json.dumps({'root':str(root),'packages':sorted(items,key=lambda x:x['path'])}))
'''
    result=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=10','28',shlex.join(['python3','-c',script,DATA_ROOT,path])],capture_output=True,text=True,timeout=120)
    if result.returncode:raise ValueError('扫描28数据包失败，请缩小到机器人或批次目录')
    return json.loads(result.stdout)
