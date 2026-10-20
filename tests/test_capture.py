"""Capture-side four-route conversion and resilient transfer integration."""
import json
from pathlib import Path
import time
import pytest
import yaml
from convest.capture.config import load
from convest.capture.worker import context, scan
from convest.capture import transfer
from convest.jobs import checksum
from convest.verify import verify
from test_ingest import service, bag


@pytest.fixture
def capture(service, tmp_path):
    cfg = {**service, 'remote_raw_root': str(tmp_path/'uploaded-raw'),
           'remote_dataset_root': str(tmp_path/'uploaded-data'), 'require_mount': None}
    for key in ('remote_raw_root','remote_dataset_root'):
        Path(cfg[key]).mkdir()
    cfg['routes']=[{**r,'history':{'mode':'all','episodes':[]}} for r in cfg['routes']]
    return cfg


def ready(cfg, queue):
    scan(cfg,queue)
    return scan(cfg,queue,now_ns=time.time_ns()+2_000_000_000)


def test_four_routes_real_conversion_verified_upload_and_restart_dedup(capture):
    cfg=capture;originals={}
    for i in range(4):
        source=bag(cfg,i,11)
        originals[source]={p.name:checksum(p) for p in source.iterdir()}
    with context(cfg) as q:
        result=ready(cfg,q)
        assert result['counts']=={'converted':4}
        for _ in range(4):assert transfer.upload_once(cfg,q)
        assert not transfer.upload_once(cfg,q)
        assert q.db.execute("select count(*) from uploads where state='completed'").fetchone()[0]==4
        assert len(list(Path(cfg['remote_dataset_root']).glob('.capture-transfer/**/*.json')))==4
        import runpy
        audit=runpy.run_path(str(__import__('convest.config',fromlist=['WORKSPACE']).WORKSPACE/'scripts/validate_capture_trial.py'))['validate']
        assert audit(cfg,rehash=True)['count']==4
        for route in cfg['routes']:
            dataset=Path(cfg['remote_dataset_root'])/route['robot']/route['group']/'episode11_data_lerobot'
            assert verify(dataset,full_video=True)['episodes']==1
            raw=Path(cfg['remote_raw_root'])/route['robot']/route['group']/'episode11'
            source=Path(route['source'])/'episode11'
            assert {p.name:checksum(p) for p in raw.iterdir()}==originals[source]
    with context(cfg) as q:
        scan(cfg,q,now_ns=time.time_ns()+4_000_000_000)
        assert all(row['attempts']==1 for row in q.rows())
        assert q.db.execute('select count(*) from uploads').fetchone()[0]==4
        assert not transfer.upload_once(cfg,q)
    for source,files in originals.items():assert {p.name:checksum(p) for p in source.iterdir()}==files


def test_offline_upload_keeps_converted_data_and_recovers_without_reconversion(capture,monkeypatch):
    cfg=capture;bag(cfg,2,1)
    with context(cfg) as q:
        ready(cfg,q)
        completed=list(Path(cfg['work_root']).rglob('local_complete.json'))
        before={p:p.read_bytes() for p in completed}
        real=transfer.check_mount
        monkeypatch.setattr(transfer,'check_mount',lambda cfg: (_ for _ in ()).throw(OSError('offline')))
        transfer.upload_once(cfg,q)
        assert q.get('wuji-A','episode1')['state']=='converted'
        assert q.db.execute('select state from uploads').fetchone()[0]=='retry'
        monkeypatch.setattr(transfer,'check_mount',real)
        q.db.execute('update uploads set next_ns=0');q.db.commit()
        transfer.upload_once(cfg,q)
        assert q.db.execute('select state from uploads').fetchone()[0]=='completed'
        scan(cfg,q,now_ns=time.time_ns()+4_000_000_000)
        assert q.get('wuji-A','episode1')['attempts']==1
        assert all(p.read_bytes()==data for p,data in before.items())


def test_partial_file_resume_and_interrupted_commit_reconciliation(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'data').write_bytes(b'abcd'*1000)
    files=transfer.manifest(source)
    destination=tmp_path/'remote'/'episode1'
    staging=destination.parent/'.uploading'/'token';staging.mkdir(parents=True)
    (staging/'data').write_bytes((source/'data').read_bytes()[:777])
    inode=(staging/'data').stat().st_ino
    transfer.copy_tree(source,destination,files,'token')
    assert (destination/'data').read_bytes()==(source/'data').read_bytes()
    assert (destination/'data').stat().st_ino==inode
    transfer.copy_tree(source,destination,files,'token')  # commit succeeded before local state
    assert not staging.exists()


def test_conflicting_remote_data_is_preserved(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'data').write_bytes(b'correct')
    target=tmp_path/'target';target.mkdir();(target/'data').write_bytes(b'foreign')
    with pytest.raises(ValueError,match='hash differs'):
        transfer.copy_tree(source,target,transfer.manifest(source),'token')
    assert (target/'data').read_bytes()==b'foreign'


def test_corrupted_transferred_data_is_not_committed(tmp_path,monkeypatch):
    source=tmp_path/'source';source.mkdir();(source/'data').write_bytes(b'correct')
    target=tmp_path/'target'
    original=transfer.os.fsync
    def corrupt(fd):
        original(fd)
        (tmp_path/'.uploading/token/data').write_bytes(b'corrupt')
    monkeypatch.setattr(transfer.os,'fsync',corrupt)
    with pytest.raises(ValueError,match='hash differs'):
        transfer.copy_tree(source,target,transfer.manifest(source),'token')
    assert not target.exists()


def test_mount_loss_never_creates_local_fallback(tmp_path,monkeypatch):
    root=tmp_path/'missing-remote'
    cfg={'require_mount':str(root),'remote_raw_root':str(root/'raw'),'remote_dataset_root':str(root/'data')}
    monkeypatch.setattr(transfer.os.path,'ismount',lambda _:False)
    with pytest.raises(OSError,match='mount is absent'):
        transfer.check_mount(cfg)
    assert not root.exists()


def test_changed_raw_after_conversion_blocks_upload(capture):
    cfg=capture;source=bag(cfg,2,1)
    with context(cfg) as q:
        ready(cfg,q)
        p=source/'collection_state.json';p.write_text(p.read_text()+'\n')
        transfer.upload_once(cfg,q)
        assert q.db.execute('select state from uploads').fetchone()[0]=='conflict'
        assert not transfer.upload_once(cfg,q)
        assert not list(Path(cfg['remote_raw_root']).rglob('*.db3'))
        assert not list(Path(cfg['remote_dataset_root']).glob('.capture-transfer/**/*.json'))


def test_invalid_recording_is_not_converted_or_uploaded(capture):
    cfg=capture;source=bag(cfg,2,1)
    (source/'collection_state.json').unlink()
    with context(cfg) as q:
        ready(cfg,q)
        assert q.get('wuji-A','episode1')['state']=='waiting_input'
        assert not transfer.upload_once(cfg,q)
        assert not list(Path(cfg['work_root']).rglob('local_complete.json'))


def test_manifest_path_escape_and_symlink_are_rejected(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'data').write_bytes(b'ok')
    with pytest.raises(ValueError,match='Unsafe'):
        transfer.copy_tree(source,tmp_path/'target',[{'path':'../escape','size':0,'sha256':''}],'token')
    other=tmp_path/'other';other.mkdir();target=tmp_path/'target';target.symlink_to(other)
    with pytest.raises(ValueError,match='symlink'):
        transfer.copy_tree(source,target,transfer.manifest(source),'token')


def test_capture_config_fingerprints_destinations_and_rejects_overlap(tmp_path):
    path=tmp_path/'config.yaml'
    raw={'schema_version':1,'state_root':'outputs/capture-test-config/state','work_root':'outputs/capture-test-config/work',
         'remote_raw_root':str(tmp_path/'raw'),'remote_dataset_root':str(tmp_path/'data'),
         'initial_history':{'mode':'none'},'routes':[{'robot':'wuji','group':'A','source':str(tmp_path/'input')}]}
    path.write_text(yaml.safe_dump(raw));one=load(path)
    raw['remote_dataset_root']=str(tmp_path/'other');path.write_text(yaml.safe_dump(raw));two=load(path)
    assert one['fingerprint']!=two['fingerprint']
    raw['remote_dataset_root']=str(tmp_path/'input'/'output');path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError,match='overlap'):load(path)


def test_crash_after_conversion_commit_reenqueues_without_running_converter(capture,monkeypatch):
    import convest.capture.worker as worker
    cfg=capture;bag(cfg,2,1)
    with context(cfg) as q:
        ready(cfg,q)
        row=q.get('wuji-A','episode1')
        q.db.execute('delete from uploads');q.db.commit()
        q.put('wuji-A','episode1',row['identity'],'processing')
    monkeypatch.setattr(worker,'run',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('must not reconvert')))
    with context(cfg) as q:
        scan(cfg,q,now_ns=time.time_ns()+4_000_000_000)
        assert q.get('wuji-A','episode1')['state']=='converted'
        assert q.db.execute('select count(*) from uploads').fetchone()[0]==1


def test_cli_runs_processing_and_upload_child_to_completion(capture,tmp_path):
    import subprocess,sys
    cfg=capture;bag(cfg,3,1)
    path=tmp_path/'capture.yaml'
    local=Path(cfg['work_root'])
    raw={'schema_version':1,'state_root':str(local/'cli-state'),'work_root':str(local/'cli-work'),
         'remote_raw_root':cfg['remote_raw_root'],'remote_dataset_root':cfg['remote_dataset_root'],
         'poll_seconds':1,'stable_scans':2,'stable_seconds':1,'retry_seconds':1,
         'initial_history':{'mode':'all'},'routes':[{'robot':'wuji','group':'B','source':cfg['routes'][3]['source']}]}
    path.write_text(yaml.safe_dump(raw))
    result=subprocess.run([sys.executable,'-m','convest.capture.cli','run','--config',str(path),'--until-idle'],
                          capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'queued_for_upload' in result.stdout and '"upload": "completed"' in result.stdout
    checked=subprocess.run([sys.executable,'-m','convest.capture.cli','status','--config',str(path)],
                           capture_output=True,text=True,check=True)
    output=json.loads(checked.stdout)
    assert output['jobs'][0]['state']=='converted' and output['uploads'][0]['state']=='completed'


def test_published_target_collision_stops_only_that_upload(capture):
    cfg=capture;bag(cfg,2,1);bag(cfg,3,1)
    with context(cfg) as q:
        ready(cfg,q)
        target=Path(cfg['remote_raw_root'])/'wuji/A/episode1'
        target.mkdir(parents=True);(target/'foreign').write_bytes(b'preserve')
        transfer.upload_once(cfg,q)
        assert q.db.execute("select state from uploads where route='wuji-A'").fetchone()[0]=='conflict'
        assert (target/'foreign').read_bytes()==b'preserve'
        transfer.upload_once(cfg,q)
        assert q.db.execute("select state from uploads where route='wuji-B'").fetchone()[0]=='completed'
        assert not transfer.upload_once(cfg,q)


def test_history_none_skips_existing_but_accepts_new_recording(capture):
    cfg=capture
    for route in cfg['routes']:route['history']={'mode':'none','episodes':[]}
    bag(cfg,2,1)
    with context(cfg) as q:
        scan(cfg,q)
        assert not q.rows()
        bag(cfg,2,2)
        ready(cfg,q)
        assert q.get('wuji-A','episode1') is None
        assert q.get('wuji-A','episode2')['state']=='converted'


def test_duplicate_local_worker_cannot_enter_same_queue(capture):
    with context(capture):
        with pytest.raises(BlockingIOError):
            with context(capture):
                raise AssertionError('second worker entered')
