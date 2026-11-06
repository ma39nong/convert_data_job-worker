from convest.subtasks.vocabulary import english_document
"""Import/queue and independently versioned stage results preserve old review data."""
import copy
import json
import threading
import urllib.request
from types import SimpleNamespace

import pytest

from test_subtasks import dataset
from convest.subtasks.core import Store, segment, sha, atomic_json
from convest.subtasks.workspace import Workspace
from convest.subtasks.remote import dataset_path
from convest.subtasks.server import Server


def test_independent_stage_saves_history_conflicts_and_readonly_sources(tmp_path):
    ep=dataset(tmp_path/'normal');store=Store([ep.root],tmp_path/'review')
    try:
        original=store.save(ep,{**store.load(ep),'segments':[segment(0,20,'grasp_truss',reviewed=True),segment(20,61,'lift_truss',reviewed=True)]})
        before=store.path(ep.id).read_bytes();sources={str(p):sha(p) for p in ep.root.rglob('*') if p.is_file()};ws=Workspace(store)
        docs=ws.stages(ep);coarse=copy.deepcopy(docs['coarse']);coarse['segments'][0]['label']='adjust'
        saved=ws.save_stage(ep,'coarse',coarse)
        assert saved['revision']==1 and saved['segments'][0]['text']=='调整番茄'
        assert ws.stages(ep)['fine']==docs['fine']
        with pytest.raises(ValueError,match='其他窗口'):ws.save_stage(ep,'coarse',coarse)
        fine=copy.deepcopy(docs['fine']);fine['segments'][0]['end_frame']=15;fine['segments'][1]['start_frame']=15
        f=ws.save_stage(ep,'fine',fine);assert f['revision']==1
        assert ws.stages(ep)['coarse']==saved
        assert store.path(ep.id).read_bytes()==before and store.load(ep)==original
        assert sources=={str(p):sha(p) for p in ep.root.rglob('*') if p.is_file()}
        assert (ws.root/'history'/ep.id/'coarse/000001.json').exists()
        assert (ws.root/'history'/ep.id/'fine/000001.json').exists()
        entry=ws.catalog()['episodes'][0];assert entry['fine_saved'] and entry['coarse_saved'] and entry['human_reviewed']
    finally:store.lockfile.close()


def test_stage_rejects_wrong_source_stale_draft_and_invalid_coarse(tmp_path):
    ep=dataset(tmp_path/'normal');store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    try:
        stale=ws.stages(ep)
        invalid=copy.deepcopy(stale['coarse']);invalid['segments'][0]['start_frame']=1
        with pytest.raises(ValueError,match='连续'):ws.save_stage(ep,'coarse',invalid)
        invalid=copy.deepcopy(stale['fine']);invalid['source']['length']=5
        with pytest.raises(ValueError,match='来源'):ws.save_stage(ep,'fine',invalid)
        store.save(ep,{**store.load(ep),'segments':[segment(0,61,'grasp_truss')]})
        with pytest.raises(ValueError,match='草稿已更新'):ws.save_stage(ep,'fine',stale['fine'])
    finally:store.lockfile.close()


@pytest.mark.parametrize('value',['/etc/passwd','sftp://29/mnt/data/train_platform/datasets/a','/mnt/data/train_platform/datasets/../secret','relative/path'])
def test_remote_import_path_cannot_escape_dataset_root(value):
    with pytest.raises(ValueError):dataset_path(value)


def test_import_reuses_registered_remote_episode_without_download(tmp_path,monkeypatch):
    from convest.subtasks import remote
    ep=dataset(tmp_path/'normal');store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    path='/mnt/data/train_platform/datasets/tomato_sharpa/normal/example'
    atomic_json(ep.root/'cache-source.json',{'host':'28','manifest':{'dataset':path}})
    monkeypatch.setattr(remote,'web_inventory',lambda *a:{'dataset':path,'episodes':[{'episode_index':0}]})
    monkeypatch.setattr(remote,'fetch',lambda *a:pytest.fail('重复导入不应再下载'))
    try:
        result=ws.import_directory('sftp://28'+path,[0]);assert result=={'directory':path,'imported':0,'reused':1,'episodes':1}
        assert len(ws.catalog()['episodes'])==1
        with pytest.raises(ValueError,match='实际存在'):ws.import_directory(path,[99])
    finally:store.lockfile.close()


def test_queue_recovers_interruption_and_preserves_human_stage(tmp_path,monkeypatch):
    from convest.subtasks import doubao
    ep=dataset(tmp_path/'normal');store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    try:
        ws.queue[ep.id]={'state':'running'};ws._checkpoint();ws=Workspace(store)
        assert ws.catalog()['episodes'][0]['state']=='interrupted'
        fine=ws.stages(ep)['fine'];fine['segments']=[segment(0,61,'grasp_truss')];ws.save_stage(ep,'fine',fine)
        monkeypatch.setattr(doubao,'annotate',lambda *a,**kw:pytest.fail('不能自动覆盖已保存细阶段'))
        result=ws.process([ep.id],SimpleNamespace(doubao_key='not-a-real-key'))
        assert result['episodes'][0]['state']=='preserved' and store.load(ep)['revision']==0
    finally:store.lockfile.close()


def test_queue_continues_after_failure_and_publishes_only_complete_results(tmp_path,monkeypatch):
    from convest.subtasks import doubao,behavior
    monkeypatch.setattr(behavior,'scan',lambda *a,**kw:{'events':[]})
    first=dataset(tmp_path/'one',recording='one');second=dataset(tmp_path/'two',recording='two')
    store=Store([first.root,second.root],tmp_path/'review');ws=Workspace(store);calls=[]
    def model(ep,*args,**kwargs):
        calls.append(ep.id)
        if ep.id==first.id:raise ValueError('测试失败')
        return {'method':'doubao_template_source_frames_v1','segments':[segment(0,61,'grasp_truss')]}
    monkeypatch.setattr(doubao,'annotate',model)
    try:
        result=ws.process([first.id,second.id],SimpleNamespace(doubao_key='not-a-real-key',doubao_model='test',progress=lambda *a:None))
        assert calls==[first.id,second.id]
        assert [r['state'] for r in result['episodes']]==['failed','ready']
        assert store.load(first)['revision']==0 and store.load(second)['revision']==1
    finally:store.lockfile.close()


def test_http_independent_stage_download_and_catalog(tmp_path):
    ep=dataset(tmp_path/'normal');server=Server(('127.0.0.1',0),[ep.root],tmp_path/'review')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base=f'http://127.0.0.1:{server.server_port}'
    try:
        d=json.load(urllib.request.urlopen(base+'/api/workspace/episode?id='+ep.id))
        payload={'id':ep.id,'stage':'coarse','document':d['stages']['coarse']}
        req=urllib.request.Request(base+'/api/workspace/save',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Subtasks-Token':server.token})
        saved=json.load(urllib.request.urlopen(req))['saved'];assert saved['stage']=='coarse' and saved['revision']==1
        with urllib.request.urlopen(base+'/api/workspace/download?id='+ep.id+'&stage=coarse') as r:
            assert 'coarse.json' in r.headers['Content-Disposition'] and json.load(r)==english_document(saved)
        c=json.load(urllib.request.urlopen(base+'/api/workspace'));assert c['episodes'][0]['coarse_saved'] and not c['episodes'][0]['fine_saved']
        assert server.store.load(ep)['revision']==0
    finally:server.shutdown();server.server_close();thread.join()


def test_import_new_cache_is_separate_and_registered_after_restart(tmp_path,monkeypatch):
    import shutil
    from convest.subtasks import remote
    ep=dataset(tmp_path/'original');store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    remote_path='/mnt/data/train_platform/datasets/tomato_sharpa/normal/new'
    source_before={str(p):sha(p) for p in ep.root.rglob('*') if p.is_file()}
    monkeypatch.setattr(remote,'web_inventory',lambda *a:{'dataset':remote_path,'episodes':[{'episode_index':0}]})
    def fetch(host,path,episodes,destination):
        shutil.copytree(ep.root,destination)
        atomic_json(destination/'cache-source.json',{'host':'28','manifest':{'dataset':remote_path}})
    monkeypatch.setattr(remote,'fetch',fetch)
    try:
        r=ws.import_directory(remote_path,[0]);assert r['imported']==1 and len(ws.catalog()['episodes'])==2
        assert not ws.cache_root.is_relative_to(ep.root)
        assert source_before=={str(p):sha(p) for p in ep.root.rglob('*') if p.is_file()}
    finally:store.lockfile.close()
    restored=Store([ep.root],tmp_path/'review')
    try:
        assert len(Workspace(restored).catalog()['episodes'])==2
    finally:restored.lockfile.close()


def test_speedup_queue_uses_saved_normal_and_keeps_mapping(tmp_path,monkeypatch):
    from convest.subtasks import boundaries
    from convest.subtasks.core import project
    normal=dataset(tmp_path/'normal');speed=dataset(tmp_path/'speedup',speedup=True)
    store=Store([normal.root,speed.root],tmp_path/'review');ws=Workspace(store)
    try:
        fine=ws.stages(normal)['fine'];fine['segments']=[segment(0,20,'grasp_truss'),segment(20,61,'lift_truss')];saved=ws.save_stage(normal,'fine',fine)
        def audit(ep,segments,*a,**kw):
            checked=copy.deepcopy(segments);checked[0]['end_frame']+=1;checked[1]['start_frame']+=1
            return checked,{'0':{'source_frame':{'needs_review':False}}},{}
        monkeypatch.setattr(boundaries,'refine_all',audit)
        result=ws._proposal(speed,store.load(speed),SimpleNamespace(progress=lambda *a:None),None)
        expected=project(normal,speed,{**store.load(normal),'segments':saved['segments']})
        assert result['segments']==expected['segments'] and result['mapping_visual_disagreements']==[0]
        assert result['projection']['normal_workspace_revision']==1 and result['unresolved_boundaries']==[0]
    finally:store.lockfile.close()


@pytest.mark.parametrize('stage,expected',[('coarse','ready'),('fine','failed')])
def test_manual_save_during_model_job_is_preserved(tmp_path,monkeypatch,stage,expected):
    from convest.subtasks import doubao,behavior
    ep=dataset(tmp_path/'normal');store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    monkeypatch.setattr(behavior,'scan',lambda *a,**kw:{'events':[]})
    def model(*a,**kw):
        manual=ws.stages(ep)[stage]
        manual['segments']=[{'start_frame':0,'end_frame':61,'label':'adjust' if stage=='coarse' else 'rotate_stem','reviewed':False}]
        ws.save_stage(ep,stage,manual)
        return {'method':'doubao_template_source_frames_v1','segments':[segment(0,61,'grasp_truss')]}
    monkeypatch.setattr(doubao,'annotate',model)
    try:
        result=ws.process([ep.id],SimpleNamespace(doubao_key='not-a-real-key',doubao_model='test',progress=lambda *a:None))
        assert result['episodes'][0]['state']==expected
        assert ws.stages(ep)[stage]['segments'][0]['label']==('adjust' if stage=='coarse' else 'rotate_stem')
        assert store.load(ep)['revision']==(1 if stage=='coarse' else 0)
    finally:store.lockfile.close()


def test_web_inventory_resolves_tree_before_reading_metadata(monkeypatch):
    from convest.subtasks import remote
    def denied(*a):raise ValueError('outside dataset tree')
    monkeypatch.setattr(remote,'browse',denied)
    monkeypatch.setattr(remote,'query',lambda *a:pytest.fail('不能越过解析后的目录边界'))
    with pytest.raises(ValueError,match='outside'):remote.web_inventory('/mnt/data/train_platform/datasets/link')


def test_previously_reviewed_fine_result_is_saved_without_migrating_annotation(tmp_path):
    ep=dataset(tmp_path/'normal');store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    try:
        doc=store.save(ep,{**store.load(ep),'segments':[segment(0,61,'rotate_stem',reviewed=True)]})
        before=store.path(ep.id).read_bytes();stages=ws.stages(ep)
        assert stages['fine']['saved_at'] and stages['fine']['inherited']
        assert stages['fine']['segments'][0]['text']=='右手调整番茄'
        assert not stages['coarse']['saved_at']
        entry=ws.catalog()['episodes'][0];assert entry['fine_saved'] and not entry['coarse_saved']
        assert store.path(ep.id).read_bytes()==before and store.load(ep)==doc
        assert not ws.stage_path(ep.id,'fine').exists()
    finally:store.lockfile.close()


def test_explicit_fine_review_status_takes_precedence_over_legacy(tmp_path):
    ep=dataset(tmp_path/'normal');store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    try:
        store.save(ep,{**store.load(ep),'segments':[segment(0,61,'grasp_truss',reviewed=True)]})
        fine=ws.stages(ep)['fine'];fine['segments'][0]['reviewed']=False
        fine=ws.save_stage(ep,'fine',fine)
        assert not ws.catalog()['episodes'][0]['human_reviewed']
        fine['segments'][0]['reviewed']=True;ws.save_stage(ep,'fine',fine)
        assert ws.catalog()['episodes'][0]['human_reviewed']
        assert store.load(ep)['revision']==1
    finally:store.lockfile.close()


def test_speedup_dependency_wait_is_not_failure_and_becomes_runnable(tmp_path,monkeypatch):
    from convest.subtasks import doubao,behavior
    normal=dataset(tmp_path/'normal');speed=dataset(tmp_path/'speedup',speedup=True)
    store=Store([normal.root,speed.root],tmp_path/'review');ws=Workspace(store)
    monkeypatch.setattr(doubao,'annotate',lambda *a,**kw:pytest.fail('依赖未就绪不应调用模型'))
    monkeypatch.setattr(behavior,'scan',lambda *a,**kw:pytest.fail('依赖未就绪不应扫描'))
    try:
        result=ws.process([speed.id],SimpleNamespace(doubao_key='test'))
        assert result['episodes'][0]['state']=='waiting_normal'
        item=next(e for e in ws.catalog()['episodes'] if e['id']==speed.id)
        assert item['state']=='waiting_normal' and item['depends_on']==normal.id
        assert sum(g['failed'] for g in ws.catalog()['directories'])==0
        assert store.load(speed)['revision']==0
        # Existing versions used a generic failure string. Derive the wait without
        # rewriting a live queue file, and expose pending once normal is ready.
        ws.queue[speed.id]={'state':'failed','error':'请先处理同源normal，再处理speedup'}
        assert next(e for e in ws.catalog()['episodes'] if e['id']==speed.id)['state']=='waiting_normal'
        store.save(normal,{**store.load(normal),'segments':[segment(0,61,'grasp_truss')]})
        assert next(e for e in ws.catalog()['episodes'] if e['id']==speed.id)['state']=='pending'
        assert ws.queue[speed.id]['state']=='failed'  # The old record stays auditable.
        ws.queue[speed.id]={'state':'failed','error':'HTTP 500'}
        assert next(e for e in ws.catalog()['episodes'] if e['id']==speed.id)['state']=='failed'
    finally:store.lockfile.close()


def test_missing_normal_waits_for_import_instead_of_independent_inference(tmp_path,monkeypatch):
    from convest.subtasks import doubao
    speed=dataset(tmp_path/'speedup',speedup=True);store=Store([speed.root],tmp_path/'review');ws=Workspace(store)
    monkeypatch.setattr(doubao,'annotate',lambda *a,**kw:pytest.fail('没有normal时不独立猜测speedup'))
    try:
        result=ws.process([speed.id],SimpleNamespace(doubao_key='test'))
        assert result['episodes'][0]['state']=='waiting_normal'
        item=ws.catalog()['episodes'][0];assert item['depends_on'] is None and '导入' in item['error']
    finally:store.lockfile.close()


def test_coarse_processing_is_independent_and_can_expand_to_fine(tmp_path,monkeypatch):
    from convest.subtasks import template,doubao,behavior
    ep=dataset(tmp_path/'normal');store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    server=SimpleNamespace(doubao_key='test',doubao_model='test',progress=lambda *a:None)
    calls=[]
    def overview(*args,**kw):
        assert kw['coarse_only'] is True;calls.append('coarse')
        return {'coarse_segments':[dict(start_frame=0,end_frame=61,label='pickup',reviewed=False)],'warning':'概览，需审核'}
    monkeypatch.setattr(template,'annotate_template',overview)
    monkeypatch.setattr(behavior,'scan',lambda *a,**kw:pytest.fail('粗处理不能做逐帧行为扫描'))
    try:
        original=store.load(ep)
        assert ws.process([ep.id],server,mode='coarse')['episodes'][0]['state']=='ready'
        assert store.load(ep)==original
        assert not ws.stage_path(ep.id,'fine').exists() and not ws.stage_path(ep.id,'coarse').exists()
        layers=ws.stages(ep);assert layers['coarse']['segments'][0]['label']=='pickup'
        assert layers['coarse']['saved_at'] is None and layers['fine']['segments'][0]['label']=='unknown'
        item=ws.catalog()['episodes'][0];assert item['coarse_ready'] and not item['fine_ready']
        monkeypatch.setattr(doubao,'annotate',lambda *a,**kw:{'method':'test','segments':[segment(0,61,'grasp_fruit')]})
        monkeypatch.setattr(behavior,'scan',lambda *a,**kw:{'events':[]})
        assert ws.process([ep.id],server,mode='fine')['episodes'][0]['state']=='ready'
        assert ws.stages(ep)['coarse']['segments'][0]['label']=='harvest' # stale rough proposal must not warp new fine results
        assert ws.catalog()['episodes'][0]['fine_ready']
        assert calls==['coarse']
    finally:store.lockfile.close()


def test_coarse_processing_preserves_manual_layer_and_maps_speedup(tmp_path,monkeypatch):
    from convest.subtasks import template
    normal=dataset(tmp_path/'normal');speed=dataset(tmp_path/'speedup',speedup=True)
    store=Store([normal.root,speed.root],tmp_path/'review');ws=Workspace(store)
    server=SimpleNamespace(doubao_key='test',doubao_model='test',progress=lambda *a:None)
    monkeypatch.setattr(template,'annotate_template',lambda *a,**kw:pytest.fail('映射/人工结果不应调用视觉模型'))
    try:
        coarse=ws.stages(normal)['coarse'];coarse['segments']=[dict(start_frame=0,end_frame=20,label='pickup',reviewed=True),dict(start_frame=20,end_frame=61,label='harvest',reviewed=True)]
        ws.save_stage(normal,'coarse',coarse);before=ws.stage_path(normal.id,'coarse').read_bytes()
        assert ws.process([normal.id],server,mode='coarse')['episodes'][0]['state']=='preserved'
        assert before==ws.stage_path(normal.id,'coarse').read_bytes()
        result=ws.process([speed.id],server,mode='coarse');assert result['episodes'][0]['state']=='ready'
        rows=ws.stages(speed)['coarse']['segments'];assert [r['label'] for r in rows]==['pickup','harvest']
        assert all(not r['reviewed'] for r in rows) and rows[-1]['end_frame']==speed.length
        assert ws.stages(speed)['fine']['segments'][0]['label']=='unknown'
        with pytest.raises(ValueError,match='粗阶段'):ws.process([normal.id],server,mode='invalid')
    finally:store.lockfile.close()
