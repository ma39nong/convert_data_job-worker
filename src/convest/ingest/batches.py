"""Independent timed batches built with the existing immutable publisher."""
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import fcntl
import os
import re
import shutil
import time
import uuid
from convest.config import atomic_json
from convest.pipeline import load_records
from convest.jobs import checksum
from convest.verify import verify
from .publish import Publisher as GenerationPublisher,OWNER,read


def durable_json(path,data):
    atomic_json(path,data)
    with path.open('rb') as stream:os.fsync(stream.fileno())
    flush_directory(path.parent)


def flush_directory(path):
    fd=os.open(path,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


class BatchPublisher:
    def __init__(self,route,fingerprint):
        self.route=route;self.fingerprint=fingerprint
        self.output=Path(route['output']);self.store=self.output/'.ingest'
        self.duration_ns=route['batch_hours']*3600*10**9

    def _owner(self):
        return {'owner':OWNER,'layout':'timed-batches-v1','route':self.route['id'],
                'fingerprint':self.fingerprint,'batch_hours':self.route['batch_hours']}

    def _exists(self):
        if self.output.is_symlink():raise ValueError('Batch container cannot be an old dataset symlink')
        if not self.output.exists():return False
        if not self.output.is_dir():raise ValueError('Output must be a batch container directory')
        marker=self.store/'owner.json'
        if self.store.is_symlink():raise ValueError('Batch store must not be a symlink')
        if marker.exists():
            if read(marker)!=self._owner():raise ValueError('Batch container ownership/config mismatch')
            return True
        if any(self.output.iterdir()):raise ValueError('Refuse nonempty unowned batch output directory')
        return False

    def _init_store(self):
        if self._exists():return
        self.output.mkdir(parents=True,exist_ok=True);self.store.mkdir()
        durable_json(self.store/'owner.json',self._owner())
        flush_directory(self.output)

    @contextmanager
    def _locked(self):
        self._init_store()
        with (self.store/'publish.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            yield

    def _catalog(self):
        path=self.store/'batches.json'
        catalog=read(path) if path.exists() else {'schema_version':1,'batches':[]}
        if catalog.get('schema_version')!=1 or not isinstance(catalog.get('batches'),list):
            raise ValueError('Invalid batch catalogue')
        ids=set();active=0
        for batch in catalog['batches']:
            if not re.fullmatch(r'batch-[0-9]{6,}',batch.get('id','')) or batch['id'] in ids or batch.get('state') not in ('active','sealed'):
                raise ValueError('Invalid batch identity/state')
            ids.add(batch['id']);active+=batch['state']=='active'
        if active>1:raise ValueError('Multiple active batches')
        return catalog

    def _save(self,catalog):
        path=self.store/'batches.json'
        if not path.exists() or read(path)!=catalog:durable_json(path,catalog)

    def _publisher(self,batch):
        route={**self.route,'output':str(self.store/'streams'/batch['id']/'current')}
        return GenerationPublisher(route,self.fingerprint)

    def _alias_target(self,batch):
        return os.path.relpath(self._publisher(batch).output,self.output)

    def _check_alias(self,name,batch):
        alias=self.output/name
        if alias.exists() or alias.is_symlink():
            if not alias.is_symlink() or os.readlink(alias)!=self._alias_target(batch):
                raise ValueError(f'Batch range name already occupied: {name}')

    def _range(self,names,completed_ns=None,name_format=None):
        numbers=[int(n[7:]) for n in names]
        stem=f'{min(numbers)}_{max(numbers)}_data_lerobot'
        if (name_format or self.route.get('batch_name_format'))=='completion-mmdd-v1':
            stamp=time.time_ns() if completed_ns is None else completed_ns
            return datetime.fromtimestamp(stamp/1e9,ZoneInfo('Asia/Shanghai')).strftime('%m%d')+'_'+stem
        return stem

    def _sync(self,catalog):
        """Recover a committed generation even if the catalogue write was interrupted."""
        published={}
        for batch in catalog['batches']:
            pub=self._publisher(batch);root=pub.current()
            if root is None:continue
            index=pub.published();names=sorted(index,key=lambda n:(int(n[7:]),n))
            if not names:raise ValueError('Empty committed batch')
            for name,record in index.items():
                if name in published:raise ValueError('Source present in multiple batches')
                published[name]=record
            if batch.get('opened_ns') is None:
                marker=read(root/'ingest/generation.json')
                # An interrupted first publication may be recovered from its
                # generation timestamp, never by restarting the 72-hour timer.
                while marker.get('parent'):marker=read(Path(marker['parent'])/'ingest/generation.json')
                batch['opened_ns']=marker['created_ns']
                batch['deadline_ns']=batch['opened_ns']+self.duration_ns
            # The newest immutable generation records the final successful append.
            # Recover from this durable timestamp, never from seal/upload/restart time.
            completed_ns=read(root/'ingest/generation.json')['created_ns']
            name_format=batch.get('name_format','range-v1')
            desired=self._range(names,completed_ns,name_format);self._check_alias(desired,batch)
            if name_format=='completion-mmdd-v1':
                batch['last_completed_ns']=completed_ns
            alias=self.output/desired
            if not alias.is_symlink():
                old=self.output/batch['name'] if batch.get('name') else None
                if old and old.is_symlink() and os.readlink(old)==self._alias_target(batch):
                    os.rename(old,alias)
                else:alias.symlink_to(self._alias_target(batch),target_is_directory=True)
                flush_directory(self.output)
            previous=batch.get('name')
            if previous and previous!=desired:
                stale=self.output/previous
                if stale.is_symlink() and os.readlink(stale)==self._alias_target(batch):
                    stale.unlink();flush_directory(self.output)
            info=read(root/'meta/info.json')
            batch.update(name=desired,path=str(alias),sources=names,source_count=len(names),episodes=info['total_episodes'],
                         frames=info['total_frames'],generation=str(root))
        self._save(catalog)
        return published

    def _verify_seal(self,batch):
        """Full decode plus source artifact hashes, without changing the live snapshot."""
        root=self._publisher(batch).current();scratch=self.store/('.verify-'+uuid.uuid4().hex)
        try:
            for record in load_records(root):
                for relative,expected in record['ingest']['artifacts'].items():
                    artifact=root/relative;st=artifact.stat()
                    if artifact.is_symlink() or st.st_size!=expected['bytes'] or st.st_mtime_ns!=expected['mtime_ns'] or st.st_mode&0o222:
                        raise ValueError(f'Batch artifact attributes changed: {relative}')
                    if checksum(artifact)!=expected['sha256']:raise ValueError(f'Batch artifact hash mismatch: {relative}')
            # Verification writes its own reports. Copy metadata, hardlink only
            # training artifacts so a failed audit cannot mutate the live batch.
            for folder in ('data','videos'):
                if (root/folder).exists():shutil.copytree(root/folder,scratch/folder,copy_function=os.link)
            for folder in ('meta','conversion','ingest'):
                if (root/folder).exists():shutil.copytree(root/folder,scratch/folder)
            result=verify(scratch,full_video=True,compare_source=False)
            report={'status':result['status'],'episodes':result['episodes'],'frames':result['frames'],
                    'artifact_sha256':'passed','full_video_decode':True,'generation':str(root),'checked_ns':time.time_ns()}
            durable_json(self.store/'seals'/f"{batch['id']}.json",report)
            return report
        finally:
            if scratch.exists():shutil.rmtree(scratch)

    def _seal_due(self,catalog,now):
        for batch in catalog['batches']:
            if batch['state']!='active' or batch.get('deadline_ns') is None or now<batch['deadline_ns']:continue
            try:result=self._verify_seal(batch)
            except Exception as exc:
                batch['seal_error']=f'{type(exc).__name__}: {exc}';self._save(catalog);raise
            batch.update(state='sealed',sealed_ns=max(now,result['checked_ns']),verification=result)
            batch.pop('seal_error',None);self._save(catalog)

    def published(self,now_ns=None):
        if not self._exists():return {}
        with self._locked():
            catalog=self._catalog();index=self._sync(catalog)
            self._seal_due(catalog,time.time_ns() if now_ns is None else now_ns)
            return index

    def current(self):
        if not self._exists():return None
        with self._locked():
            catalog=self._catalog();self._sync(catalog)
            return next((self._publisher(b).current() for b in reversed(catalog['batches']) if b.get('name')),None)

    def status(self):
        if not self._exists():return []
        return self._catalog()['batches']

    def publish(self,job_dataset,proof,episode,audit_dir,precommit=lambda:None,started_ns=None):
        started_ns=time.time_ns() if started_ns is None else started_ns
        with self._locked():
            catalog=self._catalog();index=self._sync(catalog)
            if episode in index:
                if index[episode]['ingest']['content_identity']!=proof['content_identity']:
                    raise ValueError('Published episode name reused with different content')
                return next(self._publisher(b).current() for b in catalog['batches'] if episode in b.get('sources',[]))
            # An attempt started before the boundary finishes in that batch.
            # A later attempt can never extend the expired batch.
            self._seal_due(catalog,started_ns)
            batch=next((b for b in catalog['batches'] if b['state']=='active'),None)
            if batch is None:
                batch={'id':f"batch-{len(catalog['batches'])+1:06d}",'state':'active','opened_ns':None,'name':None,
                       'name_format':self.route.get('batch_name_format','range-v1')}
                catalog['batches'].append(batch);self._save(catalog)
            self._check_alias(self._range([*batch.get('sources',[]),episode],name_format=batch.get('name_format','range-v1')),batch)
            root=self._publisher(batch).publish(job_dataset,proof,episode,audit_dir,precommit)
            self._sync(catalog)
            self._seal_due(catalog,time.time_ns())
            return root
