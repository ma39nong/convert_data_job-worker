"""Annotation safety, real video feature extraction and source-frame projection."""
import json
from pathlib import Path
import threading
import urllib.error
import urllib.request

import av
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from convest.subtasks.core import Episode, Store, atomic_json, motion_candidates, project, segment, sha, validate_segments
from convest.subtasks.learning import descriptors, predict, train
from convest.subtasks.server import Server


def video(path, frames, fps=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(path), 'w') as container:
        stream = container.add_stream('libx264', rate=fps);stream.width=32;stream.height=32;stream.pix_fmt='yuv420p'
        for idx in frames:
            image = np.zeros((32, 32, 3), np.uint8)
            image[:, :, 0 if idx < 30 else 1] = 180
            image[:, :, 2] = idx % 70
            f = av.VideoFrame.from_ndarray(image, format='rgb24')
            for packet in stream.encode(f): container.mux(packet)
        for packet in stream.encode(): container.mux(packet)


def dataset(root, *, speedup=False, recording='recording-1'):
    n = 30 if speedup else 61
    source = np.arange(n)*2 if speedup else np.arange(n)
    q = np.stack([source/10, (source >= 30)*2.0], axis=1)
    table = pa.table({'observation.state': q.tolist(), 'action': q.tolist(),
                      'frame_index': list(range(n)), 'episode_index': [0]*n,
                      'index': list(range(n)), 'task_index': [0]*n,
                      'timestamp': np.arange(n)/10, 'source_timestamp_ns': (source+1000).tolist()})
    (root/'data/chunk-000').mkdir(parents=True)
    pq.write_table(table, root/'data/chunk-000/episode_000000.parquet')
    names = ['left_fr3_joint1.position', 'right_fr3_joint1.position']
    features = {'observation.state': {'names': names, 'dtype':'float64', 'shape':[2]}}
    for k in range(3):
        camera = f'observation.images.cam{k}'
        features[camera] = {'dtype':'video','shape':[32,32,3]}
        video(root/f'videos/chunk-000/{camera}/episode_000000.mp4', source)
    atomic_json(root/'meta/info.json', {'codebase_version':'v2.1','fps':10,'chunks_size':1000,
        'total_episodes':1,'total_frames':n,'features':features,
        'data_path':'data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet',
        'video_path':'videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4'})
    entry = {'episode_index':0,'length':n,'source_recording_id':recording,'source_start_ns':1000,'source_end_ns':1060}
    (root/'meta/episodes.jsonl').write_text(json.dumps(entry)+'\n')
    (root/'meta/tasks.jsonl').write_text('{"task_index":0,"task":"pick"}\n')
    if speedup:
        atomic_json(root/'processing/episode_000000.json', {'episode_index':0,'source_length':61,
            'source_index':source.tolist(),'action_source_index':(source+2).tolist()})
    return Episode(root, 0)


@pytest.fixture
def pair(tmp_path):
    return dataset(tmp_path/'normal'), dataset(tmp_path/'speedup',speedup=True)


def test_validation_derives_coarse_and_requires_full_coverage():
    ss = validate_segments([{'start_frame':0,'end_frame':10,'label':'grasp_fruit','coarse':'错误'}],10,30)
    assert ss[0]['coarse']=='摘取番茄' and ss[0]['end_seconds']==10/30
    for bad in [[],[segment(1,10)],[segment(0,9)],[segment(0,6),segment(5,10)],
                [segment(0,0),segment(0,10)],[segment(0,10,reviewed=True)],
                [{**segment(0,10),'start_frame':False}],
                [{**segment(0,10),'similarity_margin':float('nan')}]]:
        with pytest.raises(ValueError):validate_segments(bad,10,30)


def test_store_source_readonly_history_and_revision(pair,tmp_path):
    normal,_=pair;before={str(p):sha(p) for p in normal.root.rglob('*') if p.is_file()}
    store=Store([normal.root],tmp_path/'review')
    try:
        doc=store.load(normal);old=json.loads(json.dumps(doc))
        doc['segments']=[segment(0,30,'grasp_truss',reviewed=True),segment(30,61,'pick_and_place',reviewed=True)]
        saved=store.save(normal,doc)
        assert saved['status']=='reviewed' and saved['revision']==1
        assert (store.output/'history'/normal.id/'000001.json').is_file()
        with pytest.raises(ValueError,match='其他窗口'):store.save(normal,old)
        with pytest.raises(BlockingIOError):Store([normal.root],store.output)
        assert before=={str(p):sha(p) for p in normal.root.rglob('*') if p.is_file()}
        with (normal.root/'meta/episodes.jsonl').open('a') as f:f.write(' ')
        with pytest.raises(ValueError,match='指纹'):store.load(Episode(normal.root,0))
    finally:store.lockfile.close()


def test_output_cannot_overlap_source(pair):
    normal,_=pair
    with pytest.raises(ValueError,match='分离'):Store([normal.root],normal.root/'annotations')


def test_project_exact_nearest_frame_and_collapsed_interval(pair,tmp_path):
    normal,speedup=pair;store=Store([normal.root,speedup.root],tmp_path/'review')
    try:
        doc=store.load(normal)
        doc['segments']=[segment(0,1,'grasp_truss',reviewed=True),segment(1,2,'lift_truss',reviewed=True),
                         segment(2,30,'grasp_fruit',reviewed=True),segment(30,61,'pick_and_place',reviewed=True)]
        doc=store.save(normal,doc)
        result=project(normal,speedup,doc)
        assert [(s['start_frame'],s['end_frame'],s['label']) for s in result['segments']]==[
            (0,1,'grasp_truss'),(1,15,'grasp_fruit'),(15,30,'pick_and_place')]
        assert result['collapsed_normal_segments']==[1]
        assert result['action_crosses_boundary_frames']==[0,14]
        assert all(not s['reviewed'] for s in result['segments'])
        assert result['normal_status']=='reviewed'
    finally:store.lockfile.close()


def test_projection_rejects_wrong_recording_and_corrupt_mapping(pair,tmp_path):
    normal,speedup=pair;store=Store([normal.root],tmp_path/'review')
    try:
        doc=store.load(normal)
        speedup.entry['source_recording_id']='other'
        with pytest.raises(ValueError,match='身份'):project(normal,speedup,doc)
        speedup.entry['source_recording_id']=normal.entry['source_recording_id']
        p=speedup.root/'processing/episode_000000.json';m=json.loads(p.read_text());m['source_index'][3]=-1;atomic_json(p,m)
        with pytest.raises(ValueError,match='映射'):project(normal,speedup,doc)
    finally:store.lockfile.close()


def test_projection_rejects_wrong_timestamp(pair,tmp_path):
    normal,speedup=pair;store=Store([normal.root],tmp_path/'review')
    try:
        doc=store.load(normal);data=speedup.table.to_pydict();data['source_timestamp_ns'][2]+=1
        pq.write_table(pa.table(data),speedup.parquet)
        with pytest.raises(ValueError,match='来源时间戳'):project(normal,Episode(speedup.root,0),doc)
    finally:store.lockfile.close()


def test_motion_does_not_invent_semantics(pair):
    normal,_=pair
    segments=motion_candidates(normal)
    assert segments[0]['start_frame']==0 and segments[-1]['end_frame']==normal.length
    assert all(s['label']=='unknown' and not s['reviewed'] for s in segments)


def test_visual_learning_review_gate_and_abstention(pair,tmp_path):
    normal,_=pair;store=Store([normal.root],tmp_path/'review')
    try:
        first=predict(store,normal)
        assert first['method']=='motion_candidates'
        doc=store.load(normal);doc['segments']=[segment(0,30,'grasp_truss'),segment(30,61,'pick_and_place')]
        saved=store.save(normal,doc)
        with pytest.raises(ValueError,match='至少两类'):train(store)
        for s in saved['segments']:s['reviewed']=True
        saved=store.save(normal,saved)
        report=train(store)
        assert report[0]['reference_kind']=='reviewed_examples'
        output=predict(store,normal)
        assert output['in_reference_set'] is True
        assert {s['label'] for s in output['segments']} >= {'grasp_truss','pick_and_place'}
        assert not any(s['reviewed'] for s in output['segments'])
        saved['segments'][0]['label']='lift_truss';store.save(normal,saved)
        with pytest.raises(ValueError,match='重新训练'):predict(store,normal)
    finally:store.lockfile.close()


def test_video_length_mismatch_refuses_features(pair,tmp_path):
    normal,_=pair
    video(normal.cameras['observation.images.cam0'],list(range(60)))
    with pytest.raises(ValueError,match='帧数不同步'):descriptors(normal,tmp_path/'features')


def test_http_token_ranges_and_readonly_preview(pair,tmp_path):
    normal,_=pair
    server=Server(('127.0.0.1',0),[normal.root],tmp_path/'review')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    try:
        bootstrap=json.load(urllib.request.urlopen(base+'/api/bootstrap'))
        assert bootstrap['app']=='subtasks-review'
        url=base+f'/api/video?id={normal.id}&camera=observation.images.cam0'
        with urllib.request.urlopen(urllib.request.Request(url,headers={'Range':'bytes=10-29'})) as r:
            assert r.status==206 and len(r.read())==20
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(url,headers={'Range':'bytes=99999999-'}))
        assert error.value.code==416
        req=urllib.request.Request(base+'/api/save',data=b'{}',headers={'Content-Type':'application/json'})
        with pytest.raises(urllib.error.HTTPError) as error:urllib.request.urlopen(req)
        assert error.value.code==403
        with urllib.request.urlopen(base+'/api/download?id='+normal.id) as response:
            assert 'attachment;' in response.headers['Content-Disposition']
            assert json.load(response)['source']['length']==normal.length
        doc=json.load(urllib.request.urlopen(base+'/api/episode?id='+normal.id))['annotation']
        doc['segments']=[segment(0,normal.length,'idle')]
        req=urllib.request.Request(base+'/api/save',data=json.dumps({'id':normal.id,'annotation':doc}).encode(),
             headers={'Content-Type':'application/json','X-Subtasks-Token':bootstrap['token']})
        assert json.load(urllib.request.urlopen(req))['annotation']['revision']==1
        req=urllib.request.Request(base+'/api/save',data=b'{}',headers={'X-Subtasks-Token':bootstrap['token'],'Origin':'http://evil.test'})
        with pytest.raises(urllib.error.HTTPError) as error:urllib.request.urlopen(req)
        assert error.value.code==403
    finally:server.shutdown();server.server_close();thread.join()


def test_fetch_readonly_verified_and_idempotent(tmp_path, monkeypatch):
    from convest.subtasks import remote
    import hashlib
    payload=b'cached metadata'
    manifest={'dataset':'/remote/dataset','episodes':[0],'files':{'meta/info.json':{
        'sha256':hashlib.sha256(payload).hexdigest(),'size':len(payload),'mtime_ns':1}}}
    monkeypatch.setattr(remote,'query',lambda *a:manifest)
    calls=[]
    def fake_run(argv,**kwargs):
        calls.append(argv)
        stage=Path(argv[-1]);(stage/'meta').mkdir();(stage/'meta/info.json').write_bytes(payload)
    monkeypatch.setattr(remote.subprocess,'run',fake_run)
    result=remote.fetch('28','/remote/dataset',[0],tmp_path/'cache')
    assert result['status']=='verified' and len(calls)==1
    assert remote.fetch('28','/remote/dataset',[0],tmp_path/'cache')['status']=='already_verified'
    assert len(calls)==1
    (tmp_path/'cache/meta/info.json').write_bytes(b'changed')
    with pytest.raises(ValueError,match='缓存内容变化'):remote.fetch('28','/remote/dataset',[0],tmp_path/'cache')


def test_fetch_changed_remote_or_wrong_bytes_never_publishes(tmp_path,monkeypatch):
    from convest.subtasks import remote
    import hashlib
    manifest={'dataset':'/remote/dataset','episodes':[0],'files':{'data/file':{
        'sha256':hashlib.sha256(b'original').hexdigest(),'size':8,'mtime_ns':1}}}
    monkeypatch.setattr(remote,'query',lambda *a:manifest)
    def fake_run(argv,**kwargs):
        stage=Path(argv[-1]);(stage/'data').mkdir();(stage/'data/file').write_bytes(b'bad-data')
    monkeypatch.setattr(remote.subprocess,'run',fake_run)
    with pytest.raises(ValueError,match='校验失败'):remote.fetch('28','/remote/dataset',[0],tmp_path/'cache')
    assert not (tmp_path/'cache').exists()
    assert not list(tmp_path.glob('*.partial-*'))
    calls=iter([manifest,{**manifest,'dataset':'/changed'}])
    monkeypatch.setattr(remote,'query',lambda *a:next(calls))
    def good_run(argv,**kwargs):
        stage=Path(argv[-1]);(stage/'data').mkdir();(stage/'data/file').write_bytes(b'original')
    monkeypatch.setattr(remote.subprocess,'run',good_run)
    with pytest.raises(ValueError,match='远端'):remote.fetch('28','/remote/dataset',[0],tmp_path/'cache')
    assert not (tmp_path/'cache').exists()


def test_remote_rejects_shell_options_before_ssh():
    from convest.subtasks.remote import query
    for host in ['-oProxyCommand=bad','28;uname','28\nwhoami']:
        with pytest.raises(ValueError):query(host,'/dataset')
    with pytest.raises(ValueError):query('28','relative/path')


def test_external_import_cannot_claim_reviewed(pair,tmp_path):
    from convest.subtasks.cli import main
    normal,_=pair
    store=Store([normal.root],tmp_path/'review')
    doc=store.load(normal);store.lockfile.close()
    doc['segments']=[segment(0,normal.length,'grasp_truss',reviewed=True)]
    file=tmp_path/'prediction.json';atomic_json(file,doc)
    main(['import','--root',str(normal.root),'--output',str(tmp_path/'review'),'--id',normal.id,'--file',str(file)])
    store=Store([normal.root],tmp_path/'review')
    try:
        result=store.load(normal)
        assert result['status']=='draft' and not result['segments'][0]['reviewed']
        assert result['origin']=='external_prediction_import'
    finally:store.lockfile.close()


def test_camera_path_escape_refused(pair,tmp_path):
    normal,_=pair
    p=normal.root/'meta/info.json';info=json.loads(p.read_text())
    info['video_path']='../../outside/{video_key}.mp4';atomic_json(p,info)
    with pytest.raises(ValueError,match='escapes'):Episode(normal.root,0)


def jev_response(model='jev-1.14.0',label='grasp_truss',confidence=.9):
    from convest.subtasks.jev import CRITERIA
    return {'model':model,'answers':{'phase':{'type':'choice','choice':label,'confidence':confidence,
           'probabilities':{k:float(k==label) for k in CRITERIA}}},'usage':{'input_tokens':100,'output_tokens':10}}


def test_jev_requires_explicit_key_and_plan_is_offline(pair,tmp_path,monkeypatch):
    from convest.subtasks.jev import annotate,plan
    monkeypatch.delenv('TYPESAFE_API_KEY',raising=False)
    normal,_=pair
    result=plan(normal,tmp_path/'review')
    assert result['network_requests_sent']==0 and result['coarse_requests']==4
    with pytest.raises(ValueError,match='未配置'):annotate(normal,tmp_path/'review')
    assert not list((tmp_path/'review').rglob('*.jpg'))


def test_jev_temporal_context_refinement_and_budget(pair,tmp_path):
    from convest.subtasks.jev import annotate
    import base64
    normal,_=pair
    class FakeClient:
        def __init__(self):self.payloads=[]
        def call(self,payload):
            self.payloads.append(payload)
            anchor=payload['state']['anchor_frame']
            for asset in payload['assets'].values():assert base64.b64decode(asset['data_base64']).startswith(b'\xff\xd8')
            return jev_response(label='grasp_truss' if anchor<30 else 'pick_and_place'),str(anchor)
    client=FakeClient();result=annotate(normal,tmp_path/'review',client=client,max_requests=30)
    assert result['request_count']>4 and result['unrefined_transition_samples']==0
    assert {s['label'] for s in result['segments']}=={'grasp_truss','pick_and_place'}
    assert result['segments'][0]['start_frame']==0 and result['segments'][-1]['end_frame']==61
    assert not any(s['reviewed'] for s in result['segments'])
    assert all(len(p['state']['frames_oldest_first'][0]['cameras'])==3 for p in client.payloads)
    limited=annotate(normal,tmp_path/'limited',client=FakeClient(),max_requests=4)
    assert limited['request_count']==4 and limited['unrefined_transition_samples']>0
    with pytest.raises(ValueError,match='超过'):annotate(normal,tmp_path/'too-small',client=FakeClient(),max_requests=1)


def test_jev_rejects_bad_responses_and_abstains(pair,tmp_path):
    from convest.subtasks.jev import annotate,validate_response
    payload={'model':'jev-1.14.0'}
    for change in ['model','sum','confidence','usage','choice']:
        response=jev_response()
        if change=='model':response['model']='different'
        if change=='sum':response['answers']['phase']['probabilities']['grasp_truss']=.3
        if change=='confidence':response['answers']['phase']['confidence']=True
        if change=='usage':response['usage']['input_tokens']=-1
        if change=='choice':response['answers']['phase']['choice']='lift_truss'
        with pytest.raises(ValueError):validate_response(payload,response)
    class Uncertain:
        def call(self,payload):return jev_response(confidence=.2),'key'
    result=annotate(pair[0],tmp_path/'review',client=Uncertain())
    assert len(result['segments'])==1 and result['segments'][0]['label']=='unknown'


@pytest.mark.parametrize('injected',[False,True])
def test_jev_client_caches_and_keeps_key_out_of_audit(tmp_path,monkeypatch,injected):
    from convest.subtasks import jev
    import base64,io
    if injected:monkeypatch.delenv('TYPESAFE_API_KEY',raising=False)
    else:monkeypatch.setenv('TYPESAFE_API_KEY','not-a-real-secret-for-test')
    calls=[]
    class Opener:
        def open(self,request,timeout):
            calls.append(request)
            assert request.get_header('Authorization')=='Bearer not-a-real-secret-for-test'
            assert request.get_header('Typesafe-danger-zone-images')=='true'
            assert request.full_url=='https://api.typesafe.ai/v1/systemone'
            return io.BytesIO(json.dumps(jev_response()).encode())
    monkeypatch.setattr(jev.urllib.request,'build_opener',lambda *a:Opener())
    payload={'model':'jev-1.14.0','state':'test-image-ref','questions':{},'assets':{
        'test-image-ref':{'type':'image','format':'jpg','data_base64':base64.b64encode(b'fake-test-image').decode()}}}
    client=jev.Client(tmp_path/'api',api_key='not-a-real-secret-for-test' if injected else None)
    first=client.call(payload);second=client.call(payload)
    assert first==second and len(calls)==1
    assert all(b'not-a-real-secret-for-test' not in p.read_bytes() for p in tmp_path.rglob('*') if p.is_file())


def test_jev_model_connection_keeps_secret_private(pair,tmp_path,monkeypatch):
    from convest.subtasks import model_config
    normal,_=pair
    monkeypatch.setenv('SUBTASKS_HTTP_TOKEN','preserved-browser-session')
    monkeypatch.delenv('TYPESAFE_API_KEY',raising=False)
    server=Server(('127.0.0.1',0),[normal.root],tmp_path/'review')
    assert server.token=='preserved-browser-session'
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    secret='private-test-key'
    try:
        monkeypatch.setattr(model_config,'validate_key',lambda key:key if key==secret else (_ for _ in ()).throw(ValueError('invalid key')))
        def connect(key,token=server.token):
            req=urllib.request.Request(base+'/api/models/connect',data=json.dumps({'key':key}).encode(),headers={'X-Subtasks-Token':token,'Content-Type':'application/json'})
            return urllib.request.urlopen(req)
        with pytest.raises(urllib.error.HTTPError) as error:connect(secret,'bad-token')
        assert error.value.code==403 and not server.jev_key
        with connect(secret) as response:
            raw=response.read();assert secret.encode() not in raw
            assert json.loads(raw)=={'enabled':True,'configured':True,'verified':True,'model':'jev-1.14.0'}
        with pytest.raises(urllib.error.HTTPError):connect('bad')
        assert server.jev_key==secret
        bootstrap=json.load(urllib.request.urlopen(base+'/api/bootstrap'))
        assert bootstrap['jev']['configured'] and secret not in json.dumps(bootstrap)
        assert b'type="password"' in urllib.request.urlopen(base+'/models').read()
        assert not any(secret.encode() in p.read_bytes() for p in (tmp_path/'review').rglob('*') if p.is_file())
    finally:server.shutdown();server.server_close();thread.join()


def test_jev_key_validation_uses_only_model_list(monkeypatch):
    import io
    from convest.subtasks import model_config
    class Opener:
        def open(self,request,timeout):
            assert request.full_url=='https://api.typesafe.ai/v1/models'
            assert request.get_header('Authorization')=='Bearer test-key'
            assert request.data is None
            return io.BytesIO(b'{"models":[{"name":"jev-1.14.0"}]}')
    monkeypatch.setattr(model_config.urllib.request,'build_opener',lambda *args:Opener())
    assert model_config.validate_key(' test-key ')=='test-key'
    for key in ('',None,'bad\nkey','x'*4097):
        with pytest.raises(ValueError):model_config.validate_key(key)
    class BadOpener:
        def open(self,*args,**kwargs):
            raise urllib.error.HTTPError('https://api.typesafe.ai/v1/models',401,'secret-must-not-leak',{},None)
    monkeypatch.setattr(model_config.urllib.request,'build_opener',lambda *args:BadOpener())
    with pytest.raises(ValueError) as error:model_config.validate_key('test-key')
    assert 'secret-must-not-leak' not in str(error.value)
