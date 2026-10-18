import json
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import pytest
pa=pytest.importorskip("pyarrow")
pq=pytest.importorskip("pyarrow.parquet")
pytest.importorskip("av")
import yaml
from convest.align import align,GROUPS
from convest.config import WORKSPACE,load_config,atomic_json
from convest.sources.gello_rosbag2 import Series,CompactTimeMap
from convest.sources import robot_rosbag2 as source
from convest.recipes.multimodal import prepare,output_segments
from convest.quality import RecordingRejected
from convest.formats.multimodal import original_times,nested_array,write_segment,reindex_segment,write_metadata
from convest.verify import verify,values
from convest.jobs import check_ready
from conversion_helpers import make_conversion_bag


def raw_streams(hand=20):
    ts=1_000_000_000+np.arange(31)*1_000_000_000//30
    raw={}
    for group,n in zip(GROUPS,(7,7,hand,hand)):
        raw[f'state.{group}']=Series(ts,[np.full(2*n,i,np.float32) for i in range(31)],ts)
        raw[f'action.{group}']=Series(ts,[np.full(n,100+i,np.float32) for i in range(31)],ts)
        raw[f'status.{group}']=Series(ts,[(True,True)]*31,ts)
    for c in ('cam0','cam1','cam2'):raw[c]=Series(ts,list(range(31)),ts)
    return raw


@pytest.mark.parametrize('robot,dim', [('wuji',54),('sharpa',58)])
def test_actual_cdr_multimodal_write_reindex_and_full_source_verification(tmp_path,robot,dim):
    raw=make_conversion_bag(tmp_path/'source',robot)
    cfg=load_config(WORKSPACE/f'configs/{robot}.yaml');cfg['source_root']=str(raw)
    contract=yaml.safe_load(Path(cfg['contract']).read_text())
    readiness=check_ready(raw,contract,cfg['schema_dir']);assert readiness['messages']>0
    item=source.discover(raw)[0];out=tmp_path/'dataset';out.mkdir()
    with source.Bag(raw,cfg['schema_dir']) as bag:
        streams,aligned=prepare(source,bag,item,cfg,contract)
        assert aligned.action.shape[1]==dim
        spec=output_segments(item,aligned,cfg)[0]
        ep=write_segment(out,bag,streams,aligned,spec['bounds'],0,0,cfg,contract,spec)
        reindex_segment(out,ep,0,0)
        for relative in ep['paths']:
            (out/'final'/relative).replace(out/relative)
    record={'source':str(raw),'source_snapshot':source.snapshot(raw),'alignment':aligned.report,'episodes':[ep]}
    write_metadata(out,[record],cfg)
    atomic_json(out/'conversion/manifest.json',{'config':cfg})
    atomic_json(out/'conversion/records/000000.json',record)
    checked=verify(out,compare_source=True)
    assert checked['episodes']==1 and checked['lossless_sensor_rows_compared']==12*(1 if robot=='wuji' else 21)
    info=json.loads((out/'meta/info.json').read_text())
    assert info['robot_type']==f'dual_fr3_{robot}'
    assert ('observation.engaged' in info['features'])==(robot=='wuji')
    # Streaming reindex must retain every sensor column while changing only global indices.
    table=pq.read_table(out/ep['paths'][0]);reindex_segment(out,ep,1001,400)
    new=pq.read_table(out/'final/data/chunk-001/episode_001001.parquet')
    assert new['index'].to_pylist()==list(range(400,412))
    for name in table.column_names:
        if name not in ('episode_index','index','task_index'):assert new[name].equals(table[name])


def test_sharpa_no_fabricated_hand_status_and_fresh_commands_required():
    raw=raw_streams(22)
    for side in ('left','right'):del raw[f'status.{side}_hand']
    aligned=align(raw,1_000_000_000,2_000_000_000,30,150_000_000)
    assert aligned.state.shape==(31,116) and np.isnan(aligned.engaged[:,2:]).all()
    raw['action.left_hand']=Series(raw['cam0'].times[:1],[np.zeros(22)])
    aligned=align(raw,1_000_000_000,2_000_000_000,30,150_000_000)
    assert not aligned.valid_mask[5:].any()


def test_inactive_arm_holds_real_transition_command_and_audit_index():
    raw=raw_streams();t=raw['cam0'].times
    raw['status.left_arm']=Series(t,[(True,True)]*4+[(False,True)]*18+[(True,True)]*9)
    raw['action.left_arm']=Series(t[:4],[np.full(7,i,np.float32) for i in range(4)])
    aligned=align(raw,int(t[0]),int(t[-1]),30,150_000_000)
    assert np.all(aligned.action[4:22,:7]==3)
    assert np.all(aligned.source_indices['action.left_arm'][4:22]==3)
    assert not aligned.valid_mask[22:].any()


@pytest.mark.parametrize('frames', [1,2,3])
def test_rgb_short_gap_tolerance_does_not_change_sensor_timestamps(frames):
    raw=raw_streams();s=raw['cam0'];keep=[i for i in range(31) if i<=6 or i>=11+frames]
    raw['cam0']=Series(s.times[keep],[s.values[i] for i in keep]);before=raw['cam0'].times.copy()
    aligned=align(raw,1_000_000_000,2_000_000_000,30,150_000_000,camera_short_gap_max_frames=2)
    np.testing.assert_array_equal(raw['cam0'].times,before)
    assert aligned.report['camera_short_gap_tolerance']['accepted_frames']==(frames if frames<=2 else 0)
    cfg={'min_segment_frames':2,'task':'task'}
    if frames==3:
        with pytest.raises(RecordingRejected):output_segments({'path':'source'},aligned,cfg)
    else:assert len(output_segments({'path':'source'},aligned,cfg))==1


def test_dense_sensor_gaps_are_not_rgb_tolerated_and_reject_internal_range():
    raw=raw_streams();cfg=load_config(WORKSPACE/'configs/wuji.yaml')
    s=raw['cam0'];keep=[i for i in range(31) if i<=6 or i>=13]
    key='observation.depths.cam0';raw[key]=Series(s.times[keep],[0]*len(keep))
    fake=SimpleNamespace(read_streams=lambda bag,contract:raw,STATUS_MESSAGE_TYPES=[],timestamp_policy=None)
    _,aligned=prepare(fake,SimpleNamespace(compact_time_map=None),{'source_start_ns':int(s.times[0]),'source_end_ns':int(s.times[-1])},cfg,{})
    with pytest.raises(RecordingRejected):output_segments({'path':'source'},aligned,cfg)


def test_native_sensor_nested_roundtrip_and_inverse_mapping():
    a=np.arange(48,dtype=np.uint16).reshape(3,4,4)
    np.testing.assert_array_equal(values(nested_array(a),[4,4]),a)
    mapping=CompactTimeMap(((100,200,100),(400,450,50)),frozenset(),'report')
    np.testing.assert_array_equal(original_times(np.array([99,100,299,300,301]),mapping),[99,200,399,450,451])


def test_readiness_rejects_incomplete_transfer_and_mismatched_robot(tmp_path):
    cfg=load_config(WORKSPACE/'configs/wuji.yaml');contract=yaml.safe_load(Path(cfg['contract']).read_text())
    raw=make_conversion_bag(tmp_path/'source')
    (raw/'transfer.partial').write_text('pending')
    with pytest.raises(ValueError,match='temporary'):check_ready(raw,contract,cfg['schema_dir'])
    (raw/'transfer.partial').unlink()
    meta=yaml.safe_load((raw/'metadata.yaml').read_text());meta['rosbag2_bagfile_information']['message_count']+=1
    (raw/'metadata.yaml').write_text(yaml.safe_dump(meta))
    with pytest.raises(ValueError,match='count'):check_ready(raw,contract,cfg['schema_dir'])
    raw=make_conversion_bag(tmp_path/'sharpa','sharpa')
    with source.Bag(raw,cfg['schema_dir']) as bag:
        with pytest.raises(ValueError,match='Missing'):source.read_streams(bag,contract)


def test_job_resume_recovery_hashes_and_owned_artifacts(tmp_path,monkeypatch):
    import convest.jobs as jobs
    import convest.pipeline as pipeline
    source_root=tmp_path/'input';raw=make_conversion_bag(source_root/'episode1')
    cfg=load_config(WORKSPACE/'configs/wuji.yaml');cfg['source_root']=str(source_root)
    monkeypatch.setattr(jobs,'WORKSPACE',tmp_path)
    monkeypatch.setattr(pipeline,'WORKSPACE',tmp_path)
    def serial(items,config,contract,staging,workers):
        for item in items:
            stage=staging/'single'
            try:yield item,stage,pipeline.prepare_bag(item,config,contract,stage),None
            except Exception as exc:yield item,stage,None,exc
    monkeypatch.setattr(pipeline,'prepared_in_order',serial)
    out=tmp_path/'outputs/job'
    assert jobs.run(cfg,out)==0
    source_before=jobs.snapshot(raw)
    files=list((out/'dataset').rglob('*.parquet'))+list((out/'dataset').rglob('*.mp4'))
    before={str(p.relative_to(out)):(jobs.checksum(p),p.stat().st_mtime_ns) for p in files}
    orphan=out/'dataset/data/chunk-000/episode_999999.parquet';orphan.write_text('uncommitted')
    stage=out/'dataset/conversion/staging';stage.mkdir();(stage/'partial').write_text('uncommitted')
    unrelated=out/'dataset/notes.txt';unrelated.write_text('keep')
    (out/'dataset/meta/info.json').unlink()  # Metadata must rebuild from committed journal.
    assert jobs.run(cfg,out,resume=True)==0
    assert not orphan.exists() and not stage.exists() and unrelated.read_text()=='keep'
    assert jobs.snapshot(raw)==source_before
    assert before=={str(p.relative_to(out)):(jobs.checksum(p),p.stat().st_mtime_ns) for p in files}
    # Source mutation must fail rather than silently reusing the previous output.
    state=json.loads((raw/'collection_state.json').read_text());state['note']='changed'
    (raw/'collection_state.json').write_text(json.dumps(state))
    assert jobs.run(cfg,out,resume=True)==2
    assert 'differs' in json.loads((out/'jobs/episode1.json').read_text())['error']


def test_job_recovers_kill_between_cleaned_export_and_snapshot(tmp_path,monkeypatch):
    import convest.jobs as jobs
    import convest.pipeline as pipeline
    from dataclasses import asdict
    from bag_worker.idle import default_config,trim_bag
    from convest.config import digest
    raw=make_conversion_bag(tmp_path/'input/episode1')
    cfg=load_config(WORKSPACE/'configs/wuji.yaml');cfg['source_root']=str(raw.parent)
    monkeypatch.setattr(jobs,'WORKSPACE',tmp_path);monkeypatch.setattr(pipeline,'WORKSPACE',tmp_path)
    def serial(items,config,contract,staging,workers):
        for item in items:
            stage=staging/'single'
            yield item,stage,pipeline.prepare_bag(item,config,contract,stage),None
    monkeypatch.setattr(pipeline,'prepared_in_order',serial)
    root=tmp_path/'outputs/job';clean=root/'cleaned/episode1'
    trim_bag(raw,clean)
    contract=yaml.safe_load(Path(cfg['contract']).read_text())
    ready=jobs.check_ready(raw,contract,cfg['schema_dir'])
    atomic_json(root/'manifest.json',{'owner':'convert-data-job-worker','fingerprint':digest({'config':cfg,'idle':asdict(default_config()),'version':'job-2-idle-v3'}),'config':cfg})
    atomic_json(root/'jobs/episode1.json',{'state':'trimming','readiness':ready})
    assert jobs.run(cfg,root,resume=True)==0
    assert json.loads((root/'jobs/episode1.json').read_text())['state']=='completed'


def test_wrench_statistics_keep_force_torque_axes_separate():
    from convest.formats.multimodal import VectorStats
    x=np.arange(66,dtype=np.float64).reshape(11,6);x[:,3:]*=1000
    stats=VectorStats();stats.add(x[:3]);stats.add(x[3:])
    r=stats.result()
    np.testing.assert_allclose(r['mean'],x.mean(axis=0))
    np.testing.assert_allclose(r['std'],x.std(axis=0))
    assert r['count']==[11]


@pytest.mark.parametrize('seam,frames,allowed',[(True,1,True),(True,2,True),(True,3,False),(False,2,False)])
def test_depth_tolerance_requires_cleaner_seam_rgb_acceptance_and_two_frame_bound(seam,frames,allowed):
    raw=raw_streams();cfg=load_config(WORKSPACE/'configs/wuji.yaml');original=raw['cam0']
    keep=[i for i in range(31) if i<=6 or i>=11+frames]
    for key in ('cam0','observation.depths.cam0'):
        raw[key]=Series(original.times[keep],[0]*len(keep),original.times[keep])
    before=raw['observation.depths.cam0'].times.copy()
    mapping=CompactTimeMap(((1_400_000_000,1_900_000_000,500_000_000),),frozenset(),'report') if seam else None
    fake=SimpleNamespace(read_streams=lambda bag,contract:raw,STATUS_MESSAGE_TYPES=[],timestamp_policy=None)
    _,aligned=prepare(fake,SimpleNamespace(compact_time_map=mapping),
                      {'source_start_ns':1_000_000_000,'source_end_ns':2_000_000_000},cfg,{})
    np.testing.assert_array_equal(raw['observation.depths.cam0'].times,before)
    assert aligned.report['depth_splice_tolerance']['accepted_frames']==(frames if allowed else 0)
    if allowed:
        assert aligned.valid_mask.all()
        assert max(aligned.report['depth_splice_tolerance']['gaps'][0]['actual_depth_age_ms'])<=250
    else:
        with pytest.raises(RecordingRejected):output_segments({'path':'source'},aligned,cfg)


def test_original_recording_identity_survives_cleaning(tmp_path):
    from bag_worker.idle import trim_bag
    raw=make_conversion_bag(tmp_path/'raw/episode1')
    cleaned=tmp_path/'cleaned/episode1';trim_bag(raw,cleaned)
    assert source.discover(cleaned)[0]['source_recording_id']==str(raw)


def test_two_workers_commit_in_source_order_and_dataset_lock(tmp_path,monkeypatch):
    import fcntl
    import convest.pipeline as pipeline
    source_root=tmp_path/'input'
    make_conversion_bag(source_root/'episode2',frames=16)
    make_conversion_bag(source_root/'episode1',frames=12)
    cfg=load_config(WORKSPACE/'configs/wuji.yaml')
    cfg.update(source_root=str(source_root),output_root=str(tmp_path/'dataset'))
    monkeypatch.setattr(pipeline,'WORKSPACE',tmp_path)
    assert pipeline.convert(cfg,workers=2)==0
    records=pipeline.load_records(tmp_path/'dataset')
    assert [Path(r['source']).name for r in records]==['episode1','episode2']
    assert [r['episodes'][0]['episode_index'] for r in records]==[0,1]
    assert verify(tmp_path/'dataset',compare_source=True)['frames']==28
    with (tmp_path/'dataset/conversion/lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):pipeline.convert(cfg,resume=True)


def test_named_joint_mapping_and_status_timestamp_domain():
    from convest.sources.gello_rosbag2 import joint_values,sample_timestamp
    msg=SimpleNamespace(name=['b','a'],position=[2.,1.],velocity=[4.,3.])
    np.testing.assert_array_equal(joint_values(msg,['a','b'],['position','velocity']),[1,2,3,4])
    msg.name=['a','a']
    with pytest.raises(ValueError,match='Duplicate'):joint_values(msg,['a','b'],['position'])
    mapping=CompactTimeMap(((200,400,200),),frozenset({'teleop_interfaces/msg/ArmCommandStatus','example/msg/Unmodified'}),'trim')
    # An unchanged custom status header is in the old domain; compact record time wins.
    assert sample_timestamp(1000,800,'teleop_interfaces/msg/ArmCommandStatus',mapping)==800
    # An already-rewritten standard image must never be mapped twice.
    assert sample_timestamp(600,800,'sensor_msgs/msg/Image',mapping)==600
    assert sample_timestamp(600,800,'example/msg/Unmodified',mapping)==400
