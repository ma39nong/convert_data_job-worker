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
