from decimal import Decimal
import hashlib
import json
import sqlite3
import numpy as np
import pytest
from conversion_helpers import make_conversion_bag
from bag_worker.bag import Bag,IMAGE_TYPES,image_capture_ns
from bag_worker.timestamps import TimeMap


def delayed_bag(root):
    path=make_conversion_bag(root,frames=18)
    with sqlite3.connect(path/'data.db3') as db:
        db.execute('UPDATE messages SET timestamp=timestamp+100000000 WHERE topic_id IN (SELECT id FROM topics WHERE type="sensor_msgs/msg/Image")')
    return Bag(path)


@pytest.mark.parametrize('compact',[True,False])
def test_capture_selection_half_open_boundaries_payload_and_counts(tmp_path,compact):
    bag=delayed_bag(tmp_path/'source')
    a,b=1_200_000_000,1_300_000_000
    ranges=[[str(Decimal(a-bag.start)/10**9),str(Decimal(b-bag.start)/10**9)]]
    before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in bag.root.iterdir()}
    output=tmp_path/'result'
    report=bag.export(ranges,output,compact=compact,image_time_basis='capture')
    mapper=TimeMap(bag.start,[(a-bag.start,b-bag.start)])
    with sqlite3.connect(bag.files[0]) as src,sqlite3.connect(next(output.glob('*.db3'))) as dst:
        types=dict(src.execute('SELECT id,type FROM topics'));expected=[]
        for ident,topic,received,data in src.execute('SELECT * FROM messages ORDER BY id'):
            selected=image_capture_ns(data[:12]) if types[topic] in IMAGE_TYPES else received
            if a<=selected<b:continue
            expected.append(ident)
            new=dst.execute('SELECT timestamp,data FROM messages WHERE id=?',(ident,)).fetchone()
            assert new is not None
            assert new[0]==(mapper(received) if compact else received)
            if types[topic] in IMAGE_TYPES:
                assert data[:4]+data[12:]==new[1][:4]+new[1][12:]
                assert image_capture_ns(new[1][:12])==(mapper(selected) if compact else selected)
        assert expected==[r[0] for r in dst.execute('SELECT id FROM messages ORDER BY id')]
        assert len(expected)==report['kept']==sum(t['kept'] for t in report['topics'])
        # Exposure before cut, delivery inside cut: preserve it.
        preserved=src.execute('SELECT id FROM messages WHERE topic_id IN (SELECT id FROM topics WHERE type="sensor_msgs/msg/Image") AND timestamp>=? AND timestamp<?',(a,b)).fetchall()
        assert preserved and all(dst.execute('SELECT 1 FROM messages WHERE id=?',row).fetchone() for row in preserved)
    assert before=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in bag.root.iterdir()}
    assert report['message_selection']['sensor_msgs/msg/Image']=='header_timestamp'


def test_explicit_receive_compatible_and_capture_invalid_header_fails_closed(tmp_path):
    bag=delayed_bag(tmp_path/'source');out=tmp_path/'legacy'
    report=bag.export([[.12,.3]],out,image_time_basis='receive')
    assert 'image_time_basis' not in report
    with sqlite3.connect(bag.files[0]) as db:
        ident,data=db.execute('SELECT id,data FROM messages WHERE topic_id IN (SELECT id FROM topics WHERE type="sensor_msgs/msg/Image") LIMIT 1').fetchone()
        db.execute('UPDATE messages SET data=? WHERE id=?',(data[:4]+bytes(8)+data[12:],ident))
    bag=Bag(bag.root)
    with pytest.raises(ValueError,match='timestamp is invalid'):
        bag.export([[.12,.3]],tmp_path/'bad',image_time_basis='capture')
    assert not (tmp_path/'bad').exists() and not (tmp_path/'bad.partial').exists()


def test_invalid_time_basis_rejected(tmp_path):
    bag=delayed_bag(tmp_path/'source')
    with pytest.raises(ValueError,match='image_time_basis'):
        bag.export([],tmp_path/'bad',image_time_basis='nearest')


def test_actual_capture_clean_convert_and_independent_source_verification(tmp_path,monkeypatch):
    import convest.jobs as jobs
    import convest.pipeline as pipeline
    from convest.config import WORKSPACE,load_config
    from convest.sources.robot_rosbag2 import Bag as SourceBag
    from convest.pipeline import load_records
    source=make_conversion_bag(tmp_path/'input/episode1',frames=60)
    with SourceBag(source,WORKSPACE/'schemas') as reader:
        store=reader.store
        with sqlite3.connect(source/'data.db3') as db:
            for ident,payload in db.execute('SELECT id,data FROM messages WHERE topic_id IN (SELECT id FROM topics WHERE name IN ("/left/franka/joint_states","/right/franka/joint_states"))').fetchall():
                msg=store.deserialize_cdr(payload,'sensor_msgs/msg/JointState')
                i=round((msg.header.stamp.sec*10**9+msg.header.stamp.nanosec-10**9)*30/10**9)
                step=i if i<12 else 12 if i<42 else i-30
                msg.position=np.arange(len(msg.name),dtype=np.float64)/100+step/50
                db.execute('UPDATE messages SET data=? WHERE id=?',(bytes(store.serialize_cdr(msg,'sensor_msgs/msg/JointState')),ident))
            db.execute('UPDATE messages SET timestamp=timestamp+100000000 WHERE topic_id IN (SELECT id FROM topics WHERE type="sensor_msgs/msg/Image")')
    # Header delays alter delivery end time but not message counts/finalized capture window.
    cfg=load_config(WORKSPACE/'configs/wuji.yaml');cfg.update(source_root=str(source.parent),image_time_basis='capture')
    monkeypatch.setattr(jobs,'WORKSPACE',tmp_path)
    monkeypatch.setattr(pipeline,'WORKSPACE',tmp_path)
    def serial(items,config,contract,staging,workers):
        for i,item in enumerate(items):
            stage=staging/f'serial-{i}'
            try:yield item,stage,pipeline.prepare_bag(item,config,contract,stage),None
            except Exception as exc:yield item,stage,None,exc
    monkeypatch.setattr(pipeline,'prepared_in_order',serial)
    root=tmp_path/'outputs/trial';jobs.run(cfg,root,episodes=['episode1'])
    summary=json.loads((root/'summary.json').read_text())
    assert summary['completed']==1 and summary['failed']==0 and summary['excluded']==0
    assert summary['dataset_verification_passed']
    trim=json.loads((root/'cleaned/episode1/trim_report.json').read_text())
    assert trim['image_time_basis']=='capture' and trim['mapping']
    record=load_records(root/'dataset')[0]
    # Fixture telemetry starts 100 ns after the validated first grid point;
    # the existing policy trims that edge, but never an internal hole.
    alignment=record['alignment']
    assert sum(e['length'] for e in record['episodes'])==alignment['grid_frames']-alignment['invalid_frames']


def test_capture_selection_does_not_hide_a_real_original_depth_gap(tmp_path):
    import yaml
    from convest.config import WORKSPACE,load_config
    from convest.sources import robot_rosbag2 as source_module
    from convest.recipes.multimodal import prepare,output_segments
    from convest.quality import RecordingRejected
    raw=make_conversion_bag(tmp_path/'raw/episode1',frames=45)
    with sqlite3.connect(raw/'data.db3') as db:
        db.execute('DELETE FROM messages WHERE topic_id IN (SELECT id FROM topics WHERE name="/cam0/depth/image_raw") AND timestamp>=1400000000 AND timestamp<1900000000')
    output=tmp_path/'clean/episode1'
    Bag(raw).export([],output,image_time_basis='capture')
    cfg=load_config(WORKSPACE/'configs/wuji.yaml')
    contract=yaml.safe_load((WORKSPACE/'schemas/gello_contract.yaml').read_text())
    item=source_module.discover(output)[0]
    with source_module.Bag(output,WORKSPACE/'schemas') as reader:
        _,aligned=prepare(source_module,reader,item,cfg,contract)
        with pytest.raises(RecordingRejected,match='internal invalid interval'):
            output_segments(item,aligned,cfg)
