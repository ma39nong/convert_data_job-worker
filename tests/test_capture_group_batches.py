"""A+B count boundaries, joint sealing/recovery and prospective training names."""
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest
from convest.capture import daily, group_batches
from convest.capture.worker import context,scan
from convest.ingest.batches import BatchPublisher
from convest.gui import worker as gui_worker
from convest.training import worker as training
from test_capture_daily import daily_cfg
from test_capture import capture,ready
from test_ingest import service,bag


def counted(cfg,size=100):
    cfg['routes']=cfg['routes'][:2]
    cfg['batch_group_size']=size
    for r in cfg['routes']:r.update(batch_group_size=size,batch_name_format='seal-mmdd-v1')
    return cfg


def drain(cfg,q):
    for _ in range(20):
        ready(cfg,q)
        if all(r['state']=='converted' for r in q.rows()):break
    assert all(r['state']=='converted' for r in q.rows())


def active_count(cfg):
    return sum(b['source_count'] for r in cfg['routes'] for b in daily.publisher(cfg,r).status() if b['state']=='active')


def test_actual_100_source_boundary_60a_40b_then_next_batch(daily_cfg):
    cfg=counted(daily_cfg)
    # All 100 recordings go through the real readiness/quality/conversion pipeline.
    for n in range(1,61):bag(cfg,0,n)
    for n in range(1,40):bag(cfg,1,n)
    with context(cfg) as q:
        drain(cfg,q)
        assert active_count(cfg)==99
        assert not any(b['state']=='sealed' for r in cfg['routes'] for b in daily.publisher(cfg,r).status())
        daily.maintain(cfg,q,now_ns=10**20)  # elapsed time never seals a partial count batch
        assert active_count(cfg)==99
        assert q.db.execute("select count(*) from uploads where episode like 'batch-%'").fetchone()[0]==0
    # Restart retains the 99 sources; only the next valid B recording closes the round.
    with context(cfg) as q:
        bag(cfg,1,40);drain(cfg,q)
        batches=[daily.publisher(cfg,r).status()[0] for r in cfg['routes']]
        assert [b['source_count'] for b in batches]==[60,40]
        assert all(b['state']=='sealed' and b['group_ready'] for b in batches)
        assert len({b['group_id'] for b in batches})==1
        assert len({b['name_date_ns'] for b in batches})==1
        assert all(b['group_total']==100 and b['deadline_ns'] is None for b in batches)
        assert q.db.execute("select count(*) from uploads where episode like 'batch-%'").fetchone()[0]==2
        bag(cfg,0,61);drain(cfg,q)
        assert active_count(cfg)==1
        assert len(daily.publisher(cfg,cfg['routes'][0]).status())==2
        for b in batches:assert b['verification']['status']=='passed'


def test_failed_member_seal_blocks_both_then_retries_without_reconversion(daily_cfg,monkeypatch):
    cfg=counted(daily_cfg,2)
    bag(cfg,0,1);bag(cfg,1,1)
    real=BatchPublisher._verify_seal
    def fail_b(self,batch):
        if self.route['group']=='B':raise OSError('B seal interrupted')
        return real(self,batch)
    monkeypatch.setattr(BatchPublisher,'_verify_seal',fail_b)
    with context(cfg) as q:
        ready(cfg,q)
        assert 'batch_group' in daily.maintain(cfg,q)
        a=daily.publisher(cfg,cfg['routes'][0]).status()[0]
        assert a['state']=='sealed' and not a['group_ready']
        with pytest.raises(ValueError,match='waiting_seal'):
            training.sealed({'capture':cfg},{'route':cfg['routes'][0]['id'],'batch_id':a['id']})
        assert q.db.execute("select count(*) from uploads where episode like 'batch-%'").fetchone()[0]==0
        journal=json.loads((Path(cfg['state_root'])/'group_batches.json').read_text())
        stamp=journal['seals'][0]['prepared_ns']
    monkeypatch.setattr(BatchPublisher,'_verify_seal',real)
    import convest.capture.worker as processing
    monkeypatch.setattr(processing,'run',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('must reuse committed generation')))
    with context(cfg) as q:
        assert not daily.maintain(cfg,q)
        ready(cfg,q)
        for r in cfg['routes']:
            b=daily.publisher(cfg,r).status()[0]
            assert b['group_ready'] and b['name_date_ns']==stamp
            assert q.get(r['id'],'episode1')['state']=='converted'
        assert q.db.execute("select count(*) from uploads where episode like 'batch-%'").fetchone()[0]==2
        assert len(json.loads((Path(cfg['state_root'])/'group_batches.json').read_text())['seals'])==1


def test_upgrade_open_24h_batch_and_preserve_old_sealed_batches(daily_cfg):
    cfg=daily_cfg;cfg['routes']=cfg['routes'][:2]
    bag(cfg,0,1)
    with context(cfg) as q:
        ready(cfg,q)
        pub=daily.publisher(cfg,cfg['routes'][0]);old=pub.status()[0]
        daily.maintain(cfg,q,old['deadline_ns']+1)
        sealed=pub.status()[0].copy()
        bag(cfg,0,2);bag(cfg,1,2);ready(cfg,q)
    counted(cfg,3)
    with context(cfg) as q:
        assert not daily.maintain(cfg,q,now_ns=10**20)
        assert pub.status()[0]==sealed
        assert active_count(cfg)==2
        bag(cfg,1,3);drain(cfg,q)
        new=[b for r in cfg['routes'] for b in daily.publisher(cfg,r).status() if b.get('group_id')]
        assert sum(b['source_count'] for b in new)==3 and all(b['group_ready'] for b in new)
        assert pub.status()[0]==sealed


def test_counted_seal_date_is_beijing_preparation_date_and_fixed_on_retry(daily_cfg,monkeypatch):
    from types import SimpleNamespace
    cfg=counted(daily_cfg,2)
    stamp=int(datetime(2026,11,4,0,0,1,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()*1e9)
    monkeypatch.setattr(group_batches,'time',SimpleNamespace(time_ns=lambda:stamp))
    bag(cfg,0,7);bag(cfg,1,9)
    with context(cfg) as q:
        drain(cfg,q)
        assert daily.publisher(cfg,cfg['routes'][0]).status()[0]['name']=='1104_7_7_data_lerobot'
        assert daily.publisher(cfg,cfg['routes'][1]).status()[0]['name']=='1104_9_9_data_lerobot'
        group_batches.maintain(cfg)
        assert daily.publisher(cfg,cfg['routes'][0]).status()[0]['name_date_ns']==stamp


def test_short_variant_names_are_explicit_and_old_payloads_keep_their_names():
    payload={'batch_name':'1104_100_200_data_lerobot','name':'internal-pair-name'}
    assert gui_worker.training_destination_name(payload,'normal')=='1104_100_200_normal_lerobot'
    assert gui_worker.training_destination_name({**payload,'destination_name_format':'variant-short-v1'},'normal')=='1104_100_200_normal'
    assert gui_worker.training_destination_name({**payload,'destination_name_format':'variant-short-v1'},'speedup')=='1104_100_200_speedup'


def test_excluded_recording_does_not_consume_combined_quota(daily_cfg,monkeypatch):
    import convest.capture.worker as processing
    cfg=counted(daily_cfg,2)
    real=processing.run
    def exclude(config,job,episodes,**kw):
        if episodes==['episode99']:
            job.mkdir(parents=True,exist_ok=True)
            (job/'summary.json').write_text(json.dumps({'jobs':[{'state':'excluded','quality_rejection':{'reason':'test quality exclusion'}}]}))
            return
        return real(config,job,episodes=episodes,**kw)
    monkeypatch.setattr(processing,'run',exclude)
    bag(cfg,0,1);bag(cfg,1,99)
    with context(cfg) as q:
        ready(cfg,q)
        assert q.get(cfg['routes'][1]['id'],'episode99')['state']=='excluded'
        assert active_count(cfg)==1
        assert not json.loads((Path(cfg['state_root'])/'group_batches.json').read_text())['seals']
        bag(cfg,1,2);ready(cfg,q)
        assert all(b['state']=='sealed' for r in cfg['routes'] for b in daily.publisher(cfg,r).status())


def test_single_nonempty_group_can_close_100_round_without_empty_peer(daily_cfg):
    cfg=counted(daily_cfg,2)
    bag(cfg,0,1);bag(cfg,0,3)
    with context(cfg) as q:
        drain(cfg,q)
        assert daily.publisher(cfg,cfg['routes'][1]).status()==[]
        b=daily.publisher(cfg,cfg['routes'][0]).status()[0]
        assert b['group_ready'] and b['sources']==['episode1','episode3']


def test_manual_partial_batch_then_resume_automatic_count(daily_cfg):
    cfg=counted(daily_cfg,3)
    with context(cfg):
        assert group_batches.flush(cfg) is False  # no empty packages
    bag(cfg,0,1);bag(cfg,1,1)
    with context(cfg) as q:
        drain(cfg,q)
        assert active_count(cfg)==2
        assert group_batches.flush(cfg) is True
        assert group_batches.flush(cfg) is False  # repeated click is idempotent
        assert not daily.maintain(cfg,q)
        sealed=[daily.publisher(cfg,r).status()[0] for r in cfg['routes']]
        assert all(b['group_ready'] and b['group_total']==2 for b in sealed)
        assert q.db.execute("select count(*) from uploads where episode like 'batch-%'").fetchone()[0]==2
        bag(cfg,0,2);drain(cfg,q)
        assert active_count(cfg)==1
        bag(cfg,0,3);bag(cfg,1,2);drain(cfg,q)
        new=[b for r in cfg['routes'] for b in daily.publisher(cfg,r).status() if b.get('group_total')==3]
        assert len(new)==2 and all(b['group_ready'] for b in new)
        assert active_count(cfg)==0


def test_manual_seal_recovers_after_interruption_and_limit_change(daily_cfg,monkeypatch):
    cfg=counted(daily_cfg,5)
    bag(cfg,0,1);bag(cfg,1,1)
    real=BatchPublisher._verify_seal
    with context(cfg) as q:
        drain(cfg,q)
        def fail_b(self,batch):
            if self.route['group']=='B':raise OSError('manual seal interrupted')
            return real(self,batch)
        monkeypatch.setattr(BatchPublisher,'_verify_seal',fail_b)
        with pytest.raises(OSError):group_batches.flush(cfg)
    counted(cfg,3)
    monkeypatch.setattr(BatchPublisher,'_verify_seal',real)
    with context(cfg) as q:
        assert not daily.maintain(cfg,q)
        assert all(daily.publisher(cfg,r).status()[0]['group_ready'] for r in cfg['routes'])
        journal=json.loads((Path(cfg['state_root'])/'group_batches.json').read_text())
        assert journal['limit']==3 and journal['seals'][0]['limit']==5
        bag(cfg,0,2);drain(cfg,q)
        assert active_count(cfg)==1


@pytest.mark.parametrize('legacy_without_journal',[False,True])
def test_lowering_limit_seals_existing_recordings_without_splitting(daily_cfg,legacy_without_journal):
    cfg=counted(daily_cfg,5)
    bag(cfg,0,1);bag(cfg,1,1);bag(cfg,0,2)
    with context(cfg) as q:
        drain(cfg,q)
        # Ensure old policy exists, as it does during regular maintenance.
        group_batches.maintain(cfg)
    if legacy_without_journal:(Path(cfg['state_root'])/'group_batches.json').unlink()
    counted(cfg,2)
    with context(cfg) as q:
        assert not daily.maintain(cfg,q)
        assert active_count(cfg)==0
        assert all(daily.publisher(cfg,r).status()[0]['group_total']==3 for r in cfg['routes'])
        bag(cfg,0,3);drain(cfg,q)
        assert active_count(cfg)==1
