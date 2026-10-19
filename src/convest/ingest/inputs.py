"""Observed stability and read-only structural checks; no uploader cooperation."""
from contextlib import contextmanager
from pathlib import Path
import os
import hashlib
from copy import deepcopy
import shutil
import threading
import time
from convest.config import digest
from convest.jobs import check_ready,checksum
from convest.readiness import ValidatedInput,validation_context,file_snapshot
from convest.sources.gello_rosbag2 import discover
import yaml


class InputChanged(RuntimeError):pass


def snapshot(root):
    root=Path(root)
    if root.is_symlink() or not root.is_dir():raise InputChanged('Input directory missing or symlink')
    rows=[]
    for path in [root,*sorted(root.rglob('*'))]:
        if path.is_symlink():raise InputChanged('Symlink in input')
        st=path.stat()
        if not (path.is_file() or path.is_dir()):raise InputChanged('Special file in input')
        rows.append({'path':'.' if path==root else path.relative_to(root).as_posix(),
            'directory':path.is_dir(),'size':st.st_size if path.is_file() else 0,
            'mtime_ns':st.st_mtime_ns,'ctime_ns':st.st_ctime_ns,'inode':st.st_ino,'device':st.st_dev})
    return rows


def require_same(root,expected):
    try:current=snapshot(root)
    except (OSError,ValueError) as exc:raise InputChanged(str(exc)) from exc
    if current!=expected:raise InputChanged('Input changed; discard attempt and observe stability again')


def check_input(bag,route,expected,progress=None):
    bag=Path(bag);require_same(bag,expected)
    cfg=route['conversion'];contract=yaml.safe_load(Path(cfg['contract']).read_text());profile={}
    context=validation_context(cfg)
    ready=check_ready(bag,contract,cfg['schema_dir'],profile,progress)
    if context!=validation_context(cfg):raise InputChanged('Validation definitions changed during readiness check')
    item=discover(bag)[0]
    if not item['eligible']:raise ValueError(item['reason'])
    known={f['name']:f['sha256'] for f in ready['volumes']}
    known.update({'metadata.yaml':ready['metadata_sha256'],'collection_state.json':ready['collection_state_sha256']})
    files=[{'path':r['path'],'size':r['size'],'sha256':known.get(r['path']) or checksum(bag/r['path'])}
           for r in expected if not r['directory']]
    require_same(bag,expected)
    identity=digest({'robot':route['robot'],'group':route['group'],'episode':bag.name,'files':files})
    return {'content_identity':identity,'recording_id':item['source_recording_id'],'input':str(bag),'snapshot':expected,'files':files,
            'validation_context':context,
            'readiness':ready,'input_profile':profile,'readiness_basis':'configured input directory discovery; observed stability and structural validation; no uploader protocol dependency'}


def stage_input(bag,target,proof,progress=None):
    """Freeze a hash-verified working copy. Originals are opened O_NOFOLLOW/read-only."""
    bag=Path(bag);target=Path(target);require_same(bag,proof['snapshot']);target.mkdir(parents=True)
    rootfd=os.open(bag,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        signatures={r['path']:r for r in proof['snapshot']}
        for item in proof['files']:
            parts=Path(item['path']).parts;fds=[];parent=rootfd
            try:
                for part in parts[:-1]:
                    fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent);fds.append(fd);parent=fd
                fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
                with os.fdopen(fd,'rb') as source:
                    st=os.fstat(source.fileno());expected=signatures[item['path']]
                    if (st.st_ino,st.st_dev,st.st_size,st.st_mtime_ns,st.st_ctime_ns)!=(expected['inode'],expected['device'],expected['size'],expected['mtime_ns'],expected['ctime_ns']):
                        raise InputChanged('Input file changed while staging')
                    out=target/item['path'];out.parent.mkdir(parents=True,exist_ok=True)
                    class HashingReader:
                        def __init__(self):self.digest=hashlib.sha256();self.done=0;self.last=time.monotonic()
                        def read(self,size):
                            data=source.read(size);self.digest.update(data);self.done+=len(data)
                            if progress and (not data or time.monotonic()-self.last>=5):
                                progress('copying',file=item['path'],bytes_done=self.done,bytes_total=item['size']);self.last=time.monotonic()
                            return data
                    reader=HashingReader()
                    with out.open('xb') as dest:shutil.copyfileobj(reader,dest,8*1024*1024)
                    if reader.digest.hexdigest()!=item['sha256']:raise InputChanged('Source bytes differ during staging')
                # Keep the independent destination read: a write/copy error must
                # not be hidden by hashing only the source stream.
                if checksum(out,progress)!=item['sha256']:raise InputChanged('Staged bytes differ from validated input')
            finally:
                for fd in reversed(fds):os.close(fd)
    finally:os.close(rootfd)
    require_same(bag,proof['snapshot'])
    from convest.sources.gello_rosbag2 import snapshot as job_snapshot
    ready=deepcopy(proof['readiness']);ready.update(source=str(target.resolve()),snapshot=job_snapshot(target))
    return ValidatedInput(target.resolve(),proof['validation_context'],file_snapshot(target),ready,deepcopy(proof['input_profile']))


def verify_input_content(bag,proof,progress=None):
    """Final full content check; do not repeat SQLite or stream decoding."""
    require_same(bag,proof['snapshot'])
    for item in proof['files']:
        if checksum(Path(bag)/item['path'],progress)!=item['sha256']:
            raise InputChanged('Original content changed before publication')
    require_same(bag,proof['snapshot'])


@contextmanager
def monitor_input(bag,expected,interval):
    stop=threading.Event();changed=threading.Event()
    def check():
        while not stop.wait(interval):
            try:require_same(bag,expected)
            except Exception:changed.set();return
    thread=threading.Thread(target=check,daemon=True);thread.start()
    def guard():
        if changed.is_set():raise InputChanged('Input changed during processing')
        require_same(bag,expected)
    try:guard();yield guard
    finally:stop.set();thread.join()
