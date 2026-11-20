import json
import threading
import time
from types import SimpleNamespace
from convest.subtasks.speedup_sync import SpeedupSync


def doc():return {'saved_at':1,'revision':1,'inherited':False,'segments':[{'label':'harvest','reviewed':True}]}


def server(tmp_path):
    rows=[{'id':n,'variant':'normal','workflow':'without_return','excluded':False,'superseded':False,'archived':False,'coarse_saved':True,'fine_saved':False,'pair_state':'matched','pair_ids':[n+'s'],'remote_directory':n,'episode_index':i} for i,n in enumerate(['a','b'])]
    docs={n:{'coarse':doc() if len(n)==1 else {},'fine':{}} for n in ['a','as','b','bs']};maps=[]
    def mapping(n,s,stages,replace=False):
        assert replace is False;maps.append(n);docs[s]['coarse']=doc();return {'stages':{'coarse':docs[s]['coarse']}}
    ws=SimpleNamespace(root=tmp_path,lock=threading.RLock(),catalog=lambda:{'episodes':rows},store=SimpleNamespace(episode=lambda id_:SimpleNamespace(id=id_)),stages=lambda ep:docs[ep.id],map_saved=mapping)
    return SimpleNamespace(workspace=ws,export_lock=threading.Lock()),docs,maps


def wait(sync):
    for _ in range(300):
        if sync.status()['state'] not in {'queued','running'}:return sync.status()
        time.sleep(.01)
    raise AssertionError('sync never completed')


def test_network_does_not_lock_review_and_launch_is_deduplicated(tmp_path,monkeypatch):
    srv,docs,maps=server(tmp_path);started=threading.Event();release=threading.Event();calls=[]
    def publish(ws,ep,stage,**kwargs):
        calls.append(ep.id)
        if ep.id=='a':started.set();assert release.wait(5)
    monkeypatch.setattr('convest.subtasks.delivery.publish',publish)
    sync=SpeedupSync(srv)
    try:
        first=sync.start('without_return');assert started.wait(3)
        assert sync.start('without_return')['id']==first['id']
        assert srv.workspace.lock.acquire(timeout=.2);srv.workspace.lock.release()
        assert sync.status()['active_ids']==['a','as']
        release.set();result=wait(sync)
        assert result['completed']==2 and result['checked']==2 and not result['failures']
        assert calls==['a','as','b','bs'] and maps==['a','b']
        assert not srv.workspace.syncing_ids
        assert json.loads(sync.path.read_text())['state']=='completed'
    finally:release.set();sync.close()


def test_failed_normal_export_cannot_map_and_next_item_continues(tmp_path,monkeypatch):
    srv,docs,maps=server(tmp_path);docs['bs']['coarse']=doc()
    def publish(ws,ep,stage,**kwargs):raise ValueError('offline')
    monkeypatch.setattr('convest.subtasks.delivery.publish',publish);sync=SpeedupSync(srv)
    try:
        sync.start('without_return');result=wait(sync)
        assert result['checked']==2 and len(result['failures'])==1 and not maps
        assert result['completed']==0 and result['skipped']==3
        assert docs['bs']['coarse']==doc()
    finally:sync.close()


def test_saved_stage_guard_only_blocks_active_sync_pair(tmp_path):
    import pytest
    from test_subtasks import dataset
    from convest.subtasks.core import Store
    from convest.subtasks.workspace import Workspace
    ep=dataset(tmp_path/'normal');store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    try:
        body=ws.stages(ep)['coarse'];ws.syncing_ids={ep.id};ws.speedup_sync_owner=-1
        with pytest.raises(ValueError,match='后台同步'):ws.save_stage(ep,'coarse',body)
        ws.syncing_ids={'another'};saved=ws.save_stage(ep,'coarse',body);assert saved['revision']==1
    finally:store.lockfile.close()


def test_failed_target_upload_retries_without_remapping(tmp_path,monkeypatch):
    srv,docs,maps=server(tmp_path);docs['bs']['coarse']=doc();calls=[];offline=True
    def publish(ws,ep,stage,**kwargs):
        calls.append(ep.id)
        if ep.id=='as' and offline:raise ValueError('target upload failed')
    monkeypatch.setattr('convest.subtasks.delivery.publish',publish);sync=SpeedupSync(srv)
    try:
        sync.start('without_return');first=wait(sync);assert len(first['failures'])==1 and maps==['a']
        offline=False;sync.start('without_return');second=wait(sync)
        assert second['completed']==1 and not second['failures'] and maps==['a']
        assert calls==['a','as','as'] and not sync.pending
    finally:sync.close()


def test_already_saved_pairs_skip_without_loading_media(tmp_path,monkeypatch):
    srv,docs,maps=server(tmp_path);rows=srv.workspace.catalog()['episodes']
    targets=[{'id':e['pair_ids'][0],'variant':'speedup','coarse_saved':True,'fine_saved':False} for e in rows]
    srv.workspace.catalog=lambda:{'episodes':rows+targets}
    def load_media(_):raise AssertionError('already saved target must not load full media')
    srv.workspace.stages=load_media
    sync=SpeedupSync(srv)
    try:
        sync.start('without_return');result=wait(sync)
        assert result['checked']==2 and result['skipped']==4 and not result['failures'] and not maps
    finally:sync.close()
