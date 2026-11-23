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


@pytest.mark.parametrize('dated,oss,counted_mode', [(False, False, False), (True, False, False), (True, True, False), (True, True, True)])
def test_gui_native_pipeline_two_groups_separate_targets_and_retry(gui_spec,monkeypatch,dated,oss,counted_mode):
    s=gui_spec;c=s['capture'];t=s['task']
    if oss:
        from convest.gui import oss_remote, oss_transfer
        s['training_delivery'] = {'mode': 'oss', 'final_root': str(Path(t['normal_root']).parents[2]),
                                  'transit_root': str(Path(t['normal_root']).parents[2].parent / 'oss')}
        monkeypatch.setattr(oss_remote, 'require_oss_mount', lambda root: None)
        monkeypatch.setattr(oss_remote, 'require_final_mount', lambda root: None)
        monkeypatch.setattr(oss_transfer, 'call', lambda target, q, **kw: oss_remote.run(q, kw.get('progress', lambda **event: None)))
        def send(target, source, request, pending, total, progress):
            import io
            data = b''.join((Path(source) / name).read_bytes() for name in pending)
            oss_remote.receive({**request, 'paths': pending}, io.BytesIO(data))
        monkeypatch.setattr(oss_transfer, 'stream_files', send)
    if dated:
        for r in c['routes']:r['batch_name_format']='completion-mmdd-v1'
    if counted_mode:
        c['batch_group_size']=2
        for r in c['routes']:r.update(batch_group_size=2,batch_name_format='seal-mmdd-v1')
        s['training']['destination_name_format']='variant-short-v1'
    for i in range(2):bag(c,i,1)
    with capture_context(c) as q:
        ready(c,q)
        for _ in range(4 if counted_mode else 2):assert worker.capture_upload(s,q)
        assert all(not (Path(r['source'])/'episode1').exists() for r in c['routes'])
        for r in c['routes']:
            assert (Path(t['raw_root'])/r['group']/'episode1/metadata.yaml').is_file()
        ledger=json.loads((Path(c['state_root'])/'recordings.json').read_text())
        assert all('/scene/'+r['route'][-1]+'/episode1' in r['remote_original'] for r in ledger['recordings'])
        if not counted_mode:
            daily.maintain(c,q,due(c))
            for _ in range(2):assert worker.capture_upload(s,q)
    with training.context(s['training']) as db:
        training.discover(s['training'],db)
        for _ in range(2):assert training.process_once(s['training'],db)
        before=[dict(r) for r in db.execute('select id,attempts,payload from tasks')]
        transport = worker.oss_transfer if oss else worker
        original=transport.transfer;failed=[False]
        def once(*args,**kwargs):
            if '/speedup/' in str(args[2]) and not failed[0]:failed[0]=True;raise OSError('connection lost')
            return original(*args,**kwargs)
        monkeypatch.setattr(transport,'transfer',once)
        assert worker.training_upload(s,db)
        assert db.execute("select count(*) from tasks where state='upload_retry'").fetchone()[0]==1
        db.execute('update tasks set next_ns=0');db.commit()
        assert worker.training_upload(s,db);assert worker.training_upload(s,db)
        assert all(r['state']=='uploaded' for r in db.execute('select state from tasks'))
        monkeypatch.setattr(training,'recipe',lambda cfg:'changed-code-recipe')
        training.discover(s['training'],db)
        assert db.execute('select count(*) from tasks').fetchone()[0]==2
        assert [dict(r) for r in db.execute('select id,attempts,payload from tasks')]==before
    if oss:
        assert not list(Path(s['training_delivery']['transit_root']).rglob('*.json'))
        assert all(json.loads(p.read_text())['state'] == 'cleaned'
                   for p in Path(s['training_delivery']['final_root']).rglob('*.oss.json'))
    for v in ('normal','speedup'):
        assert len(list(Path(t[v+'_root']).glob('*/.workflow-pairs/*/completion.json')))==2
        for group in ('A','B'):
            info_path=next((Path(t[v+'_root'])/group).glob('*/meta/info.json'))
            if dated:
                import re
                assert re.fullmatch(r'[0-9]{4}_1_1_'+v+('' if counted_mode else '_lerobot'), info_path.parent.parent.name)
                upstream=daily.publisher(c,next(r for r in c['routes'] if r['group']==group)).status()[0]
                assert info_path.parent.parent.name==upstream['name'].replace('_data_lerobot','_'+v+('' if counted_mode else '_lerobot'))
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
    assert all(r['batch_name_format']=='seal-mmdd-v1' and r['batch_group_size']==100 for r in a['capture']['routes'])
    manual_same, manual_spec = config.runtime(settings, {**task, 'auto_upload_paths': False})
    assert manual_same == root
    assert manual_spec['training']['fingerprint'] == a['training']['fingerprint']
    release_root, release_spec = config.runtime(settings, {**task, 'delete_lerobot': True})
    assert release_root == root
    assert release_spec['training']['fingerprint'] == a['training']['fingerprint']
    legacy = copy.deepcopy(settings)
    del legacy['tasks'][0]['delete_lerobot']
    assert config.validate(legacy)['tasks'][0]['delete_lerobot'] is False
    task2={**task,'normal_root':task['normal_root']+'-other'}
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
        with urllib.request.urlopen(base+'/brand-logo.png') as r:
            assert r.headers['Content-Type']=='image/png'
            assert r.read()==(Path(config.__file__).parent/'static/brand-logo.png').read_bytes()
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


@pytest.mark.parametrize('counted_mode',[False,True])
def test_gui_lerobot_release_requires_all_backups_and_recovers(gui_spec, monkeypatch, counted_mode):
    from convest.gui import retention
    from convest.ingest.batches import durable_json
    s = gui_spec; c = s['capture']; cfg = s['training']
    # Build real native normal/speedup pairs and completed remote receipts.
    test_gui_native_pipeline_two_groups_separate_targets_and_retry(s, monkeypatch, True, False, counted_mode)
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


def test_gui_cleanup_pair_retention_remote_failure_recovery_and_restart(gui_spec, monkeypatch):
    from convest.gui import cleanup, retention
    from convest.ingest.batches import durable_json
    s=gui_spec
    test_gui_native_pipeline_two_groups_separate_targets_and_retry(s, monkeypatch, True, False, True)
    monkeypatch.setattr(retention,'verify_backup',local_verify)
    with training.context(s['training']) as db:
        rows=db.execute('SELECT * FROM tasks').fetchall()
        roots=[Path(json.loads(r['payload'])['source']) for r in rows]
        preview=cleanup.sweep(s,db)
        assert all(r['status']=='candidate' for r in preview)
        assert all((p/'normal/videos').exists() for p in roots)
        def unavailable(*args,**kwargs):raise OSError('offline')
        monkeypatch.setattr(retention,'verify_backup',unavailable)
        assert all(r['status']=='retained' for r in cleanup.sweep(s,db,True))
        assert all((p/'normal/videos').exists() for p in roots)
        for path in (Path(s['training']['state_root'])/'retention').glob('*.json'):
            doc=json.loads(path.read_text());doc['retry_after_ns']=0;durable_json(path,doc)
        monkeypatch.setattr(retention,'verify_backup',local_verify)
        # First release the complete source, then interrupt in the pair cleanup.
        for row in rows:retention.release_one(s,db,row,worker.training_destination_name)
        # Old durable receipts alone are insufficient: detect changed remote bytes
        # even after the complete local source was previously released.
        receipt=json.loads((Path(s['training']['state_root'])/'receipts'/(rows[0]['id']+'.json')).read_text())
        remote=next((Path(receipt['variants']['normal']['destination'])/'data').rglob('*.parquet'))
        original_bytes=remote.read_bytes();remote.write_bytes(b'changed remote data')
        failed=cleanup.sweep(s,db,True)
        assert next(r for r in failed if r['id']==rows[0]['id'])['status']=='retained'
        assert (roots[0]/'normal/data').exists()
        remote.write_bytes(original_bytes)
        original=retention.shutil.rmtree
        once=[False]
        def interrupted(path,*args,**kwargs):
            original(path,*args,**kwargs)
            if not once[0]:once[0]=True;raise OSError('power loss')
        interrupted.avoids_symlink_attacks=True
        monkeypatch.setattr(retention.shutil,'rmtree',interrupted)
        first=cleanup.sweep(s,db,True)
        assert any(r['status']=='retained' for r in first)
        monkeypatch.setattr(retention.shutil,'rmtree',original)
        assert all(r['status']=='released' for r in cleanup.sweep(s,db,True))
        for root in roots:
            for variant in ('normal','speedup'):
                assert not (root/variant/'data').exists()
                assert not (root/variant/'videos').exists()
                assert (root/variant/'meta/info.json').exists()
            assert (root/'source.json').exists() and (root/'pipeline.json').exists()
        training.discover(s['training'],db)
        assert db.execute('SELECT count(*) FROM tasks').fetchone()[0]==2
        active=worker.prepare_training(s['training'],db)
        assert training.recipe(active)==training.recipe(s['training'])
        assert not training.process_once(active,db)
        assert not worker.training_upload(s,db)
        assert all(r['status']=='released' for r in cleanup.sweep(s,db,True))


def test_gui_cleanup_policy_and_scan_ownership(tmp_path, monkeypatch):
    from convest.gui import cleanup
    from convest.ingest.batches import durable_json
    monkeypatch.setattr(config,'ROOT',tmp_path/'gui')
    manager=Manager()
    assert cleanup.policy(manager.root)=={'enabled':True}
    assert manager.cleanup_policy({'enabled':True,'keep_days':30})=={'enabled':True}
    assert cleanup.validate({'enabled':False,'keep_days':7})=={'enabled':False}
    for value in ({'enabled':'yes'},{'enabled':None},{}):
        with pytest.raises(ValueError):manager.cleanup_policy(value)
    run=manager.root/'runs/task-0123456789ab' / ('a'*20)
    run.mkdir(parents=True)
    # Historical removed task is still discovered, but unsafe paths cannot be used.
    durable_json(run/'spec.json',{'root':str(run),'capture':{'state_root':'/unrelated'},'training':{}})
    report=cleanup.scan(manager.root,True)
    assert len(report)==1 and report[0]['status']=='retained'
    assert 'ownership' in report[0]['reason']


def test_gui_cleanup_scan_skips_live_worker_locks(tmp_path):
    import fcntl
    from convest.gui import cleanup
    from convest.ingest.batches import durable_json
    run=tmp_path/'runs/task-0123456789ab'/('a'*20)
    for folder in ('capture/state','training/state'):(run/folder).mkdir(parents=True)
    spec={'root':str(run),'capture':{k:str(run/v) for k,v in {'state_root':'capture/state','work_root':'capture/work','local_batch_root':'capture/batches'}.items()},
          'training':{k:str(run/v) for k,v in {'state_root':'training/state','output_root':'training/datasets'}.items()}}
    durable_json(run/'spec.json',spec)
    with (run/'training/state/upload.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        report=cleanup.scan(tmp_path,True)
    assert '运行或收尾' in report[0]['reason']
    assert not (run/'training/state/queue.sqlite3').exists()


def test_gui_cleanup_scans_old_versions_with_original_endpoints(tmp_path, monkeypatch):
    import sqlite3
    from convest.gui import cleanup
    from convest.ingest.batches import durable_json
    seen=[]
    def sweep(spec, db, apply, allow_legacy_cd=False):
        seen.append((spec['training_server'],apply))
        return [{'id':'sample','status':'candidate'}]
    monkeypatch.setattr(cleanup,'sweep',sweep)
    for index in ('a','b'):
        run=tmp_path/'runs/task-0123456789ab'/(index*20)
        for folder in ('capture/state','training/state'):(run/folder).mkdir(parents=True)
        spec={'root':str(run),'capture':{k:str(run/v) for k,v in {'state_root':'capture/state','work_root':'capture/work','local_batch_root':'capture/batches'}.items()},
              'training':{k:str(run/v) for k,v in {'state_root':'training/state','output_root':'training/datasets'}.items()},
              'training_server':{'host':index,'port':28}}
        durable_json(run/'spec.json',spec)
        with sqlite3.connect(run/'training/state/queue.sqlite3'):pass
    assert len(cleanup.scan(tmp_path,False))==2
    assert seen==[({'host':'a','port':28},False),({'host':'b','port':28},False)]


def test_gui_cleanup_background_and_stale_running_report(tmp_path,monkeypatch):
    from convest.gui import cleanup
    from convest.ingest.batches import durable_json
    monkeypatch.setattr(config,'ROOT',tmp_path/'gui')
    manager=Manager();entered=threading.Event();finish=threading.Event()
    def scan(*args):
        entered.set();assert finish.wait(5);return []
    monkeypatch.setattr(cleanup,'scan',scan)
    manager.cleanup_policy({'enabled':True,'keep_days':7})
    durable_json(manager.root/'cleanup-report.json',{'status':'running','apply':True})
    durable_json(manager.root/'cleanup-schedule.json',{'next_ns':time.time_ns()+365*86400*10**9})
    manager.cleanup_tick()
    assert entered.wait(2)
    # Snapshot remains available while remote checks are blocked.
    assert manager.snapshot()['cleanup']['report']['status']=='running'
    with pytest.raises(ValueError,match='运行'):manager.cleanup()
    with pytest.raises(ValueError,match='清理正在运行'):manager.start()
    finish.set()
    until=time.monotonic()+3
    while time.monotonic()<until:
        if manager.snapshot()['cleanup']['report']['status']=='completed':break
        time.sleep(.01)
    assert manager.snapshot()['cleanup']['report']['status']=='completed'
    assert json.loads((manager.root/'cleanup-schedule.json').read_text())['mode']=='immediate-v1'


def raw_archive_spec(s,tmp_path):
    s['root']=str(tmp_path/'gui-run')
    s['task'].update(source=str(Path(s['capture']['routes'][0]['source']).parent),
                     delete_raw=True,include_existing=True)
    return s


def test_gui_cd_direct_raw_upload_skips_all_content_validation_and_never_converts(gui_spec,tmp_path,monkeypatch):
    from convest.gui import raw_archive
    s=raw_archive_spec(gui_spec,tmp_path);c=s['capture']
    monkeypatch.setattr(raw_archive,'transfer',local_transfer)
    monkeypatch.setattr(raw_archive,'verify_backup',local_verify)
    def forbidden(*a,**kw):raise AssertionError('C/D must not validate bag content or convert')
    monkeypatch.setattr(raw_archive,'check_raw_ready',forbidden)
    monkeypatch.setattr(training,'process_once',forbidden)
    originals={}
    for group in ('C','D'):
        bagdir=Path(s['task']['source'])/group/'episode7';bagdir.mkdir(parents=True)
        # Intentionally invalid SQLite and metadata; these are raw files to back up.
        (bagdir/'data.db3').write_bytes(b'raw bytes without content inspection')
        (bagdir/'metadata.yaml').write_text('not valid rosbag metadata')
        originals[group]={p.name:p.read_bytes() for p in bagdir.iterdir()}
    before=c['fingerprint'];now=time.time_ns()
    with capture_context(c) as cap, raw_archive.context(s) as archive:
        raw_archive.scan(s,archive,cap,now)
        assert all((Path(s['task']['source'])/g/'episode7').exists() for g in 'CD')
        for step in (2,4):raw_archive.scan(s,archive,cap,now+step*10**9)
        assert {r['route']:r['state'] for r in archive.rows()}=={'sharpa-C':'deleted','sharpa-D':'deleted'}
        assert not cap.rows()
        assert cap.db.execute('SELECT count(*) FROM capture_batches').fetchone()[0]==0
        assert c['fingerprint']==before
        for g in 'CD':
            assert not (Path(s['task']['source'])/g/'episode7').exists()
            remote=Path(s['task']['raw_root'])/g/'episode7'
            assert {p.name:p.read_bytes() for p in remote.iterdir()}==originals[g]
        # Completed backup remains idempotent; a reused source name is retained.
        newer=Path(s['task']['source'])/'C/episode7';newer.mkdir();(newer/'new').write_text('new recording')
        raw_archive.scan(s,archive,cap,now+6*10**9)
        assert archive.get('sharpa-C','episode7')['state']=='conflict'
        assert (newer/'new').read_text()=='new recording'


def test_gui_cd_remote_failure_and_interrupted_deletion_resume_without_reupload(gui_spec,tmp_path,monkeypatch):
    from convest.gui import raw_archive
    s=raw_archive_spec(gui_spec,tmp_path);s['task']['delete_raw']=False;now=time.time_ns()
    source=Path(s['task']['source'])/'C/episode9';source.mkdir(parents=True);(source/'raw').write_bytes(b'original')
    monkeypatch.setattr(raw_archive,'transfer',local_transfer)
    def offline(*a,**kw):raise OSError('offline verify')
    monkeypatch.setattr(raw_archive,'verify_backup',offline)
    with capture_context(s['capture']) as cap, raw_archive.context(s) as archive:
        raw_archive.scan(s,archive,cap,now)
        raw_archive.scan(s,archive,cap,now+2*10**9)
        assert source.exists()
        assert archive.get('sharpa-C','episode9')['state']=='uploaded'
        s['task']['delete_raw']=True
        raw_archive.scan(s,archive,cap,now+4*10**9)
        assert source.exists() and archive.get('sharpa-C','episode9')['state']=='uploaded'
        monkeypatch.setattr(raw_archive,'verify_backup',local_verify)
        def no_reupload(*a,**kw):raise AssertionError('Already uploaded; only resume deletion')
        monkeypatch.setattr(raw_archive,'transfer',no_reupload)
        original=raw_archive.shutil.rmtree;once=[False]
        def interrupted(path,*a,**kw):
            original(path,*a,**kw)
            if not once[0]:once[0]=True;raise OSError('power loss after deletion')
        interrupted.avoids_symlink_attacks=True
        monkeypatch.setattr(raw_archive.shutil,'rmtree',interrupted)
        raw_archive.scan(s,archive,cap,now+6*10**9)
        assert archive.get('sharpa-C','episode9')['state']=='deleting'
        monkeypatch.setattr(raw_archive.shutil,'rmtree',original)
        raw_archive.scan(s,archive,cap,now+8*10**9)
        assert archive.get('sharpa-C','episode9')['state']=='deleted'
        assert not source.exists()


def test_gui_ab_raw_backup_waits_for_existing_conversion_and_deletion(gui_spec,tmp_path,monkeypatch):
    from convest.gui import raw_archive
    s=raw_archive_spec(gui_spec,tmp_path);c=s['capture'];source=bag(c,0,21)
    monkeypatch.setattr(raw_archive,'transfer',local_transfer)
    monkeypatch.setattr(raw_archive,'verify_backup',local_verify)
    now=time.time_ns()
    with capture_context(c) as cap, raw_archive.context(s) as archive:
        raw_archive.scan(s,archive,cap,now)
        raw_archive.scan(s,archive,cap,now+2*10**9)
        assert archive.get('sharpa-A','episode21')['state']=='uploaded'
        assert source.exists()  # not yet converted: backup cannot destroy conversion input
        ready(c,cap)
        assert cap.get('sharpa-A','episode21')['state']=='converted'
        assert worker.capture_upload(s,cap)
        assert not source.exists()
        raw_archive.scan(s,archive,cap,now+4*10**9)
        assert archive.get('sharpa-A','episode21')['state']=='deleted'
        assert cap.db.execute('SELECT count(*) FROM capture_batches').fetchone()[0]==1


def test_gui_cd_active_recording_and_keep_raw_switch_are_respected(gui_spec,tmp_path,monkeypatch):
    from convest.gui import raw_archive
    s=raw_archive_spec(gui_spec,tmp_path);s['task']['delete_raw']=False
    source=Path(s['task']['source'])/'D/episode4';source.mkdir(parents=True)
    state=source/'collection_state.json';state.write_text(json.dumps({'state':'recording','finalized':False}))
    (source/'raw').write_bytes(b'data')
    monkeypatch.setattr(raw_archive,'transfer',local_transfer)
    monkeypatch.setattr(raw_archive,'verify_backup',local_verify)
    now=time.time_ns()
    with capture_context(s['capture']) as cap, raw_archive.context(s) as archive:
        raw_archive.scan(s,archive,cap,now)
        raw_archive.scan(s,archive,cap,now+2*10**9)
        assert not (Path(s['task']['raw_root'])/'D/episode4').exists()
        state.write_text(json.dumps({'state':'finalized','finalized':True,'failures':['ignored quality']}))
        raw_archive.scan(s,archive,cap,now+4*10**9)
        raw_archive.scan(s,archive,cap,now+6*10**9)
        assert archive.get('sharpa-D','episode4')['state']=='uploaded'
        assert source.exists()  # switch off: backup only, no deletion


def test_gui_cd_existing_different_remote_never_overwrites_or_deletes(gui_spec,tmp_path,monkeypatch):
    from convest.gui import raw_archive
    s=raw_archive_spec(gui_spec,tmp_path);now=time.time_ns()
    source=Path(s['task']['source'])/'C/episode3';source.mkdir(parents=True);(source/'raw').write_bytes(b'new')
    remote=Path(s['task']['raw_root'])/'C/episode3';remote.mkdir(parents=True);(remote/'raw').write_bytes(b'old')
    monkeypatch.setattr(raw_archive,'transfer',local_transfer)
    monkeypatch.setattr(raw_archive,'verify_backup',local_verify)
    with capture_context(s['capture']) as cap, raw_archive.context(s) as archive:
        raw_archive.scan(s,archive,cap,now)
        raw_archive.scan(s,archive,cap,now+2*10**9)
        assert archive.get('sharpa-C','episode3')['state']=='retry'
        assert (source/'raw').read_bytes()==b'new'
        assert (remote/'raw').read_bytes()==b'old'


def test_gui_excluded_ab_is_backed_up_and_released_without_fake_batch(gui_spec,tmp_path,monkeypatch):
    from convest.gui import raw_archive
    s=raw_archive_spec(gui_spec,tmp_path);c=s['capture'];source=bag(c,0,22);now=time.time_ns()
    monkeypatch.setattr(raw_archive,'transfer',local_transfer)
    monkeypatch.setattr(raw_archive,'verify_backup',local_verify)
    with capture_context(c) as cap, raw_archive.context(s) as archive:
        raw_archive.scan(s,archive,cap,now)
        raw_archive.scan(s,archive,cap,now+2*10**9)
        row=archive.get('sharpa-A','episode22');proof=json.loads(row['proof'])
        cap.put('sharpa-A','episode22','excluded-source','excluded',proof={'snapshot':proof['snapshot'],'files':proof['files']})
        raw_archive.scan(s,archive,cap,now+4*10**9)
        assert archive.get('sharpa-A','episode22')['state']=='deleted'
        assert not source.exists()
        assert cap.db.execute('SELECT count(*) FROM capture_batches').fetchone()[0]==0


def test_completed_upload_cleanup_is_immediate_and_cleans_owned_work(gui_spec,monkeypatch):
    from convest.gui import cleanup,retention
    s=gui_spec;c=s['capture']
    test_gui_native_pipeline_two_groups_separate_targets_and_retry(s,monkeypatch,True,False,True)
    monkeypatch.setattr(retention,'verify_backup',local_verify)
    with training.context(s['training']) as db:
        row=db.execute('SELECT * FROM tasks LIMIT 1').fetchone();src=json.loads(row['spec'])
        with __import__('sqlite3').connect(Path(c['state_root'])/'queue.sqlite3') as cap:
            p=json.loads(cap.execute('SELECT payload FROM recordings WHERE route=? AND batch_id=?',(src['route'],src['batch_id'])).fetchone()[0])
        work=Path(c['work_root'])/p['route']/p['episode']/p['identity']/'attempt-0001'
        (work/'cleaned').mkdir(exist_ok=True);(work/'cleaned/leftover').write_bytes(b'large leftover')
        (work/'dataset').mkdir(exist_ok=True);(work/'dataset/leftover').write_bytes(b'large duplicate')
        assert all(r['status']=='candidate' for r in cleanup.sweep(s,db))
        result=cleanup.sweep(s,db,True)
        assert all(r['status']=='released' for r in result),result
        assert not (work/'cleaned').exists() and not (work/'dataset').exists()
        assert (work/'manifest.json').exists()
        assert db.execute("SELECT count(*) FROM tasks WHERE state='uploaded'").fetchone()[0]==2


def test_old_cd_releases_work_with_only_raw_backup_and_retires_training(gui_spec,tmp_path,monkeypatch):
    from convest.gui import cleanup,cleanup_work,retention,raw_archive
    s=raw_archive_spec(gui_spec,tmp_path);c=s['capture'];c['routes']=c['routes'][:1]
    s['root']=str(tmp_path/'gui/runs/task-0123456789ab'/('a'*20))
    route=c['routes'][0]
    route.update(id='sharpa-C',group='C',source=str(Path(route['source']).parent/'C'),output=str(Path(c['local_batch_root'])/'sharpa/C'))
    route['conversion']['source_root']=route['source']
    Path(route['source']).mkdir(parents=True)
    s['task']['delete_raw']=False;c['delete_raw_after_upload']=False
    c['route_targets']={route['id']:{'raw':str(Path(s['task']['raw_root'])/'C')}}
    monkeypatch.setattr(retention,'verify_backup',local_verify)
    source=bag(c,0,7)
    with capture_context(c) as cap:
        ready(c,cap)
        assert worker.capture_upload(s,cap)  # raw only; the batch is not sealed yet
        daily.maintain(c,cap,due(c))
        batch=cap.db.execute('SELECT * FROM capture_batches').fetchone()
        raw_row=cap.db.execute('SELECT * FROM uploads WHERE raw_done=1').fetchone()
        raw_receipt=Path(c['state_root'])/'receipts'/(raw_row['id']+'.json')
        src={'route':route['id'],'robot':'sharpa','group':'C','batch_id':batch['batch_id'],'name':batch['name'],
             'source':batch['generation'],'recipe':'old-cd-recipe'}
    with training.context(s['training']) as db:
        assert len(cleanup_work.unqueued_cd(s,[]))==1
        assert cleanup.sweep(s,db,allow_legacy_cd=True)[0]['status']=='candidate'
        tid=digest(['legacy-cd',src])
        db.execute('INSERT INTO tasks(id,spec,state,updated_ns) VALUES(?,?,?,?)',(tid,json.dumps(src),'pending',time.time_ns()));db.commit()
        # Current training code must leave old C/D conversions and uploads alone.
        active=worker.prepare_training(s['training'],db)
        assert training.recipe(active)==training.recipe(s['training'])
        assert not training.process_once(active,db)
        assert not worker.training_upload(s,db)
        raw_receipt.unlink()
        missing=cleanup.sweep(s,db,True,allow_legacy_cd=True)
        assert missing[0]['status']=='retained' and (Path(src['source'])/'videos').exists()
        # The new raw-only version can supply the missing original backup proof
        # for an older C/D conversion, without uploading any derived dataset.
        newer=copy.deepcopy(s);newer['root']=str(tmp_path/'gui/runs/task-0123456789ab'/('b'*20))
        monkeypatch.setattr(raw_archive,'transfer',local_transfer)
        monkeypatch.setattr(raw_archive,'verify_backup',local_verify)
        from convest.ingest.batches import durable_json
        with capture_context(c) as cap,raw_archive.context(newer) as archive:
            durable_json(Path(newer['root'])/'spec.json',newer)
            now=time.time_ns();raw_archive.scan(newer,archive,cap,now);raw_archive.scan(newer,archive,cap,now+2*10**9)
            assert archive.get('sharpa-C','episode7')['state']=='uploaded'
        result=cleanup.sweep(s,db,True,allow_legacy_cd=True)
        assert result[0]['status']=='released',result
        assert not (Path(src['source'])/'data').exists() and not (Path(src['source'])/'videos').exists()
        assert (Path(src['source'])/'meta/info.json').exists() and source.exists()
        assert db.execute('SELECT state FROM tasks WHERE id=?',(tid,)).fetchone()[0]=='archived_raw_only'
        assert not Path(s['task']['normal_root']).exists()
        assert not (Path(s['task']['lerobot_root'])/'C'/src['name']).exists()
        with capture_context(c) as cap:
            assert not worker.capture_upload(s,cap)
            assert cap.db.execute("SELECT state FROM uploads WHERE dataset_done=0 AND episode LIKE 'batch-%'").fetchone()[0]=='archived_raw_only'


def test_automatic_cleanup_new_completion_does_not_wait_for_timer(gui_spec,tmp_path,monkeypatch):
    import sqlite3
    from convest.gui import cleanup
    from convest.ingest.batches import durable_json
    s=gui_spec;s['root']=str(tmp_path/'gui/runs/task-0123456789ab'/('a'*20))
    root=Path(s['root']).parents[2]
    durable_json(root/'cleanup-policy.json',{'enabled':True,'keep_days':365})
    called=[]
    monkeypatch.setattr(cleanup,'sweep',lambda *a,**kw:called.append(True) or [])
    with capture_context(s['capture']),training.context(s['training']) as db:
        cleanup.automatic(s,db);cleanup.automatic(s,db)
        assert len(called)==1
        db.execute('INSERT INTO tasks(id,spec,state,updated_ns) VALUES(?,?,?,?)',('new','{}','uploaded',time.time_ns()));db.commit()
        cleanup.automatic(s,db)
        assert len(called)==2  # previous minute timer is still in the future
        assert cleanup.policy(root)=={'enabled':True}


def test_gui_port_in_use_preserves_original_error(tmp_path,monkeypatch):
    monkeypatch.setattr(config,'ROOT',tmp_path/'ui')
    server=Server(('127.0.0.1',0))
    try:
        with pytest.raises(OSError):Server(('127.0.0.1',server.server_port))
    finally:server.server_close()


def test_failed_cd_work_without_batch_cleans_after_raw_only_backup(gui_spec,tmp_path,monkeypatch):
    from convest.gui import cleanup,raw_archive,retention
    from convest.ingest.inputs import check_input,snapshot
    from convest.ingest.batches import durable_json
    s=raw_archive_spec(gui_spec,tmp_path);c=s['capture'];c['routes']=c['routes'][:1]
    s['root']=str(tmp_path/'gui/runs/task-0123456789ab'/('a'*20));s['task']['delete_raw']=False
    route=c['routes'][0];route.update(id='sharpa-D',group='D',source=str(Path(route['source']).parent/'D'),output=str(Path(c['local_batch_root'])/'sharpa/D'))
    route['conversion']['source_root']=route['source'];Path(route['source']).mkdir(parents=True)
    source=bag(c,0,31);proof=check_input(source,route,snapshot(source))
    with capture_context(c):pass
    attempt=Path(c['work_root'])/route['id']/source.name/proof['content_identity']/'attempt-0001'
    durable_json(attempt/'manifest.json',{'owner':'convert-data-job-worker','config':route['conversion']})
    for name in ('cleaned','dataset'):
        (attempt/name).mkdir();(attempt/name/'leftover').write_bytes(b'failed conversion data')
    monkeypatch.setattr(raw_archive,'transfer',local_transfer);monkeypatch.setattr(raw_archive,'verify_backup',local_verify)
    monkeypatch.setattr(retention,'verify_backup',local_verify)
    newer=copy.deepcopy(s);newer['root']=str(tmp_path/'gui/runs/task-0123456789ab'/('b'*20))
    with capture_context(c) as cap,raw_archive.context(newer) as archive:
        cap.put(route['id'],source.name,proof['content_identity'],'failed',proof=proof)
        durable_json(Path(newer['root'])/'spec.json',newer)
        now=time.time_ns();raw_archive.scan(newer,archive,cap,now);raw_archive.scan(newer,archive,cap,now+2*10**9)
        assert archive.get(route['id'],source.name)['state']=='uploaded'
        assert cap.db.execute('SELECT count(*) FROM capture_batches').fetchone()[0]==0
    with training.context(s['training']) as db:
        result=cleanup.sweep(s,db,True,allow_legacy_cd=True)
        assert len(result)==1 and result[0]['status']=='released',result
        assert not (attempt/'cleaned').exists() and not (attempt/'dataset').exists()
        assert (attempt/'manifest.json').exists() and source.exists()
