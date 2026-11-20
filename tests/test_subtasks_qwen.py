import copy
import json
from types import SimpleNamespace
import pytest

from convest.subtasks import qwen, processing_queue
from convest.subtasks.core import Store
from convest.subtasks.workspace import Workspace
from test_subtasks import dataset

def document(ep):
    ends=[1,2,3,4,ep.length/ep.fps]
    labels=['pickup','harvest','adjust','harvest','return']
    return {'sequence_supported':True,
        'segments':[{'phase_index':i+1,'label':label,'start_seconds':ends[i-1] if i else 0,'end_seconds':ends[i]} for i,label in enumerate(labels)],
        'handoffs':[{'from_phase':i+1,'to_phase':i+2,'seconds':ends[i],'observation':'可见动作','needs_review':False,'reason':''} for i in range(4)],
        'harvest_summary':{k:{'successful_count':1,'delivery_seconds':[t]} for k,t in [('first_harvest',1.5),('second_harvest',3.5)]},'review_notes':[]}

@pytest.mark.parametrize('fault',['null','gap','end','count','handoff','nan'])
def test_invalid_or_partial_result_is_not_invented(tmp_path,fault):
    ep=dataset(tmp_path/'full-block-more'/'normal');doc=document(ep)
    if fault=='null':doc['segments'][0]['end_seconds']=None
    if fault=='gap':doc['segments'][1]['start_seconds']=1.1
    if fault=='end':doc['segments'][-1]['end_seconds']-=.5
    if fault=='count':doc['harvest_summary']['first_harvest']['successful_count']=2
    if fault=='handoff':doc['handoffs'][0]['seconds']=1.1
    if fault=='nan':doc['segments'][0]['end_seconds']=float('nan')
    with pytest.raises(ValueError):qwen.parse(doc,ep)

def test_original_main_single_call_cache_and_source_binding(tmp_path,monkeypatch):
    ep=dataset(tmp_path/'full-block-more'/'normal');doc=document(ep);calls=[]
    key=tmp_path/'private-key';key.write_text('test-only-key');monkeypatch.setattr(qwen,'KEY_PATH',key)
    monkeypatch.setattr(ep,'validate_media',lambda *a:None)
    class Response:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def read(self,*a):return json.dumps({'model':qwen.MODEL,'choices':[{'finish_reason':'stop','message':{'content':json.dumps(doc)}}]}).encode()
    class Opener:
        def open(self,request,**kw):calls.append(json.loads(request.data));return Response()
    monkeypatch.setattr(qwen.urllib.request,'build_opener',lambda *a:Opener())
    first=qwen.annotate(ep,tmp_path/'output')
    # Re-parse the retained raw response without another paid request.
    for cached in (tmp_path/'output'/'qwen'/'responses').glob('*.json'):cached.unlink()
    second=qwen.annotate(ep,tmp_path/'output')
    assert len(calls)==1 and second['cached'] and first['network_requests']==1
    payload=calls[0];assert payload['enable_thinking'] is False
    video=payload['messages'][0]['content'][0];assert 'fps' not in video and video['type']=='video_url'
    text=payload['messages'][0]['content'][1]['text'];assert '{duration_seconds}' not in text and '6.1 秒' in text
    assert all(not r['reviewed'] for r in first['coarse_segments'])
    assert first['coarse_segments'][-1]['end_frame']==ep.length
    assert 'test-only-key' not in ''.join(p.read_text() for p in (tmp_path/'output').rglob('*.json'))
    old=copy.deepcopy(ep.identity());assert first['source']==old

def test_qwen_coarse_has_no_doubao_calls_and_preserves_saved_results(tmp_path,monkeypatch):
    ep=dataset(tmp_path/'full-block-more'/'normal');store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    calls=[]
    def annotate(ep,*a,**kw):
        calls.append(ep.id)
        return {'coarse_segments':qwen.parse(document(ep),ep),'method':'qwen_main_coarse_single_pass_v2',
                'provider':'qwen','model_analysis':document(ep),'warning':'待审核'}
    monkeypatch.setattr(qwen,'annotate',annotate)
    monkeypatch.setattr(ws,'model_client',lambda *a,**kw:pytest.fail('Qwen 不得调用豆包'))
    try:
        server=SimpleNamespace(doubao_key='')
        result=ws.process([ep.id],server,mode='coarse',provider='qwen')
        assert result['episodes'][0]['state']=='ready' and calls==[ep.id]
        layers=ws.stages(ep);assert layers['coarse']['processing_provider']=='qwen'
        assert layers['coarse']['model_analysis']['harvest_summary'] and not layers['coarse']['saved_at']
        assert layers['fine']['segments'][0]['label']=='unknown'
        ws.save_stage(ep,'coarse',layers['coarse'],confirm_review=True)
        before=ws.stage_path(ep.id,'coarse').read_bytes()
        assert ws.process([ep.id],server,mode='coarse',provider='qwen')['episodes'][0]['state']=='preserved'
        assert calls==[ep.id] and ws.stage_path(ep.id,'coarse').read_bytes()==before
        with pytest.raises(ValueError,match='粗阶段'):ws.process([ep.id],server,provider='qwen',mode='fine')
    finally:store.lockfile.close()

def test_qwen_batch_retries_failures_once_and_completed_resume_does_not_recall(tmp_path,monkeypatch):
    ep=dataset(tmp_path/'normal');store=Store([ep.root],tmp_path/'review');ws=Workspace(store);calls=[]
    def process(ids,server,**kw):
        calls.append(kw['provider']);return {'episodes':[{'id':ep.id,'state':'failed' if len(calls)==1 else 'ready'}]}
    monkeypatch.setattr(ws,'process',process)
    try:
        batch=processing_queue.create(ws,[ep.id],'coarse',provider='qwen')
        processing_queue.run(ws,None,batch['id']);assert calls==['qwen','qwen'];assert processing_queue.active(ws)['retry_order']==[ep.id]
        batch=processing_queue.active(ws);batch.update(state='running')
        processing_queue.checkpoint(ws,batch);processing_queue.run(ws,None,batch['id'])
        assert calls==['qwen','qwen'] and processing_queue.summary(ws)['provider']=='qwen'
    finally:store.lockfile.close()

def test_http_qwen_queue_reaches_editable_coarse_and_no_fine(tmp_path,monkeypatch):
    import threading
    import time
    import urllib.request
    import urllib.error
    from convest.subtasks.server import Server
    ep=dataset(tmp_path/'full-block-more'/'normal')
    monkeypatch.setattr(qwen,'configured',lambda:True)
    monkeypatch.setattr(qwen,'annotate',lambda ep,*a,**kw:{'coarse_segments':qwen.parse(document(ep),ep),
        'provider':'qwen','method':'qwen_main_coarse_single_pass_v2','model_analysis':document(ep),'warning':'待审核'})
    server=Server(('127.0.0.1',0),[ep.root],tmp_path/'review',doubao_key='')
    threading.Thread(target=server.serve_forever,daemon=True).start()
    base=f'http://127.0.0.1:{server.server_port}'
    def get(path):
        with urllib.request.urlopen(base+path) as r:return json.load(r)
    def post(body):
        request=urllib.request.Request(base+'/api/workspace/process',data=json.dumps(body).encode(),headers={
            'Content-Type':'application/json','X-Subtasks-Token':server.token})
        with urllib.request.urlopen(request) as r:return json.load(r)
    try:
        b=get('/api/bootstrap');assert b['qwen']['configured'] and b['qwen']['model']==qwen.MODEL
        with pytest.raises(urllib.error.HTTPError):post({'ids':[ep.id],'provider':'qwen','mode':'fine'})
        started=post({'ids':[ep.id],'provider':'qwen','mode':'coarse','workers':1})
        assert started['provider']=='qwen'
        for _ in range(100):
            job=get('/api/job?id='+started['job'])
            if job['state'] not in {'queued','running'}:break
            time.sleep(.02)
        assert job['state']=='completed'
        d=get('/api/workspace/episode?id='+ep.id)
        assert d['stages']['coarse']['processing_provider']=='qwen'
        assert len(d['stages']['coarse']['segments'])==5 and not d['stages']['coarse']['saved_at']
        assert d['stages']['fine']['segments'][0]['label']=='unknown'
    finally:
        server.shutdown();server.server_close();server.pool.shutdown(wait=True);server.store.lockfile.close()


def test_local_decode_repairs_only_known_end_rounding(tmp_path):
    ep=dataset(tmp_path/'full-block-more'/'normal');doc=document(ep)
    doc['segments'][-1]['end_seconds']=ep.length/ep.fps-1/ep.fps
    decoded,fixes=qwen.decode_response('视频时长说明\n```json\n'+json.dumps(doc)+'\n```',ep)
    assert fixes and qwen.parse(decoded,ep)[-1]['end_frame']==ep.length
    assert decoded['segments'][:-1]==doc['segments'][:-1]
    doc['segments'][-1]['end_seconds']-=.5
    decoded,fixes=qwen.decode_response(json.dumps(doc),ep)
    assert not fixes
    with pytest.raises(ValueError):qwen.parse(decoded,ep)


def test_local_recovery_blocks_network_when_raw_is_missing(tmp_path,monkeypatch):
    ep=dataset(tmp_path/'full-block-more'/'normal')
    monkeypatch.setattr(ep,'validate_media',lambda *a:None)
    monkeypatch.setattr(qwen.urllib.request,'build_opener',lambda *a:pytest.fail('must never open network'))
    with pytest.raises(ValueError,match='已阻止 API 调用'):qwen.annotate(ep,tmp_path/'output',allow_network=False)


def test_recover_complete_cuts_when_optional_notes_are_malformed(tmp_path):
    ep=dataset(tmp_path/'full-block-more'/'normal');doc=document(ep)
    text=json.dumps(doc).split('"review_notes":')[0]+'"review_notes": ["broken unescaped\n unfinished'
    recovered,changes=qwen.decode_response(text,ep)
    assert changes and recovered['segments']==doc['segments']
    assert qwen.parse(recovered,ep)[-1]['end_frame']==ep.length
    assert all(h['needs_review'] for h in recovered['handoffs'])
    assert recovered['harvest_summary']==doc['harvest_summary']
    with pytest.raises(ValueError):qwen.decode_response(text.replace('"end_seconds": 2','"end_seconds": missing'),ep)


def test_conflicting_count_uses_valid_delivery_times_without_changing_cuts(tmp_path):
    ep=dataset(tmp_path/'full-block-more'/'normal');doc=document(ep)
    doc['harvest_summary']['first_harvest']={'successful_count':4,'delivery_seconds':[1.2,1.5,1.8]}
    doc['harvest_summary']['second_harvest']={'successful_count':3,'delivery_seconds':[3.1,3.3,3.6,3.9]}
    fixed,changes=qwen.decode_response(json.dumps(doc),ep)
    assert [fixed['harvest_summary'][k]['successful_count'] for k in ('first_harvest','second_harvest')]==[3,4]
    assert fixed['segments']==doc['segments']
    assert fixed['harvest_summary']['first_harvest']['delivery_seconds']==[1.2,1.5,1.8]
    assert len(changes)==2 and qwen.parse(fixed,ep)
    doc['harvest_summary']['first_harvest']['successful_count']=None
    fixed,_=qwen.decode_response(json.dumps(doc),ep)
    assert fixed['harvest_summary']['first_harvest']['successful_count'] is None
    malformed=json.dumps(doc).split('"review_notes":')[0]+'"review_notes": ["broken\n'
    fixed,_=qwen.decode_response(malformed,ep)
    assert fixed['harvest_summary']['second_harvest']['successful_count']==4
    assert fixed['segments']==doc['segments']
