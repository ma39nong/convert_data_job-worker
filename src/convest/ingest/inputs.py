"""Observed stability and read-only structural checks; no uploader cooperation."""
from contextlib import contextmanager
from pathlib import Path
import os
import shutil
import threading
import time
from convest.config import digest
from convest.jobs import check_ready,checksum
from convest.sources.gello_rosbag2 import discover
import yaml


class InputChanged(RuntimeError):pass


def check_upload_layout(staging,formal_parent):
    """Read-only preflight; the external uploader must still use rename(2)."""
    staging=Path(staging).resolve(strict=True);formal_parent=Path(formal_parent).resolve(strict=True)
    if not staging.is_dir() or '.uploading' not in staging.parts:raise ValueError('Staging directory must be below .uploading')
    if not formal_parent.is_dir() or '.uploading' in formal_parent.parts:raise ValueError('Destination must be a formal category directory')
    if staging.stat().st_dev!=formal_parent.stat().st_dev:
        raise ValueError('Cross-filesystem upload publication prohibited; rename would fail with EXDEV')
    return {'same_device':True,'staging':str(staging),'formal_parent':str(formal_parent),
            'uploader_operation_verified':False,'required_operation':'same-filesystem atomic rename; no copy/delete fallback'}


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


def check_input(bag,route,expected):
    bag=Path(bag);require_same(bag,expected)
    cfg=route['conversion'];contract=yaml.safe_load(Path(cfg['contract']).read_text());profile={}
    ready=check_ready(bag,contract,cfg['schema_dir'],profile)
    item=discover(bag)[0]
    if not item['eligible']:raise ValueError(item['reason'])
    known={f['name']:f['sha256'] for f in ready['volumes']}
    known.update({'metadata.yaml':ready['metadata_sha256'],'collection_state.json':ready['collection_state_sha256']})
    files=[{'path':r['path'],'size':r['size'],'sha256':known.get(r['path']) or checksum(bag/r['path'])}
           for r in expected if not r['directory']]
    require_same(bag,expected)
    identity=digest({'robot':route['robot'],'group':route['group'],'episode':bag.name,'files':files})
    return {'content_identity':identity,'recording_id':item['source_recording_id'],'input':str(bag),'snapshot':expected,'files':files,
            'readiness':ready,'input_profile':profile,'readiness_basis':'formal-directory publication by upstream atomic rename contract; defensive stability and structural validation'}


def stage_input(bag,target,proof):
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
                    with out.open('xb') as dest:shutil.copyfileobj(source,dest,8*1024*1024)
                if checksum(out)!=item['sha256']:raise InputChanged('Staged bytes differ from validated input')
            finally:
                for fd in reversed(fds):os.close(fd)
    finally:os.close(rootfd)
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
