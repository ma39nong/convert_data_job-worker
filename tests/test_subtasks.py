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


def test_doubao_fixed_labels_complete_timeline_and_rejects_invalid():
    from convest.subtasks.doubao import parse_segments
    def response(rows):return {'status':'completed','output':[{'content':[{'type':'output_text','text':json.dumps({'segments':rows})}]}]}
    good=[{'end_seconds':2.5,'label':'grasp_truss','confidence':.9},{'end_seconds':6.1,'label':'lift_truss','confidence':.8}]
    rows=parse_segments(response(good),61,10)
    assert [(s['start_frame'],s['end_frame'],s['coarse'],s['reviewed']) for s in rows]==[(0,25,'拿起番茄',False),(25,61,'拿起番茄',False)]
    assert parse_segments(response([{**good[0],'confidence':.2},good[1]]),61,10)[0]['label']=='unknown'
    for bad in [[],[good[0]],[{**good[0],'end_seconds':float('nan')},good[1]],
                [{**good[0],'label':'new_unlisted_action'},good[1]],
                [{**good[0],'confidence':True},good[1]],
                [good[0],{**good[1],'end_seconds':2.4}]]:
        with pytest.raises(ValueError):parse_segments(response(bad),61,10)


def test_doubao_reads_credential_without_executing_source(tmp_path):
    from convest.subtasks.doubao import credential_from_source
    path=tmp_path/'existing.py'
    path.write_text("raise RuntimeError('must not execute')\nDEFAULT_ARK_API_KEY = 'private-key'\n")
    assert credential_from_source(path)=='private-key'
    path.write_text("DEFAULT_ARK_API_KEY = __import__('os').getenv('KEY')")
    with pytest.raises(ValueError):credential_from_source(path)


def test_doubao_montage_uses_identical_source_frames_for_three_cameras(pair,tmp_path):
    from convest.subtasks.doubao import make_montage
    normal,_=pair;path=tmp_path/'composite.mp4'
    manifest=make_montage(normal,path,20,41)
    assert manifest['source_frame_indices']==list(range(20,41,2))
    with av.open(str(path)) as container:
        frames=list(container.decode(video=0))
    assert len(frames)==11
    for i,frame in enumerate(frames):
        rgb=frame.to_ndarray(format='rgb24')
        a,b,c=rgb[180,240],rgb[180,720],rgb[540,240]
        assert np.max(np.abs(a.astype(int)-b))<=5 and np.max(np.abs(a.astype(int)-c))<=5
        assert np.max(rgb[540,720])<10
        assert (a[0]>a[1])==(i<5)  # source frame 30 switches red to green


def test_doubao_client_cache_no_secret_in_logs_and_no_retry(tmp_path,monkeypatch):
    from convest.subtasks import doubao
    import io
    path=tmp_path/'video.mp4';path.write_bytes(b'fake-video')
    calls=[]
    class Opener:
        def open(self,request,timeout):
            calls.append(request)
            payload=json.loads(request.data)
            assert request.full_url==doubao.ENDPOINT and request.get_header('Authorization')=='Bearer secret-test-key'
            assert payload['store'] is False and any(c['type']=='input_video' for c in payload['input'][0]['content'])
            return io.BytesIO(json.dumps({'model':doubao.MODEL,'status':'completed'}).encode())
    monkeypatch.setattr(doubao.urllib.request,'build_opener',lambda *args:Opener())
    client=doubao.Client(tmp_path/'api','secret-test-key')
    a=client.call(path,'prompt',5);b=client.call(path,'prompt',5)
    assert len(calls)==1 and a[2] is False and b[2] is True
    assert all(b'secret-test-key' not in p.read_bytes() for p in tmp_path.rglob('*') if p.is_file())
    (tmp_path/'api/STOP').write_text('stop')
    with pytest.raises(ValueError,match='STOP'):client.call(path,'different prompt',5)
    assert len(calls)==1


def test_doubao_stitches_context_and_budget_before_network(pair,tmp_path,monkeypatch):
    from convest.subtasks import doubao
    normal,_=pair;calls=[]
    def montage(ep,path,start,end):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'video')
        return {'sample_fps':5,'source_start_frame':start,'source_end_frame':end,'sha256':sha(path)}
    monkeypatch.setattr(doubao,'make_montage',montage)
    class Client:
        def call(self,path,prompt,fps):
            start,end=map(int,path.stem.split('_')[1:])
            # One consistent boundary in original video at frame 30.
            rows=[]
            if start<30:rows.append({'end_seconds':(min(30,end)-start)/10,'label':'grasp_truss','confidence':.9})
            if end>30:rows.append({'end_seconds':(end-start)/10,'label':'lift_truss','confidence':.8})
            calls.append(path)
            return {'output_text':json.dumps({'segments':rows}),'usage':{}},str(len(calls)),False
    with pytest.raises(ValueError,match='上限'):
        doubao.annotate_unconstrained(normal,tmp_path/'result',chunk_seconds=5,max_requests=1,client=Client())
    assert not calls
    result=doubao.annotate_unconstrained(normal,tmp_path/'result',chunk_seconds=5,max_requests=2,client=Client())
    assert [(r['start_frame'],r['end_frame'],r['label']) for r in result['segments']]==[(0,30,'grasp_truss'),(30,61,'lift_truss')]
    assert result['network_requests']==2 and all(not s['reviewed'] for s in result['segments'])


def test_long_model_job_allows_manual_save_and_revision_conflict(pair,tmp_path):
    normal,_=pair
    server=Server(('127.0.0.1',0),[normal.root],tmp_path/'review')
    started=threading.Event();finish=threading.Event();saved=threading.Event()
    old=server.store.load(normal)
    def remote():
        started.set();assert finish.wait(5)
        with pytest.raises(ValueError,match='其他窗口'):
            server.store.save(normal,old,origin='model')
        return {'conflict_detected':True}
    try:
        server.submit(remote,lock_store=False)
        assert started.wait(2)
        def manual():
            server.store.save(normal,{**old,'segments':[segment(0,normal.length,'grasp_truss')]})
            saved.set()
        thread=threading.Thread(target=manual);thread.start()
        assert saved.wait(2), 'manual save blocked behind network job'
        finish.set();thread.join()
    finally:
        finish.set();server.server_close()
    assert json.loads(server.store.path(normal.id).read_text())['origin']=='manual'


def test_doubao_reference_images_only_other_reviewed_recordings(pair,tmp_path):
    from convest.subtasks.doubao import reference_examples
    normal,speedup=pair
    other=dataset(tmp_path/'other',recording='different-recording')
    store=Store([normal.root,speedup.root,other.root],tmp_path/'review')
    try:
        doc=store.load(normal)
        store.save(normal,{**doc,'segments':[segment(0,30,'grasp_truss',reviewed=True),segment(30,61,'lift_truss',reviewed=True)]})
        assert reference_examples(store,normal)==[]  # same episode cannot supply answers
        assert reference_examples(store,speedup)==[] # same recording variant also excluded
        examples=reference_examples(store,other)
        assert {e['label'] for e in examples}=={'grasp_truss','lift_truss'}
        assert all(e['source_id']==normal.id and e['revision']==1 and e['data_url'].startswith('data:image/jpeg;base64,') for e in examples)
    finally:store.lockfile.close()


def test_tomato_template_rejects_repeated_rotation_and_wrong_fine_order():
    from convest.subtasks.template import validate_template
    labels=['grasp_truss','lift_truss','grasp_fruit','pick_and_place','return_truss','rotate_stem',
            'grasp_truss','lift_truss','grasp_fruit','pick_and_place','return_stem']
    make=lambda labels:[segment(i,i+1,label) for i,label in enumerate(labels)]
    assert len(validate_template(make(labels)))==7
    with pytest.raises(ValueError):validate_template(make(labels[:6]+labels))
    with pytest.raises(ValueError):validate_template(make(labels[:2]+['pick_and_place','grasp_fruit']+labels[4:]))
    with pytest.raises(ValueError):validate_template(make(labels+['stop']))


def test_boundary_refinement_only_selects_present_source_frames_and_marks_uncertain():
    from convest.subtasks.template import parse_boundaries
    task=[{'id':2,'frames':[1300,1301,1302]}]
    response=lambda frame,confidence:{'output_text':json.dumps({'boundaries':[{'id':2,'first_after_frame':frame,'confidence':confidence}]})}
    assert parse_boundaries(response(1301,.9),task)[2]['frame']==1301
    assert parse_boundaries(response(None,.3),task)[2]['needs_review']
    assert parse_boundaries(response(None,None),task)[2]['needs_review']
    assert parse_boundaries(response(1300,.9),task)[2]['needs_review'] # no prior BEFORE image
    for bad in [response(1299,.9),response(1301,float('nan')),response(True,.8)]:
        with pytest.raises(ValueError):parse_boundaries(bad,task)


def test_whole_template_and_source_frame_passes_preserve_grammar(pair,tmp_path,monkeypatch):
    from convest.subtasks import template,doubao
    import base64
    normal,_=pair
    labels=['grasp_truss','lift_truss','grasp_fruit','pick_and_place','return_truss','rotate_stem',
            'grasp_truss','lift_truss','grasp_fruit','pick_and_place','return_stem']
    def montage(ep,path,start,end,**kwargs):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'video')
        return {'sample_fps':1,'sha256':sha(path)}
    monkeypatch.setattr(doubao,'make_montage',montage)
    monkeypatch.setattr(template,'images_at',lambda ep,indices,**kwargs:[{'frame':i,'sha256':'fake','data_url':'data:image/jpeg;base64,'+base64.b64encode(b'image').decode()} for i in indices])
    class Client:
        def call(self,path,prompt,fps):
            if 'LOCAL FINE PASS' in prompt:
                lo,hi=map(int,path.stem.split('_')[-2:]);duration=(hi-lo)/10
                rows=[{'end_seconds':duration/2,'label':'grasp_fruit','confidence':.9,'attachment':'attached','retention':'held','event':'approach_attached'}, {'end_seconds':duration,'label':'pick_and_place','confidence':.9,'attachment':'detached','retention':'held','event':'detached_held'}]
                return {'output_text':json.dumps({'segments':rows})},'local',False
            assert 'Exactly these' in prompt and 'actually detached' in prompt
            rows=[{'end_seconds':(i+1)*.5 if i<10 else 6.1,'label':label,'confidence':.9} for i,label in enumerate(labels)]
            return {'output_text':json.dumps({'segments':rows})},'global',False
        def call_images(self,groups,prompt):
            rows=[{'id':g['task']['id'],'first_after_frame':g['task']['candidate_frame'],'confidence':.9, 'states':[{'frame':f,'attachment':'attached' if f<g['task']['candidate_frame'] else 'detached','retention':'held'} for f in g['task']['frames']]} for g in groups]
            return {'output_text':json.dumps({'boundaries':rows})},str(rows),False
    result=doubao.annotate(normal,tmp_path/'out',client=Client())
    assert [s['label'] for s in result['segments']]==labels
    assert result['rotation_count']==1 and result['request_count']==10
    assert all(not s['reviewed'] for s in result['segments'])
    assert result['segments'][-1]['end_frame']==normal.length


def test_apply_template_proposal_checks_source_revision_and_clears_review(pair,tmp_path):
    from convest.subtasks.template import POLICY
    normal,_=pair;server=Server(('127.0.0.1',0),[normal.root],tmp_path/'review')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    labels=['grasp_truss','lift_truss','grasp_fruit','pick_and_place','return_truss','rotate_stem',
            'grasp_truss','lift_truss','grasp_fruit','pick_and_place','return_stem']
    proposal={'source':normal.identity(),'method':'doubao_template_source_frames_v1','policy':POLICY,
              'segments':[segment(i*5,(i+1)*5 if i<10 else normal.length,label,reviewed=True) for i,label in enumerate(labels)]}
    def apply(proposal,revision=0):
        request=urllib.request.Request(base+'/api/apply-proposal',data=json.dumps({'id':normal.id,'proposal':proposal,'revision':revision,'replace':True}).encode(),headers={'X-Subtasks-Token':server.token})
        return json.load(urllib.request.urlopen(request))
    try:
        with pytest.raises(urllib.error.HTTPError):apply({**proposal,'source':{}})
        assert server.store.load(normal)['revision']==0
        saved=apply(proposal)['annotation']
        assert saved['status']=='draft' and saved['origin']==proposal['method']
        assert not any(s['reviewed'] for s in saved['segments'])
        with pytest.raises(urllib.error.HTTPError):apply(proposal)
        assert server.store.load(normal)['revision']==1
    finally:server.shutdown();server.server_close();thread.join()


def test_left_preparation_motion_cue_requires_stationary_prefix(monkeypatch):
    from types import SimpleNamespace
    from convest.subtasks.template import left_motion_candidate
    from convest.training.adatempo import lerobot_io
    q=np.zeros((100,7));q[30:,0]=np.arange(1,71)*.0002
    ep=SimpleNamespace(fps=30,length=100,table=None,info={'features':{'observation.state':{'names':[f'left_fr3_joint{i}.position' for i in range(1,8)]}}})
    monkeypatch.setattr(lerobot_io,'values',lambda *a:q)
    assert left_motion_candidate(ep,35)==30
    q[:,0]=np.arange(100)*.001
    assert left_motion_candidate(ep,35) is None


def test_boundary_image_requests_use_bounded_direct_output_and_do_not_cache_incomplete(tmp_path,monkeypatch):
    from convest.subtasks import doubao
    import io
    count=[]
    class Opener:
        def open(self,request,timeout):
            payload=json.loads(request.data);count.append(payload)
            assert payload['thinking']=={'type':'disabled'} and 'reasoning' not in payload
            assert payload['max_output_tokens']==2000
            return io.BytesIO(json.dumps({'model':doubao.MODEL,'status':'incomplete','incomplete_details':{'reason':'length'}}).encode())
    monkeypatch.setattr(doubao.urllib.request,'build_opener',lambda *a:Opener())
    client=doubao.Client(tmp_path/'api','private-key')
    groups=[{'task':{'id':0},'assets':[{'frame':100,'sha256':'fake','data_url':'data:image/jpeg;base64,ZmFrZQ=='}]}]
    with pytest.raises(ValueError,match='未完成'):client.call_images(groups,'find first detached frame')
    assert len(count)==1 and not list((tmp_path/'api/responses').glob('*.json'))
    assert len(list((tmp_path/'api/raw-incomplete').glob('*.json')))==1
    for p in (tmp_path/'api/requests').glob('*.json'):
        assert b'private-key' not in p.read_bytes() and b'data:image' not in p.read_bytes()


def test_harvest_grammar_keeps_failed_attempts_without_inventing_placement():
    from convest.subtasks.template import validate_template
    labels=['grasp_truss','lift_truss','grasp_fruit','fruit_drop','harvest_retry','grasp_fruit',
            'return_truss','rotate_stem','grasp_truss','lift_truss','grasp_fruit',
            'harvest_uncertain','return_stem']
    assert len(validate_template([segment(i,i+1,label) for i,label in enumerate(labels)]))==7


def test_attachment_evidence_rejects_grip_pulling_freefall_and_missing_observations():
    from convest.subtasks.harvest import parse_proven_boundaries
    tasks=[{'id':1,'event':'first_detached_frame','frames':[100,101,102]}]
    def result(states):
        return parse_proven_boundaries({'output_text':json.dumps({'boundaries':[{
            'id':1,'first_after_frame':101,'confidence':.99,'states':states}]})},tasks)[1]
    observations=[{'frame':f,'attachment':'attached' if f==100 else 'detached','retention':'held'} for f in [100,101,102]]
    assert not result(observations)['needs_review']
    assert result([{**s,'attachment':'attached'} for s in observations])['needs_review']
    assert result([{**s,'retention':'falling'} for s in observations])['needs_review']
    assert result(observations[1:])['needs_review']
    assert result([])['frame'] is None
    with pytest.raises(ValueError):result(observations+[observations[0]])


def test_long_attached_attempt_and_drop_do_not_force_a_place():
    from convest.subtasks.harvest import parse_harvest
    rows=[{'end_seconds':30,'label':'grasp_fruit','confidence':.9,'attachment':'attached','retention':'held','event':'approach_attached'},
          {'end_seconds':32,'label':'fruit_drop','confidence':.9,'attachment':'detached','retention':'falling','event':'drop'},
          {'end_seconds':35,'label':'harvest_retry','confidence':.9,'attachment':'occluded','retention':'empty','event':'failed_recovery'}]
    result,evidence=parse_harvest({'output_text':json.dumps({'segments':rows})},1050,30)
    assert [s['label'] for s in result]==['grasp_fruit','fruit_drop','harvest_retry']
    assert result[0]['end_frame']==900 and not any(s['reviewed'] for s in result)
    rows[1]['label']='pick_and_place' # model claims placement but describes a fall
    result,_=parse_harvest({'output_text':json.dumps({'segments':rows})},1050,30)
    assert result[1]['label']=='harvest_uncertain'


def test_drop_retry_labels_survive_normal_speedup_source_mapping(pair,tmp_path):
    normal,speedup=pair
    doc={'source':normal.identity(),'revision':1,'status':'draft','outcome':'unknown',
         'segments':[segment(0,20,'grasp_fruit'),segment(20,24,'fruit_drop'),
                     segment(24,30,'harvest_retry'),segment(30,61,'grasp_fruit')]}
    result=project(normal,speedup,doc)
    assert [s['label'] for s in result['segments']]==['grasp_fruit','fruit_drop','harvest_retry','grasp_fruit']
    assert all(s['coarse']=='摘取番茄' and not s['reviewed'] for s in result['segments'])


def test_missing_video_tail_is_uncertain_instead_of_extending_success():
    from convest.subtasks.harvest import parse_harvest
    row={'end_seconds':9.5,'label':'pick_and_place','confidence':.9,
         'attachment':'detached','retention':'held','event':'detached_held'}
    result,evidence=parse_harvest({'output_text':json.dumps({'segments':[row]})},300,30)
    assert result[0]['end_frame']==285 and result[1]['label']=='harvest_uncertain'
    row['end_seconds']=8
    with pytest.raises(ValueError,match='结尾'):parse_harvest({'output_text':json.dumps({'segments':[row]})},300,30)


def test_user_attached_frame_is_a_hard_negative_for_detachment():
    from convest.subtasks.harvest import parse_proven_boundaries
    task=[{'id':0,'event':'first_detached_frame','frames':[945,946,947], 'known_attached_frames':[946]}]
    rows={'boundaries':[{'id':0,'first_after_frame':946,'confidence':.99,'states':[
        {'frame':945,'attachment':'attached','retention':'held'},
        {'frame':946,'attachment':'detached','retention':'held'},
        {'frame':947,'attachment':'detached','retention':'held'}]}]}
    result=parse_proven_boundaries({'output_text':json.dumps(rows)},task)[0]
    assert result['needs_review'] and result['frame'] is None and result['human_attachment_conflict']


def test_bad_model_frame_abstains_without_losing_an_independent_valid_boundary():
    from convest.subtasks.harvest import review_boundaries
    tasks=[{'id':0,'event':'left_return_approach','frames':[10,11,12]},
           {'id':1,'event':'left_return_approach','frames':[20,21,22]}]
    rows={'boundaries':[{'id':0,'first_after_frame':13,'confidence':.9},
                        {'id':1,'first_after_frame':21,'confidence':.9}]}
    result=review_boundaries({'output_text':json.dumps(rows)},tasks)
    assert result[0]['frame'] is None and result[0]['needs_review'] and result[0]['schema_error']
    assert result[1]['frame']==21 and not result[1]['needs_review']
    rows['boundaries'][0]['id']=8
    assert all(x['needs_review'] for x in review_boundaries({'output_text':json.dumps(rows)},tasks).values())


def test_drop_audit_rejects_controlled_tray_release(pair,tmp_path,monkeypatch):
    from convest.subtasks.harvest import verify_drop_claims
    from convest.subtasks import template
    import base64
    normal,_=pair
    monkeypatch.setattr(template,'images_at',lambda ep,frames,**kwargs:[{'frame':f,'data_url':'data:image/jpeg;base64,'+base64.b64encode(b'image').decode(),'sha256':'fake'} for f in frames])
    class Client:
        def call_images(self,groups,prompt):
            return {'output_text':json.dumps({'target_drop':True,'same_target':True,'release':'controlled_release','held_frame':10,'falling_frame':22,'reason':'normal release into intended tray'})},'audit',False
    spans=[segment(0,20,'grasp_fruit'),segment(20,25,'fruit_drop'),segment(25,61,'grasp_fruit')]
    result,checks,records=verify_drop_claims(normal,spans,tmp_path/'audit',Client())
    assert result[1]['label']=='harvest_uncertain' and not checks[0]['verified_drop']
    with pytest.raises(ValueError,match='预算'):verify_drop_claims(normal,spans,tmp_path/'audit',Client(),max_requests=0)


def test_later_user_attached_witness_rejects_same_attempt_drop_and_recovery(pair,tmp_path):
    from convest.subtasks.harvest import verify_drop_claims
    normal,_=pair
    class Client:
        def call_images(self,*args):raise AssertionError('human contradiction needs no paid request')
    spans=[segment(0,20,'grasp_fruit'),segment(20,25,'fruit_drop'),segment(25,30,'harvest_retry'),segment(30,61,'grasp_fruit')]
    result,checks,records=verify_drop_claims(normal,spans,tmp_path,Client(),known_attached=[27])
    assert not records and not any(s['label']=='fruit_drop' for s in result)
    assert next(s for s in result if s['start_frame']<=27<s['end_frame'])['label']=='grasp_fruit'


def test_visible_truss_fall_is_a_drop_even_if_left_hand_did_not_hold_it(pair,tmp_path,monkeypatch):
    from convest.subtasks.harvest import verify_drop_claims
    from convest.subtasks import template
    import base64
    normal,_=pair
    monkeypatch.setattr(template,'images_at',lambda ep,frames,**kwargs:[{'frame':f,'data_url':'data:image/jpeg;base64,'+base64.b64encode(b'image').decode(),'sha256':'fake'} for f in frames])
    class Client:
        def call_images(self,groups,prompt):
            return {'output_text':json.dumps({'observed_drop':True,'origin':'truss','target_drop':False,'same_target':False,'release':'uncontrolled','held_frame':10,'falling_frame':22})},'fall',False
    spans=[segment(0,20,'grasp_fruit'),segment(20,25,'fruit_drop'),segment(25,61,'grasp_fruit')]
    result,checks,records=verify_drop_claims(normal,spans,tmp_path,Client())
    assert result[1]['label']=='fruit_drop' and checks[0]['verified_drop']


def test_training_behavior_records_are_independent_and_survive_saves(pair,tmp_path):
    normal,_=pair;store=Store([normal.root],tmp_path/'review')
    try:
        original=store.load(normal)
        original['segments']=[segment(0,normal.length,'grasp_fruit',reviewed=True)]
        original['review_events']=[{'start_frame':20,'end_frame':30,'kind':'empty_hand_place','status':'confirmed','origin':'manual','note':'empty before reaching tray'}]
        original['training_review']={'decision':'pending','note':'人工稍后决定'}
        saved=store.save(normal,original)
        assert saved['status']=='reviewed' and saved['training_review']['decision']=='pending'
        assert saved['outcome']=='unknown' and saved['review_events'][0]['kind']=='empty_hand_place'
        next_doc={k:v for k,v in saved.items() if k not in ('review_events','training_review')}
        again=store.save(normal,next_doc)
        assert again['review_events']==saved['review_events'] and again['training_review']==saved['training_review']
        bad={**again,'review_events':[{**again['review_events'][0],'end_frame':normal.length+1}]}
        with pytest.raises(ValueError):store.save(normal,bad)
    finally:store.lockfile.close()


def test_behavior_model_cannot_confirm_itself_or_exclude_training(pair):
    from convest.subtasks.behavior import parse_candidates,merge_candidates
    normal,_=pair
    response={'output_text':json.dumps({'events':[{'start_seconds':1,'end_seconds':2,'kind':'drop_then_resume','evidence':'dropped then direct regrasp','status':'confirmed','training_review':{'decision':'exclude'}}]})}
    events=parse_candidates(response,normal)
    assert events[0]['status']=='pending' and events[0]['origin']=='model' and 'training_review' not in events[0]
    reviewed=[{**events[0],'status':'dismissed'}]
    assert merge_candidates(reviewed,events,normal.length)==reviewed
    response['output_text']=json.dumps({'events':[{'start_seconds':1,'end_seconds':90,'kind':'empty_hand_place'}]})
    with pytest.raises(ValueError):parse_candidates(response,normal)


def test_behavior_events_project_with_observation_frames_but_need_rechecking(pair):
    normal,speedup=pair
    doc={'source':normal.identity(),'revision':2,'status':'draft','segments':[segment(0,61,'grasp_fruit')],
         'review_events':[{'id':'x','start_frame':20,'end_frame':30,'kind':'truss_takeover_return','status':'confirmed','origin':'manual','note':'left took whole truss'},
                          {'id':'short','start_frame':1,'end_frame':2,'kind':'empty_hand_place'},
                          {'id':'false-alarm','start_frame':30,'end_frame':40,'kind':'empty_hand_place','status':'dismissed'}],
         'training_review':{'decision':'exclude'}}
    result=project(normal,speedup,doc)
    assert len(result['review_events'])==1
    assert result['review_events'][0]['start_frame']==10 and result['review_events'][0]['end_frame']==15
    assert result['review_events'][0]['status']=='pending' and result['collapsed_review_events']==['short']
    assert 'training_review' not in result


def test_behavior_scan_api_preserves_labels_and_manual_train_choice(pair,tmp_path,monkeypatch):
    from convest.subtasks import behavior
    normal,_=pair;server=Server(('127.0.0.1',0),[normal.root],tmp_path/'review',doubao_key='test-only')
    doc=server.store.load(normal);doc['segments']=[segment(0,61,'grasp_fruit',reviewed=True)];doc['training_review']={'decision':'keep'}
    before=server.store.save(normal,doc)
    def scan(ep,output,**kwargs):
        return {'policy':behavior.POLICY,'events':behavior.validate_events([{'start_frame':20,'end_frame':30,'kind':'truss_takeover_return','origin':'model','status':'pending'}],ep.length),'warning':'candidate only'}
    monkeypatch.setattr(behavior,'scan',scan)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base=f'http://127.0.0.1:{server.server_port}'
    try:
        request=urllib.request.Request(base+'/api/scan-behavior',data=json.dumps({'id':normal.id,'revision':before['revision']}).encode(),headers={'X-Subtasks-Token':server.token})
        job=json.load(urllib.request.urlopen(request))['job']
        import time
        for _ in range(100):
            value=json.load(urllib.request.urlopen(base+'/api/job?id='+job))
            if value['state'] in {'completed','failed'}:break
            time.sleep(.01)
        assert value['state']=='completed'
        saved=value['result']['annotation']
        assert saved['segments']==before['segments'] and saved['training_review']==before['training_review']
        assert saved['review_events'][0]['status']=='pending' and saved['outcome']==before['outcome']
    finally:server.shutdown();server.server_close();thread.join()
