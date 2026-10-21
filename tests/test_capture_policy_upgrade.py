"""A cleaning-default upgrade preserves completed capture output and upload state."""
from pathlib import Path
import json
import time
import pytest
from convest.capture import worker
from convest.capture.worker import context,scan
from convest.ingest.batches import BatchPublisher
from test_capture import capture,ready
from test_ingest import service,bag


@pytest.mark.parametrize('mode',['single','daily'])
def test_completed_legacy_capture_and_upload_manifest_reused_after_policy_upgrade(capture,monkeypatch,mode):
    cfg=capture
    if mode=='daily':
        root=Path(cfg['work_root'])
        cfg.update(delivery_mode='daily',batch_hours=24,delete_raw_after_upload=True,cleanup_intermediates=True,
                   local_batch_root=str(root/'batches'),work_root=str(root/'work'))
        for r in cfg['routes']:r.update(output=str(root/'batches'/r['robot']/r['group']),batch_hours=24)
    bag(cfg,2,1);real=worker.run
    def legacy(config,*args,**kwargs):return real({**config,'image_time_basis':'receive'},*args,**kwargs)
    monkeypatch.setattr(worker,'run',legacy)
    with context(cfg) as q:
        ready(cfg,q);row=q.get('wuji-A','episode1');assert row['state']=='converted'
        owner=(Path(cfg['state_root'])/'owner.json').read_bytes()
        payloads=[tuple(r) for r in q.db.execute('SELECT id,payload FROM uploads ORDER BY id')]
        completes={p:p.read_bytes() for p in Path(cfg['work_root']).rglob('local_complete.json')}
        if mode=='daily':
            pub=BatchPublisher(cfg['routes'][2],cfg['fingerprint']);old=pub.current()
            clock=pub.status()[0]['deadline_ns']
        def forbidden(*args,**kwargs):raise AssertionError('Successful old recording must not rerun')
        monkeypatch.setattr(worker,'run',forbidden)
        scan(cfg,q,now_ns=time.time_ns()+4_000_000_000)
        assert q.get('wuji-A','episode1')['attempts']==1
        assert payloads==[tuple(r) for r in q.db.execute('SELECT id,payload FROM uploads ORDER BY id')]
        assert owner==(Path(cfg['state_root'])/'owner.json').read_bytes()
        assert all(p.read_bytes()==value for p,value in completes.items())
        if mode=='daily':assert pub.current()==old and pub.status()[0]['deadline_ns']==clock
        # Only a new recording uses the new default; retain the old completed payload.
        monkeypatch.setattr(worker,'run',real);bag(cfg,2,2);ready(cfg,q)
        assert q.get('wuji-A','episode2')['state']=='converted'
        if mode=='daily':
            assert pub.status()[0]['deadline_ns']==clock
            trim=json.loads((pub.current()/'ingest/sources/episode2/trim_report.json').read_text())
        else:
            trim=json.loads(next((Path(cfg['work_root'])/'wuji-A/episode2').glob('*/attempt-*/cleaned/episode2/trim_report.json')).read_text())
        assert trim['image_time_basis']=='capture'
