"""Immutable dataset generations with an atomic A/B publication pointer."""
from pathlib import Path
from copy import deepcopy
import os
import fcntl
import shutil
import time
import uuid
import json
from convest.config import atomic_json
from convest.pipeline import load_records
from convest.formats import multimodal as writer
from convest.verify import verify_episode
from convest.jobs import checksum

OWNER='convest-ingest-v1'


def read(path):return json.loads(Path(path).read_text())


class Publisher:
    def __init__(self,route,fingerprint):
        self.route=route;self.fingerprint=fingerprint
        self.output=Path(route['output'])
        self.store=self.output.parent/'.worker-generations'/self.output.name

    def current(self):
        if not self.output.exists() and not self.output.is_symlink():return None
        if not self.output.is_symlink():
            if self.output.is_dir() and not any(self.output.iterdir()):return None
            raise ValueError('Refuse to take over a nonempty real output directory')
        root=self.output.resolve(strict=True)
        if not root.is_relative_to(self.store.resolve()):raise ValueError('Output points outside owned generation store')
        marker=read(root/'ingest/generation.json')
        if (marker.get('owner'),marker.get('route'),marker.get('fingerprint'),marker.get('state'))!=(OWNER,self.route['id'],self.fingerprint,'ready'):
            raise ValueError('Published generation ownership/config mismatch')
        return root

    def published(self):
        root=self.current()
        return read(root/'ingest/index.json') if root else {}

    def _init_store(self):
        if self.store.is_symlink():raise ValueError('Generation store must not be a symlink')
        self.store.mkdir(parents=True,exist_ok=True)
        marker=self.store/'owner.json'
        expected={'owner':OWNER,'route':self.route['id'],'fingerprint':self.fingerprint}
        if marker.exists():
            if read(marker)!=expected:raise ValueError('Generation store belongs to another config')
        else:
            if any(self.store.iterdir()):raise ValueError('Refuse nonempty unowned generation store')
            try:
                fd=os.open(marker,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            except FileExistsError:
                if read(marker)!=expected:raise ValueError('Generation ownership race')
            else:
                with os.fdopen(fd,'w') as stream:
                    json.dump(expected,stream);stream.flush();os.fsync(stream.fileno())

    def publish(self,job_dataset,proof,episode,audit_dir,precommit=lambda:None):
        self._init_store()
        with (self.store/'publish.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return self._publish(job_dataset,proof,episode,audit_dir,precommit)

    def _publish(self,job_dataset,proof,episode,audit_dir,precommit):
        old=self.current();previous=load_records(old) if old else []
        existing={r['ingest']['episode']:r for r in previous}
        if episode in existing:
            if existing[episode]['ingest']['content_identity']!=proof['content_identity']:raise ValueError('Published episode name reused with different content')
            return old
        self._init_store()
        name=f'gen-{len(previous)+1:08d}-{uuid.uuid4().hex}'
        building=self.store/('.building-'+name);finished=self.store/name
        building.mkdir()
        try:
            if old:
                # Only immutable verified files are inherited. Metadata is copied
                # separately: mutating it can never change the previous generation.
                for record in previous:
                    for relative,p in record['ingest']['artifacts'].items():
                        src=old/relative;st=src.stat()
                        if src.is_symlink() or st.st_size!=p['bytes'] or st.st_mtime_ns!=p['mtime_ns'] or st.st_mode&0o222:
                            raise ValueError('Published artifact changed; explicit full audit required')
                        dst=building/relative;dst.parent.mkdir(parents=True,exist_ok=True);os.link(src,dst)
                for folder in ('conversion/records','ingest/sources'):
                    if (old/folder).exists():shutil.copytree(old/folder,building/folder)
            records=load_records(Path(job_dataset))
            if len(records)!=1:raise ValueError('Expected exactly one converted source')
            record=deepcopy(records[0]);eps=record['episodes']
            if not eps:raise ValueError('No output episode')
            if previous and eps[0]['features']!=previous[0]['episodes'][0]['features']:
                raise ValueError('Feature/camera shape differs from the existing dataset')
            audit=read(Path(job_dataset)/'conversion/verification.json')
            if audit.get('status')!='passed' or not audit.get('independent_alignment') or not audit.get('source_comparison'):
                raise ValueError('Source comparison verification missing')
            trimming=read(Path(record['source'])/'trim_report.json')['auto_idle_detection']
            if trimming.get('effectiveness_audit',{}).get('status')!='passed':raise ValueError('Independent cleaning audit missing')
            global_index=sum(e['length'] for r in previous for e in r['episodes'])
            next_episode=sum(len(r['episodes']) for r in previous)
            artifacts={}
            for ep in eps:
                writer.reindex_segment(Path(job_dataset),ep,next_episode,global_index,0)
                ep['source_recording_id']=proof['recording_id']
                for relative in ep['paths']:
                    src=Path(job_dataset)/'final'/relative;dst=building/relative
                    dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dst)
                    h=checksum(src)
                    if checksum(dst)!=h:raise ValueError('Published copy checksum mismatch')
                    dst.chmod(0o444)
                    artifacts[relative]={'bytes':dst.stat().st_size,'mtime_ns':dst.stat().st_mtime_ns,'sha256':h}
                    with dst.open('rb') as stream:os.fsync(stream.fileno())
                global_index+=ep['length'];next_episode+=1
            record['ingest']={'episode':episode,'route':self.route['id'],**proof,'artifacts':artifacts,
                'source_comparison':'passed before publication; cleaned work files are temporary',
                'cleaning_effectiveness':trimming['effectiveness_audit']}
            source_audit=building/'ingest/sources'/episode
            shutil.copytree(audit_dir,source_audit)
            all_records=[*previous,record]
            atomic_json(building/f'conversion/records/{len(previous):06d}.json',record)
            cfg=self.route['conversion']
            atomic_json(building/'conversion/manifest.json',{'owner':'convest-data','config':cfg,'publisher':OWNER})
            writer.write_metadata(building,all_records,cfg)
            info=read(building/'meta/info.json')
            offset=sum(e['length'] for r in previous for e in r['episodes'])
            results=read(old/'conversion/verification.json')['results'] if old else []
            for ep in eps:
                result=verify_episode(building,info,ep,{'stats':ep['stats']},record,cfg,None,offset,False,True)
                results.append(result);offset+=ep['length']
            if (len(results)!=info['total_episodes'] or any(r['status']!='passed' for r in results)
                or sum(e['length'] for r in all_records for e in r['episodes'])!=info['total_frames']):
                raise ValueError('Generation metadata/verification counts differ')
            atomic_json(building/'conversion/verification.json',{'status':'passed','episodes':len(results),
                'frames':info['total_frames'],'passed_episodes':len(results),'failed_episodes':0,'results':results,
                'full_video_decode':True,'source_comparison':False,
                'scope':'new additions decoded and verified; previous immutable hardlinks inherit prior verification',
                'parent_generation':str(old) if old else None})
            atomic_json(building/'ingest/index.json',{r['ingest']['episode']:{'ingest':{k:r['ingest'][k] for k in ('episode','route','content_identity','input','snapshot')}} for r in all_records})
            atomic_json(building/'ingest/generation.json',{'owner':OWNER,'route':self.route['id'],
                'fingerprint':self.fingerprint,'state':'ready','created_ns':time.time_ns(),'parent':str(old) if old else None})
            for folder in building.rglob('*'):
                if folder.is_file() and not folder.is_symlink():
                    with folder.open('rb') as stream:os.fsync(stream.fileno())
            building.rename(finished)
            # Flush directory entries before exposing the new generation.
            for folder in [p for p in finished.rglob('*') if p.is_dir()]+[finished,self.store]:
                fd=os.open(folder,os.O_RDONLY)
                try:os.fsync(fd)
                finally:os.close(fd)
            if self.current()!=old:raise ValueError('Publication pointer changed concurrently')
            precommit()
            if self.output.exists() and not self.output.is_symlink():
                self.output.rmdir()  # Atomic only-if-empty; never remove another dataset.
            temporary=self.output.with_name('.'+self.output.name+'.publish-'+uuid.uuid4().hex)
            try:
                temporary.symlink_to(os.path.relpath(finished,self.output.parent),target_is_directory=True)
                precommit()
                os.replace(temporary,self.output)
                fd=os.open(self.output.parent,os.O_RDONLY)
                try:os.fsync(fd)
                finally:os.close(fd)
            finally:temporary.unlink(missing_ok=True)
            return finished
        except BaseException:
            if building.exists():shutil.rmtree(building)
            if finished.exists() and (not self.output.is_symlink() or self.output.resolve()!=finished.resolve()):
                shutil.rmtree(finished)
            raise
