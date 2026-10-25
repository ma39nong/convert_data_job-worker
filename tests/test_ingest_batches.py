"""Timed batch boundaries, independent datasets and crash recovery, locally."""
import json
import os
import time
from pathlib import Path
import pytest
import yaml
from convest.config import atomic_json
from convest.ingest.config import load_service_config
from convest.ingest.batches import BatchPublisher
from convest.ingest.worker import worker_context,scan
from convest.jobs import checksum
from convest.verify import verify
from test_ingest import service,bag,ready_scan


def publisher(cfg,route=2):return BatchPublisher(cfg['routes'][route],cfg['fingerprint'])


def test_batch_ranges_gaps_rollover_local_indices_restart_and_global_dedup(service):
    cfg=service;bag(cfg,2,36);bag(cfg,2,41)
    with worker_context(cfg) as q:
        assert ready_scan(cfg,q)['counts']=={'published':2}
        pub=publisher(cfg);first=pub.status()[0];root=pub.current()
        assert first['name']=='36_41_data_lerobot' and first['sources']==['episode36','episode41']
        assert first['deadline_ns']-first['opened_ns']==72*3600*10**9
        assert first['source_count']==2 and first['episodes']==2
        original={str(p.relative_to(root)):checksum(p) for p in root.rglob('*') if p.is_file()}
        # Last batch closes on background scans even with no new episode.
        report=scan(cfg,q,now_ns=first['deadline_ns'])
        assert not report['route_errors'] and pub.status()[0]['state']=='sealed'
        assert pub.status()[0]['verification']['artifact_sha256']=='passed'
        assert original=={str(p.relative_to(root)):checksum(p) for p in root.rglob('*') if p.is_file()}
    bag(cfg,2,55)
    with worker_context(cfg) as q:
        assert ready_scan(cfg,q)['counts']=={'published':3}
        batches=pub.status();assert len(batches)==2
        assert batches[0]['state']=='sealed' and batches[1]['name']=='55_55_data_lerobot'
        newer=pub.current();assert newer!=root and verify(newer)['episodes']==1
        rows=[json.loads(line) for line in (newer/'meta/episodes.jsonl').read_text().splitlines()]
        assert [r['episode_index'] for r in rows]==[0]
        opened=batches[1]['opened_ns']
    with worker_context(cfg) as q:
        assert scan(cfg,q)['counts']=={'published':3}
        assert pub.status()[1]['opened_ns']==opened and len(pub.published())==3
        scan(cfg,q,now_ns=opened+100*3600*10**9)
        scan(cfg,q,now_ns=opened+200*3600*10**9)
        assert len(pub.status())==2 and all(b['state']=='sealed' for b in pub.status())
    assert sorted(p.name for p in Path(cfg['routes'][2]['output']).iterdir() if not p.name.startswith('.'))==[
        '36_41_data_lerobot','55_55_data_lerobot']


def test_four_routes_have_independent_batch_clocks_and_cross_batch_source_conflicts(service):
    cfg=service;b=bag(cfg,2,36);bag(cfg,0,8)
    with worker_context(cfg) as q:
        ready_scan(cfg,q);wuji=publisher(cfg);sharpa=publisher(cfg,0)
        # Exercise one route's clock without changing the other routes' clocks.
        wuji.published(now_ns=wuji.status()[0]['deadline_ns'])
        assert wuji.status()[0]['state']=='sealed' and sharpa.status()[0]['state']=='active'
        bag(cfg,2,55);ready_scan(cfg,q);assert len(wuji.status())==2
        root=Path(wuji.status()[0]['generation'])
        p=b/'metadata.yaml';p.write_text(p.read_text()+'\n# late source change\n')
        report=scan(cfg,q);assert report['counts']=={'conflict':1,'published':2}
        assert Path(wuji.status()[0]['generation'])==root and len(wuji.published())==2


def test_attempt_crossing_deadline_finishes_old_batch_then_seals(service,monkeypatch):
    import convest.ingest.worker as worker
    cfg=service;bag(cfg,2,1)
    with worker_context(cfg) as q:
        ready_scan(cfg,q);pub=publisher(cfg);bag(cfg,2,2);real=worker.run
        def crosses(*args,**kw):
            result=real(*args,**kw)
            # Wall-clock boundary occurs after this attempt started but before publication.
            path=pub.store/'batches.json';catalog=json.loads(path.read_text());current=catalog['batches'][0]
            current['deadline_ns']=time.time_ns()-1
            current['opened_ns']=current['deadline_ns']-pub.duration_ns
            atomic_json(path,catalog);return result
        monkeypatch.setattr(worker,'run',crosses)
        assert ready_scan(cfg,q)['counts']=={'published':2}
        batches=pub.status();assert len(batches)==1 and batches[0]['state']=='sealed'
        assert batches[0]['name']=='1_2_data_lerobot' and batches[0]['source_count']==2
        monkeypatch.setattr(worker,'run',real)
        bag(cfg,2,3);assert ready_scan(cfg,q)['counts']=={'published':3}
        assert [b['name'] for b in pub.status()]==['1_2_data_lerobot','3_3_data_lerobot']


def test_failed_attempt_does_not_start_batch_clock(service,monkeypatch):
    import convest.ingest.worker as worker
    cfg=service;bag(cfg,2,1);real=worker.run
    def fails(*args,**kw):raise OSError('conversion unavailable')
    with worker_context(cfg) as q:
        monkeypatch.setattr(worker,'run',fails)
        assert ready_scan(cfg,q)['counts']=={'retry':1}
        assert publisher(cfg).status()==[]
        monkeypatch.setattr(worker,'run',real)
        q.put('wuji-A','episode1',q.get('wuji-A','episode1')['identity'],'retry',next_ns=0)
        before=time.time_ns();assert scan(cfg,q,now_ns=before+4_000_000_000)['counts']=={'published':1}
        assert publisher(cfg).status()[0]['opened_ns']>=before


@pytest.mark.parametrize('failure',['alias','catalog'])
def test_generation_commit_then_range_update_failure_recovers_without_reconversion(service,monkeypatch,failure):
    import convest.ingest.batches as module
    import convest.ingest.worker as worker
    cfg=service;bag(cfg,2,1)
    with worker_context(cfg) as q:
        ready_scan(cfg,q);pub=publisher(cfg);bag(cfg,2,2)
        if failure=='alias':
            real=module.os.rename
            def fails(src,dst,*args,**kw):
                if Path(src).name=='1_1_data_lerobot':raise OSError('range rename interrupted')
                return real(src,dst,*args,**kw)
            monkeypatch.setattr(module.os,'rename',fails)
        else:
            real=module.BatchPublisher._save
            def fails(self,catalog):
                if any(b.get('source_count')==2 for b in catalog['batches']):raise OSError('catalog write interrupted')
                return real(self,catalog)
            monkeypatch.setattr(module.BatchPublisher,'_save',fails)
        assert ready_scan(cfg,q)['counts']=={'published':1,'retry':1}
    monkeypatch.undo()
    def no_reconversion(*args,**kw):raise AssertionError('Already committed source reconverted')
    monkeypatch.setattr(worker,'run',no_reconversion)
    with worker_context(cfg) as q:
        assert scan(cfg,q)['counts']=={'published':2}
        assert pub.status()[0]['name']=='1_2_data_lerobot' and verify(pub.current())['episodes']==2
        assert not (pub.output/'1_1_data_lerobot').exists()


def test_seal_failure_blocks_route_until_successful_recheck_and_does_not_block_other_routes(service,monkeypatch):
    cfg=service;bag(cfg,2,1)
    with worker_context(cfg) as q:
        ready_scan(cfg,q);pub=publisher(cfg);deadline=pub.status()[0]['deadline_ns']
        real=BatchPublisher._verify_seal
        def fails(self,batch):
            if self.route['id']=='wuji-A':raise ValueError('full batch audit failed')
            return real(self,batch)
        monkeypatch.setattr(BatchPublisher,'_verify_seal',fails)
        bag(cfg,2,2);bag(cfg,0,1)
        scan(cfg,q,now_ns=deadline)
        report=scan(cfg,q,now_ns=deadline+2_000_000_000)
        assert 'wuji-A' in report['route_errors'] and q.get('sharpa-A','episode1')['state']=='published'
        assert len(pub.status())==1 and pub.status()[0]['state']=='active' and 'seal_error' in pub.status()[0]
        monkeypatch.setattr(BatchPublisher,'_verify_seal',real)
        pub.published(now_ns=deadline)
        assert pub.status()[0]['state']=='sealed' and 'seal_error' not in pub.status()[0]


def test_last_batch_seals_even_when_input_root_is_temporarily_missing(service):
    cfg=service;bag(cfg,2,1)
    with worker_context(cfg) as q:
        ready_scan(cfg,q);pub=publisher(cfg);deadline=pub.status()[0]['deadline_ns']
        source=Path(cfg['routes'][2]['source']);source.rename(source.with_name('offline-A'))
        report=scan(cfg,q,now_ns=deadline)
        assert 'wuji-A' in report['route_errors'] and pub.status()[0]['state']=='sealed'


def test_batch_config_output_paths_duration_and_server_existing_data_policy(service):
    cfg=service
    for r in cfg['routes']:
        assert Path(r['source'])==Path(cfg['data_root'])/'origin-rosbag'/r['robot']/r['group']
        assert Path(r['output'])==Path(cfg['data_root'])/'data-lerobot'/r['robot']/r['group']
        assert r['batch_hours']==72
    from convest.config import WORKSPACE
    template=yaml.safe_load((WORKSPACE/'configs/ingest.server.example.yaml').read_text())
    assert template['initial_history']=={'mode':'all'} and template['batch_hours']==72
    assert template['data_root']=='/data/data-fruits/real/harvest-tomato/data'
    path=Path(cfg['state_root']).parent/'config.yaml';raw=yaml.safe_load(path.read_text());raw['batch_hours']=0
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError,match='batch_hours'):load_service_config(path)


def test_occupied_public_range_is_preserved_instead_of_overwritten(service):
    cfg=service;bag(cfg,2,1);pub=publisher(cfg);pub._init_store()
    occupied=pub.output/'1_1_data_lerobot';occupied.mkdir();(occupied/'keep.txt').write_text('unrelated')
    with worker_context(cfg) as q:
        assert ready_scan(cfg,q)['counts']=={'retry':1}
        assert 'already occupied' in q.get('wuji-A','episode1')['error']
        assert (occupied/'keep.txt').read_text()=='unrelated' and pub.current() is None


def test_seal_detects_corrupt_artifact_even_when_size_and_mtime_are_unchanged(service):
    cfg=service;bag(cfg,2,1)
    with worker_context(cfg) as q:
        ready_scan(cfg,q);pub=publisher(cfg);batch=pub.status()[0];root=pub.current()
        artifact=next(root.glob('videos/**/*.mp4'));original=artifact.read_bytes();st=artifact.stat()
        def replace(data):
            artifact.chmod(0o644);artifact.write_bytes(data)
            os.utime(artifact,ns=(st.st_atime_ns,st.st_mtime_ns));artifact.chmod(0o444)
        replace(bytes([original[0]^1])+original[1:])
        with pytest.raises(ValueError,match='hash mismatch'):pub.published(now_ns=batch['deadline_ns'])
        assert pub.status()[0]['state']=='active' and 'seal_error' in pub.status()[0]
        replace(original);pub.published(now_ns=batch['deadline_ns'])
        assert pub.status()[0]['state']=='sealed' and 'seal_error' not in pub.status()[0]


def test_interrupted_first_publication_recovers_original_batch_start_timestamp(service,monkeypatch):
    cfg=service;bag(cfg,2,1);pub=publisher(cfg);real=BatchPublisher._save
    def fails(self,catalog):
        if any(b.get('opened_ns') is not None for b in catalog['batches']):raise OSError('first catalog update interrupted')
        return real(self,catalog)
    with worker_context(cfg) as q:
        monkeypatch.setattr(BatchPublisher,'_save',fails)
        assert ready_scan(cfg,q)['counts']=={'retry':1}
        pending=pub._catalog()['batches'][0];assert pending['opened_ns'] is None
        root=pub._publisher(pending).current()
        committed=json.loads((root/'ingest/generation.json').read_text())['created_ns']
    monkeypatch.setattr(BatchPublisher,'_save',real)
    with worker_context(cfg) as q:
        assert scan(cfg,q)['counts']=={'published':1}
        assert pub.current()==root and pub.status()[0]['opened_ns']==committed


def test_dated_batch_uses_last_completed_generation_beijing_date_and_keeps_legacy(service, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import convest.ingest.publish as generation
    cfg=service
    route=cfg['routes'][2]
    route['batch_name_format']='completion-mmdd-v1'
    stamps=iter(int(datetime.fromisoformat(d).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()*1e9)
                for d in ('2026-10-26T23:59:59','2026-10-27T00:00:01'))
    original=generation.atomic_json
    def dated_marker(path, value):
        if path.name=='generation.json':value={**value,'created_ns':next(stamps)}
        return original(path,value)
    monkeypatch.setattr(generation,'atomic_json',dated_marker)
    bag(cfg,2,100);bag(cfg,2,200)
    with worker_context(cfg) as q:
        assert ready_scan(cfg,q)['counts']=={'published':2}
        pub=publisher(cfg);batch=pub.status()[0]
        assert batch['name']=='1027_100_200_data_lerobot'
        assert batch['last_completed_ns']==int(datetime(2026,10,27,0,0,1,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()*1e9)
        # Restart/seal on a later day must retain the final append date.
        pub.published(now_ns=batch['deadline_ns']+10**15)
        assert pub.status()[0]['name']==batch['name']
        assert pub.status()[0]['state']=='sealed'
    # Existing catalogue entries without a naming policy stay unchanged on upgrade.
    catalogue=json.loads((pub.store/'batches.json').read_text())
    catalogue['batches'][0].pop('name_format')
    old_alias=pub.output/'100_200_data_lerobot'
    (pub.output/batch['name']).rename(old_alias)
    catalogue['batches'][0]['name']=old_alias.name
    atomic_json(pub.store/'batches.json',catalogue)
    pub.published(now_ns=batch['deadline_ns']+10**15)
    assert pub.status()[0]['name']=='100_200_data_lerobot'
