"""Background reviewed-stage sync. Network work never holds the UI busy flag."""
import copy
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from .core import atomic_json


def reviewed(doc):
    return bool(doc.get('saved_at') and not doc.get('inherited') and doc.get('segments') and all(s.get('reviewed') is True and s.get('label')!='unknown' for s in doc['segments']))


class SpeedupSync:
    def __init__(self,server):
        self.server=server;self.ws=server.workspace;self.lock=threading.RLock();self.pool=ThreadPoolExecutor(max_workers=1)
        self.pending_path=self.ws.root/'speedup-sync'/'pending-exports.json'
        self.pending=json.loads(self.pending_path.read_text()) if self.pending_path.exists() else {}
        self.job=None;self.ws.syncing_ids=set();self.ws.speedup_sync_owner=None
        self.path=self.ws.root/'speedup-sync'/'latest.json'
        if self.path.exists():
            self.job=json.loads(self.path.read_text())
            if self.job['state'] in {'queued','running'}:
                self.job.update(state='interrupted',active_ids=[],error='服务中断；已完成结果保留，可重新同步剩余项');self.checkpoint()

    def checkpoint(self):
        atomic_json(self.path,self.job)
        atomic_json(self.path.parent/(self.job['id']+'.json'),self.job)

    def status(self):
        with self.lock:return copy.deepcopy(self.job)

    def start(self,workflow):
        if workflow not in {'with_return','without_return'}:raise ValueError('无效任务分栏')
        with self.lock:
            if self.job and self.job['state'] in {'queued','running'}:return copy.deepcopy(self.job)
            self.job={'id':uuid.uuid4().hex,'workflow':workflow,'state':'queued','checked':0,'total':0,'completed':0,'skipped':0,'failures':[],'active_ids':[],'started_at':time.time()}
            self.checkpoint();self.pool.submit(self.run)
            return copy.deepcopy(self.job)

    def run(self):
        from .delivery import publish
        from .quarantine import assert_included
        ws=self.ws
        try:
            catalog={e['id']:e for e in ws.catalog()['episodes']}
            rows=[e for e in catalog.values() if e['variant']=='normal' and e['workflow']==self.job['workflow'] and not e['excluded'] and not e['superseded'] and not e['archived'] and (e['coarse_saved'] or e['fine_saved'])]
            with self.lock:self.job.update(state='running',total=len(rows));self.checkpoint()
            for entry in rows:
                id_=entry['id'];target_id=None
                try:
                    # Reserve only this pair. Reads and unrelated review saves remain available.
                    with ws.lock:
                        assert_included(ws,id_)
                        if entry['pair_state']!='matched' or len(entry['pair_ids'])!=1:raise ValueError('没有唯一对应的speedup')
                        target_id=entry['pair_ids'][0];assert_included(ws,target_id)
                        target_entry=catalog.get(target_id,{})
                        if target_entry and all(not entry[stage+'_saved'] or target_entry[stage+'_saved'] for stage in ('coarse','fine')) and not any(target_id+':'+stage in self.pending for stage in ('coarse','fine')):
                            with self.lock:self.job['skipped']+=2
                            continue
                        ws.syncing_ids={id_,target_id};ws.speedup_sync_owner=threading.get_ident()
                    with self.lock:self.job['active_ids']=[id_,target_id];self.job['current']=entry['remote_directory']+' / '+str(entry['episode_index']);self.checkpoint()
                    normal=ws.store.episode(id_);target=ws.store.episode(target_id)
                    sources=ws.stages(normal);targets=ws.stages(target)
                    for stage in ('coarse','fine'):
                        pending_key=target_id+':'+stage
                        pending=self.pending.get(pending_key)
                        if pending and reviewed(sources[stage]) and reviewed(targets[stage]) and pending['normal_id']==id_ and pending['normal_revision']==sources[stage]['revision'] and pending['target_revision']==targets[stage]['revision']:
                            try:
                                with self.server.export_lock:publish(ws,target,stage,revision=targets[stage]['revision'])
                                del self.pending[pending_key];atomic_json(self.pending_path,self.pending)
                                with self.lock:self.job['completed']+=1
                            except Exception as exc:
                                with self.lock:self.job['failures'].append({'id':id_,'package':entry['remote_directory'],'episode_index':entry['episode_index'],'stage':stage,'error':str(exc)})
                            continue
                        if not reviewed(sources[stage]) or targets[stage].get('saved_at') or targets[stage].get('projection'):
                            with self.lock:self.job['skipped']+=1
                            continue
                        try:
                            # Keep source verification and normal publication before mapping.
                            with self.server.export_lock:publish(ws,normal,stage,revision=sources[stage]['revision'])
                            mapped=ws.map_saved(id_,target_id,[stage],replace=False)['stages'][stage]
                            self.pending[pending_key]={'normal_id':id_,'normal_revision':sources[stage]['revision'],'target_revision':mapped['revision']}
                            atomic_json(self.pending_path,self.pending)
                            with self.server.export_lock:publish(ws,target,stage,revision=mapped['revision'])
                            del self.pending[pending_key];atomic_json(self.pending_path,self.pending)
                            with self.lock:self.job['completed']+=1
                        except Exception as exc:
                            with self.lock:self.job['failures'].append({'id':id_,'package':entry['remote_directory'],'episode_index':entry['episode_index'],'stage':stage,'error':str(exc)})
                except Exception as exc:
                    with self.lock:self.job['failures'].append({'id':id_,'package':entry['remote_directory'],'episode_index':entry['episode_index'],'error':str(exc)})
                finally:
                    with ws.lock:ws.syncing_ids=set();ws.speedup_sync_owner=None
                    with self.lock:self.job['checked']+=1;self.job['active_ids']=[];self.checkpoint()
            with self.lock:self.job.update(state='completed',finished_at=time.time());self.checkpoint()
        except Exception as exc:
            with ws.lock:ws.syncing_ids=set();ws.speedup_sync_owner=None
            with self.lock:self.job.update(state='failed',error=str(exc),active_ids=[],finished_at=time.time());self.checkpoint()

    def close(self):self.pool.shutdown(wait=True)
