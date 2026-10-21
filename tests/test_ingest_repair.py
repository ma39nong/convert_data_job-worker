import json
from pathlib import Path
import sqlite3
import numpy as np
import pytest
from convest.config import WORKSPACE,digest,atomic_json,load_config
from convest.ingest import worker
from convest.ingest.worker import worker_context
from convest.ingest.control import request_maintenance,maintenance_requested
from convest.ingest.batches import BatchPublisher
from convest.ingest.repair import backfill,eligible,legacy_snapshot
from convest.sources.robot_rosbag2 import Bag as SourceBag
from convest.jobs import checksum,run
from bag_worker.idle import default_config
from dataclasses import asdict
from conversion_helpers import make_conversion_bag
from test_ingest import service,bag,ready_scan


def delayed_hold(cfg,number=2):
    root=make_conversion_bag(Path(cfg['routes'][2]['source'])/f'episode{number}',frames=60)
    with SourceBag(root,WORKSPACE/'schemas') as reader:
        store=reader.store
        with sqlite3.connect(root/'data.db3') as db:
            for ident,payload in db.execute('SELECT id,data FROM messages WHERE topic_id IN (SELECT id FROM topics WHERE name IN ("/left/franka/joint_states","/right/franka/joint_states"))').fetchall():
                msg=store.deserialize_cdr(payload,'sensor_msgs/msg/JointState')
                i=round((msg.header.stamp.sec*10**9+msg.header.stamp.nanosec-10**9)*30/10**9)
                step=i if i<12 else 12 if i<42 else i-30
                msg.position=np.arange(len(msg.name),dtype=np.float64)/100+step/50
                db.execute('UPDATE messages SET data=? WHERE id=?',(bytes(store.serialize_cdr(msg,'sensor_msgs/msg/JointState')),ident))
            db.execute('UPDATE messages SET timestamp=timestamp+300000000 WHERE topic_id IN (SELECT id FROM topics WHERE type="sensor_msgs/msg/Image")')
    return root


def legacy_run(monkeypatch):
    real=worker.run
    def receive(config,*args,**kwargs):return real({**config,'image_time_basis':'receive'},*args,**kwargs)
    monkeypatch.setattr(worker,'run',receive)
    return real


def test_real_legacy_gap_backfilled_only_successful_old_files_unchanged_and_idempotent(service,tmp_path,monkeypatch):
    cfg=service;bag(cfg,2,1);source=delayed_hold(cfg)
    original={p.name:checksum(p) for p in source.iterdir()}
    real=legacy_run(monkeypatch)
    with worker_context(cfg) as q:
        ready_scan(cfg,q)
        assert q.get('wuji-A','episode1')['state']=='published'
        assert eligible(q.get('wuji-A','episode2'))
        before=legacy_snapshot(cfg);owner=(Path(cfg['state_root'])/'owner.json').read_bytes()
        monkeypatch.setattr(worker,'run',real)
        bag(cfg,2,3)  # Must not discover/process unrelated queued input during repair.
        request_maintenance(cfg,True)
        result=backfill(cfg,q,tmp_path/'repair')
        assert result['published']==1 and result['old_sources_preserved']==1
        assert q.get('wuji-A','episode2')['state']=='published' and q.get('wuji-A','episode3') is None
        after=legacy_snapshot(cfg);assert after[('wuji-A','episode1')]==before[('wuji-A','episode1')]
        assert owner==(Path(cfg['state_root'])/'owner.json').read_bytes() and maintenance_requested(cfg)
        assert original=={p.name:checksum(p) for p in source.iterdir()}
        assert backfill(cfg,q,tmp_path/'again')['selected']==0


def test_repair_requires_pause_and_honors_cancellation(service,tmp_path,monkeypatch):
    cfg=service;delayed_hold(cfg);real=legacy_run(monkeypatch)
    with worker_context(cfg) as q:
        ready_scan(cfg,q);assert eligible(q.get('wuji-A','episode2'))
        with pytest.raises(ValueError,match='Pause receiver'):
            backfill(cfg,q,tmp_path/'not-paused')
        request_maintenance(cfg,True);monkeypatch.setattr(worker,'run',real)
        result=backfill(cfg,q,tmp_path/'cancelled',lambda:True)
        assert result['status']=='cancelled' and not result['results']
        assert q.get('wuji-A','episode2')['state']=='excluded'


def test_changed_original_kept_excluded_not_force_published(service,tmp_path,monkeypatch):
    cfg=service;source=delayed_hold(cfg);real=legacy_run(monkeypatch)
    with worker_context(cfg) as q:
        ready_scan(cfg,q);assert eligible(q.get('wuji-A','episode2'))
        with (source/'collection_state.json').open('a') as stream:stream.write('\n')
        request_maintenance(cfg,True);monkeypatch.setattr(worker,'run',real)
        result=backfill(cfg,q,tmp_path/'changed')
        assert result['results'][0]['state']=='input_not_repaired'
        assert q.get('wuji-A','episode2')['state']=='excluded'
        assert not BatchPublisher(cfg['routes'][2],cfg['fingerprint']).published()


def test_default_capture_job_rejects_old_implicit_receive_resume_before_modifying(service,tmp_path,monkeypatch):
    source=bag(service,2,1);cfg=load_config(WORKSPACE/'configs/wuji.yaml');cfg['source_root']=str(source.parent)
    root=tmp_path/'outputs/job';root.mkdir(parents=True)
    atomic_json(root/'manifest.json',{'owner':'convert-data-job-worker',
        'fingerprint':digest({'config':cfg,'idle':asdict(default_config()),'version':'job-2-idle-v3'}),'config':cfg})
    import convest.jobs as jobs
    monkeypatch.setattr(jobs,'WORKSPACE',tmp_path)
    with pytest.raises(ValueError,match='Job config changed'):run(cfg,root,resume=True)


def test_selection_ignores_success_sharpa_and_other_rejections():
    error=json.dumps({'reason':'Whole recording excluded: full-all contains an internal invalid interval',
        'details':{'policy':'continuous_export_range','export_range':'full-all','gaps':[{'frames':1}]}})
    row={'route':'wuji-A','state':'excluded','error':error}
    assert eligible(row)
    assert not eligible({**row,'route':'sharpa-A'})
    assert not eligible({**row,'state':'published'})
    assert not eligible({**row,'error':'not json'})
    assert not eligible({**row,'error':'[]'})
    assert not eligible({**row,'error':json.dumps({'reason':'no valid frames','details':{'gaps':[]}})})


def test_true_original_depth_gap_remains_excluded(service,tmp_path,monkeypatch):
    import yaml
    cfg=service;source=make_conversion_bag(Path(cfg['routes'][2]['source'])/'episode2',frames=45)
    with sqlite3.connect(source/'data.db3') as db:
        db.execute('DELETE FROM messages WHERE topic_id IN (SELECT id FROM topics WHERE name="/cam0/depth/image_raw") AND timestamp>=1400000000 AND timestamp<1900000000')
        counts=dict(db.execute('SELECT topic_id,count(*) FROM messages GROUP BY topic_id'))
        ids=dict(db.execute('SELECT name,id FROM topics'))
    p=source/'metadata.yaml';meta=yaml.safe_load(p.read_text());info=meta['rosbag2_bagfile_information']
    info['message_count']=sum(counts.values())
    for entry in info['topics_with_message_count']:entry['message_count']=counts[ids[entry['topic_metadata']['name']]]
    p.write_text(yaml.safe_dump(meta))
    real=legacy_run(monkeypatch)
    with worker_context(cfg) as q:
        ready_scan(cfg,q);assert eligible(q.get('wuji-A','episode2'))
        request_maintenance(cfg,True);monkeypatch.setattr(worker,'run',real)
        result=backfill(cfg,q,tmp_path/'real-gap')
        assert result['still_excluded']==1 and not result['all_repaired']
        assert q.get('wuji-A','episode2')['state']=='excluded'
        assert not BatchPublisher(cfg['routes'][2],cfg['fingerprint']).published()


def test_committed_generation_reconciles_without_reconversion(service,tmp_path,monkeypatch):
    cfg=service;bag(cfg,2,1)
    with worker_context(cfg) as q:
        ready_scan(cfg,q);old=q.get('wuji-A','episode1')
        error=json.dumps({'reason':'Whole recording excluded: full-all contains an internal invalid interval',
            'details':{'policy':'continuous_export_range','export_range':'full-all','gaps':[{'frames':1}]}})
        q.put('wuji-A','episode1',old['identity'],'excluded',proof=old['proof'],error=error)
        request_maintenance(cfg,True)
        def forbidden(*args,**kwargs):raise AssertionError('Committed source must not reconvert')
        monkeypatch.setattr(worker,'run',forbidden)
        result=backfill(cfg,q,tmp_path/'reconcile')
        assert result['published']==1 and result['results'][0]['reconciled']
        assert q.get('wuji-A','episode1')['state']=='published'
