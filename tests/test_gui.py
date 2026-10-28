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


@pytest.mark.parametrize('dated', [False, True])
def test_gui_native_pipeline_two_groups_separate_targets_and_retry(gui_spec,monkeypatch,dated):
    s=gui_spec;c=s['capture'];t=s['task']
    if dated:
        for r in c['routes']:r['batch_name_format']='completion-mmdd-v1'
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
        monkeypatch.setattr(training,'recipe',lambda cfg:'changed-code-recipe')
        training.discover(s['training'],db)
        assert db.execute('select count(*) from tasks').fetchone()[0]==2
        assert [dict(r) for r in db.execute('select id,attempts,payload from tasks')]==before
    for v in ('normal','speedup'):
        assert len(list(Path(t[v+'_root']).glob('*/.workflow-pairs/*/completion.json')))==2
        for group in ('A','B'):
            info_path=next((Path(t[v+'_root'])/group).glob('*/meta/info.json'))
            if dated:
                import re
                assert re.fullmatch(r'[0-9]{4}_1_1_'+v+'_lerobot', info_path.parent.parent.name)
                upstream=daily.publisher(c,next(r for r in c['routes'] if r['group']==group)).status()[0]
                assert info_path.parent.parent.name==upstream['name'].replace('_data_lerobot','_'+v+'_lerobot')
                assert (Path(t['lerobot_root'])/group/upstream['name']/'meta/info.json').exists()
            info=json.loads(info_path.read_text())
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
    assert all(r['batch_name_format']=='completion-mmdd-v1' for r in a['capture']['routes'])
    manual_same, manual_spec = config.runtime(settings, {**task, 'auto_upload_paths': False})
    assert manual_same == root
    assert manual_spec['training']['fingerprint'] == a['training']['fingerprint']
    release_root, release_spec = config.runtime(settings, {**task, 'delete_lerobot': True})
    assert release_root == root
    assert release_spec['training']['fingerprint'] == a['training']['fingerprint']
    legacy = copy.deepcopy(settings)
    del legacy['tasks'][0]['delete_lerobot']
    assert config.validate(legacy)['tasks'][0]['delete_lerobot'] is False
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
        with urllib.request.urlopen(base+'/brand-logo.gif') as r:
            assert r.headers['Content-Type']=='image/gif'
            assert r.read()==(Path(config.__file__).parent/'static/brand-logo.gif').read_bytes()
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


def test_upload_path_mode_save_reload_and_legacy_migration(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path / 'gui')
    manager = Manager()
    settings = copy.deepcopy(manager.settings)
    task = settings['tasks'][0]
    assert task['auto_upload_paths'] is True
    task.update(project='grape', robot='wuji', scene='场景二')
    # Server saving enforces the same path rule as the live editor.
    generated = config.default_upload_paths(task)
    saved = manager.save(settings)
    assert all(saved['tasks'][0][k] == v for k, v in generated.items())
    assert saved == Manager().settings
    original_server = copy.deepcopy(settings['training_server'])
    settings['training_server']['port'] = 26
    saved = manager.save(settings)
    assert Manager().settings['training_server'] == {**original_server, 'port': 26}
    task['auto_upload_paths'] = False
    for key in generated:
        task[key] = str(tmp_path / key / '手选父目录')
    manual = {key: task[key] for key in generated}
    task.update(project='lemon', scene='新场景')
    saved = manager.save(settings)
    assert all(saved['tasks'][0][k] == v for k, v in manual.items())
    assert Manager().settings == saved
    # Missing legacy flag must preserve previously chosen custom directories.
    legacy = copy.deepcopy(saved)
    del legacy['tasks'][0]['auto_upload_paths']
    assert config.validate(legacy)['tasks'][0]['auto_upload_paths'] is False
    task['auto_upload_paths'] = True
    saved = manager.save(settings)
    expected = config.default_upload_paths(task)
    assert all(saved['tasks'][0][k] == v for k, v in expected.items())
    del saved['tasks'][0]['auto_upload_paths']
    assert config.validate(saved)['tasks'][0]['auto_upload_paths'] is True
    settings['tasks'][0]['auto_upload_paths'] = 'true'
    with pytest.raises(ValueError, match='任务开关无效'):
        config.validate(settings)


def test_gui_lerobot_release_requires_all_backups_and_recovers(gui_spec, monkeypatch):
    from convest.gui import retention
    from convest.ingest.batches import durable_json
    s = gui_spec; c = s['capture']; cfg = s['training']
    # Build real native normal/speedup pairs and completed remote receipts.
    test_gui_native_pipeline_two_groups_separate_targets_and_retry(s, monkeypatch, True)
    monkeypatch.setattr(retention, 'verify_backup', local_verify)
    with training.context(cfg) as db:
        rows = db.execute('SELECT * FROM tasks').fetchall()
        sources = [Path(json.loads(r['spec'])['source']) for r in rows]
        retention.release_complete_batches(s, db, worker.training_destination_name)
        assert all((p/'data').exists() for p in sources)  # default off
        s['task']['delete_lerobot'] = True
        def retry_now():
            for f in (Path(cfg['state_root'])/'retention').glob('*.json'):
                report=json.loads(f.read_text());report['retry_after_ns']=0;durable_json(f,report)
        # A failed/missing upstream completed upload keeps all payloads.
        import sqlite3
        with sqlite3.connect(Path(c['state_root'])/'queue.sqlite3') as cap:
            cap.execute("UPDATE uploads SET dataset_done=0 WHERE dataset_done=1")
        retention.release_complete_batches(s, db, worker.training_destination_name)
        assert all((p/'data').exists() for p in sources)
        with sqlite3.connect(Path(c['state_root'])/'queue.sqlite3') as cap:
            cap.execute("UPDATE uploads SET dataset_done=1 WHERE state='completed' AND episode LIKE 'batch-%'")
        retry_now()
        # Fresh remote verification failure also keeps all local complete data.
        def unavailable(*args, **kwargs): raise OSError('remote unavailable')
        monkeypatch.setattr(retention, 'verify_backup', unavailable)
        retention.release_complete_batches(s, db, worker.training_destination_name)
        assert all((p/'data').exists() for p in sources)
        monkeypatch.setattr(retention, 'verify_backup', local_verify)
        retry_now()
        # Crash after a quarantined tree is removed but before final state write.
        original_rmtree=retention.shutil.rmtree
        once=[False]
        def interrupted(path, *args, **kwargs):
            original_rmtree(path, *args, **kwargs)
            if not once[0]:once[0]=True;raise OSError('simulated power loss')
        interrupted.avoids_symlink_attacks=True
        monkeypatch.setattr(retention.shutil, 'rmtree', interrupted)
        retention.release_complete_batches(s, db, worker.training_destination_name)
        monkeypatch.setattr(retention.shutil, 'rmtree', original_rmtree)
        retry_now()
        retention.release_complete_batches(s, db, worker.training_destination_name)
        reports=[json.loads(f.read_text()) for f in (Path(cfg['state_root'])/'retention').glob('*.json')]
        assert len(reports)==2 and all(r['status']=='released' for r in reports), reports
        assert all(not (p/'data').exists() and not (p/'videos').exists() for p in sources)
        assert all((p/'meta/info.json').exists() and (p/'ingest/generation.json').exists() for p in sources)
        # Restarts continue discovery, catalogue recovery and upload idempotently.
        for row in rows:
            spec=json.loads(row['spec']);training.sealed(cfg,spec)
        training.discover(cfg,db)
        assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==2
        for route in c['routes']:
            assert daily.publisher(c,route).status()[0]['state']=='sealed'
        for row in rows:
            pair=Path(json.loads(row['payload'])['source'])
            assert (pair/'normal/meta/info.json').exists() and (pair/'speedup/meta/info.json').exists()
        retention.release_complete_batches(s, db, worker.training_destination_name)


def test_gui_release_old_generations_preserves_next_batch(gui_spec, monkeypatch):
    from convest.gui import retention
    s=gui_spec;c=s['capture'];cfg=s['training']
    c['routes']=c['routes'][:1]
    monkeypatch.setattr(retention,'verify_backup',local_verify)
    with capture_context(c) as queue:
        bag(c,0,1);ready(c,queue)
        bag(c,0,2);ready(c,queue)
        daily.maintain(c,queue,due(c))
        while worker.capture_upload(s,queue): pass
    with training.context(cfg) as db:
        training.discover(cfg,db);assert training.process_once(cfg,db)
        assert worker.training_upload(s,db)
        row=db.execute('SELECT * FROM tasks').fetchone()
        source=Path(json.loads(row['spec'])['source'])
        parent=Path(json.loads((source/'ingest/generation.json').read_text())['parent'])
        assert (parent/'videos').exists()
        s['task']['delete_lerobot']=True
        retention.release_complete_batches(s,db,worker.training_destination_name)
        report=json.loads((Path(cfg['state_root'])/'retention'/(row['id']+'.json')).read_text())
        assert report['status']=='released', report
        assert len(report['trees'])==4  # data/videos from both immutable generations
        assert not (parent/'videos').exists() and not (source/'videos').exists()
    with capture_context(c) as queue:
        bag(c,0,3);ready(c,queue)
        batches=daily.publisher(c,c['routes'][0]).status()
        assert len(batches)==2 and batches[0]['state']=='sealed' and batches[1]['state']=='active'


def test_gui_lerobot_release_option_is_saved_and_defaults_off(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path/'ui')
    manager=Manager();settings=copy.deepcopy(manager.settings)
    assert settings['tasks'][0]['delete_lerobot'] is False
    settings['tasks'][0]['delete_lerobot']=True
    manager.save(settings)
    assert Manager().settings['tasks'][0]['delete_lerobot'] is True
    settings['tasks'][0]['delete_lerobot']='yes'
    with pytest.raises(ValueError, match='任务开关无效'):
        config.validate(settings)
