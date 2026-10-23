"""GUI workflow integration: native routes, durable control and verified SSH publication protocol."""
import copy
import json
from pathlib import Path
import shutil
import threading
import time
import urllib.request
import urllib.error
import pytest
from convest.config import digest
from convest.capture.worker import context as capture_context
from convest.capture import daily
from convest.training import worker as training
from convest.gui import config, worker, remote_agent
from convest.gui.server import Server
from convest.gui.manager import Manager
from test_capture_daily import daily_cfg,due
from test_capture import capture,ready
from test_ingest import service,bag


def local_transfer(target,source,destination,files,identity,progress=lambda **kw:None,guard=lambda:None):
    token=digest({'destination':str(destination),'identity':identity,'files':files})
    request={'destination':str(destination),'token':token,'files':files}
    guard();r=remote_agent.run({**request,'operation':'prepare'})
    if r['status']!='existing':shutil.copytree(source,r['staging'],dirs_exist_ok=True)
    guard();r=remote_agent.run({**request,'operation':'commit'});progress(phase='completed')
    return {**r,'token':token}


def local_verify(target,destination,files,identity):
    token=digest({'destination':str(destination),'identity':identity,'files':files})
    return remote_agent.run({'operation':'verify','destination':str(destination),'token':token,'files':files})


@pytest.fixture
def gui_spec(daily_cfg,tmp_path,monkeypatch):
    monkeypatch.setattr(worker,'transfer',local_transfer);monkeypatch.setattr(worker,'verify_backup',local_verify)
    cfg=daily_cfg;cfg['routes']=cfg['routes'][:2]
    task={'robot':'sharpa','raw_root':str(tmp_path/'remote/rosbag/sharpa/scene'),
          'lerobot_root':str(tmp_path/'remote/lerobot/sharpa/scene'),
          'normal_root':str(tmp_path/'training/tomato_sharpa/normal/scene'),
          'speedup_root':str(tmp_path/'training/tomato_sharpa/speedup/scene')}
    cfg['route_targets']={r['id']:{'raw':str(Path(task['raw_root'])/r['group'])} for r in cfg['routes']}
    train={'capture':cfg,'state_root':str(tmp_path/'training-state'),'output_root':str(tmp_path/'training-output'),
           'fingerprint':digest(task),'workers':1,'cmax':3.,'min_free_gib':0,'retry_seconds':1}
    return {'capture':cfg,'training':train,'task':task,'data_server':{},'training_server':{}}


def test_gui_native_pipeline_two_groups_separate_targets_and_retry(gui_spec,monkeypatch):
    s=gui_spec;c=s['capture'];t=s['task']
    for i in range(2):bag(c,i,1)
    with capture_context(c) as q:
        ready(c,q)
        for _ in range(2):assert worker.capture_upload(s,q)
        assert all(not (Path(r['source'])/'episode1').exists() for r in c['routes'])
        for r in c['routes']:
            assert (Path(t['raw_root'])/r['group']/'episode1/metadata.yaml').is_file()
        ledger=json.loads((Path(c['state_root'])/'recordings.json').read_text())
        assert all('/scene/'+r['route'][-1]+'/episode1' in r['remote_original'] for r in ledger['recordings'])
        daily.maintain(c,q,due(c))
        for _ in range(2):assert worker.capture_upload(s,q)
    with training.context(s['training']) as db:
        training.discover(s['training'],db)
        for _ in range(2):assert training.process_once(s['training'],db)
        before=[dict(r) for r in db.execute('select id,attempts,payload from tasks')]
        original=worker.transfer;failed=[False]
        def once(*args,**kwargs):
            if '/speedup/' in str(args[2]) and not failed[0]:failed[0]=True;raise OSError('connection lost')
            return original(*args,**kwargs)
        monkeypatch.setattr(worker,'transfer',once)
        assert worker.training_upload(s,db)
        assert db.execute("select count(*) from tasks where state='upload_retry'").fetchone()[0]==1
        db.execute('update tasks set next_ns=0');db.commit()
        assert worker.training_upload(s,db);assert worker.training_upload(s,db)
        assert all(r['state']=='uploaded' for r in db.execute('select state from tasks'))
        assert [dict(r) for r in db.execute('select id,attempts,payload from tasks')]==before
    for v in ('normal','speedup'):
        assert len(list(Path(t[v+'_root']).glob('*/.workflow-pairs/*/completion.json')))==2
        for group in ('A','B'):
            info=json.loads(next((Path(t[v+'_root'])/group).glob('*/meta/info.json')).read_text())
            assert info['features']['observation.state']['shape']==[116]
            assert info['features']['action']['shape']==[58]
            assert info['total_episodes']==1


def test_remote_conflict_never_deletes_original(gui_spec):
    s=gui_spec;c=s['capture'];p=bag(c,0,2)
    dest=Path(s['task']['raw_root'])/'A/episode2';dest.mkdir(parents=True);(dest/'foreign').write_text('keep')
    with capture_context(c) as q:
        ready(c,q);worker.capture_upload(s,q)
        assert p.exists() and (dest/'foreign').read_text()=='keep'
        assert q.db.execute('select raw_done from uploads').fetchone()[0]==0
        assert q.db.execute('select raw_deleted_ns from recordings').fetchone()[0] is None


def test_remote_agent_create_ab_reject_traversal_and_verify_existing(tmp_path):
    result=remote_agent.run({'operation':'mkdir','parent':str(tmp_path),'name':'场景一','with_ab':True})
    assert (Path(result['path'])/'A').is_dir() and (Path(result['path'])/'B').is_dir()
    for name in ('../escape','a/b','..','a\nb'):
        with pytest.raises(ValueError):remote_agent.run({'operation':'mkdir','parent':str(tmp_path),'name':name})
    bad=tmp_path/'link';bad.symlink_to(tmp_path/'场景一',target_is_directory=True)
    with pytest.raises(ValueError):remote_agent.run({'operation':'mkdir','parent':str(bad),'name':'nested'})


def test_settings_duplicate_roots_and_paths_have_independent_versions(tmp_path,monkeypatch):
    root_test=config.WORKSPACE/'outputs'/('gui-test-'+__import__('uuid').uuid4().hex)
    monkeypatch.setattr(config,'ROOT',root_test)
    settings=config.load_settings();task=settings['tasks'][0]
    settings=config.validate(settings);root,a=config.runtime(settings,task)
    same,b=config.runtime(settings,{**task,'name':'renamed'})
    assert root==same and a['capture']['fingerprint']==b['capture']['fingerprint']
    task2={**task,'normal_root':str(tmp_path/'other-normal')}
    changed,c=config.runtime(settings,task2);assert changed!=root
    bad=copy.deepcopy(settings);bad['tasks'].append({**task,'id':config.new_task()['id']})
    with pytest.raises(ValueError):config.validate(bad)
    shutil.rmtree(root_test)


def test_http_csrf_and_local_directory_browser(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'ROOT',tmp_path/'ui')
    server=Server(('127.0.0.1',0));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    try:
        with urllib.request.urlopen(base+'/api/bootstrap') as r:value=json.load(r)
        body=json.dumps({'path':str(tmp_path)}).encode()
        request=urllib.request.Request(base+'/api/browse',data=body,headers={'Content-Type':'application/json'})
        with pytest.raises(urllib.error.HTTPError) as e:urllib.request.urlopen(request)
        assert e.value.code==403
        request.add_header('X-Workflow-Token',value['token'])
        with urllib.request.urlopen(request) as r:assert json.load(r)['path']==str(tmp_path)
        request.add_header('Origin','https://example.com')
        with pytest.raises(urllib.error.HTTPError) as e:urllib.request.urlopen(request)
        assert e.value.code==403
    finally:server.shutdown();server.server_close();thread.join()


def test_manager_restart_reattach_graceful_stop_and_resume(monkeypatch):
    root_test=config.WORKSPACE/'outputs'/('gui-control-test-'+__import__('uuid').uuid4().hex)
    monkeypatch.setattr(config,'ROOT',root_test)
    manager=Manager();tid=manager.settings['tasks'][0]['id']
    def wait_for(expected,timeout=15):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            value=manager.state(tid)
            if value['state']==expected:return value
            time.sleep(.1)
        raise AssertionError(manager.state(tid))
    try:
        manager.start(tid);first=wait_for('running')
        assert Manager().state(tid)['pid']==first['pid']
        with pytest.raises(ValueError):manager.save(manager.settings)
        manager.stop(tid);stopped=wait_for('stopped')
        assert all(c['exit_code']==0 for c in stopped['children'].values())
        old_root=manager.current(tid)
        manager.start(tid);second=wait_for('running')
        assert second['pid']!=first['pid'] and manager.current(tid)==old_root
        manager.stop(tid);wait_for('stopped')
    finally:
        manager.stop(tid)
        if manager.state(tid)['state']=='stopped':shutil.rmtree(root_test)
