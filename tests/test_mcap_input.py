"""Real ROS2 MCAP/CDR, H264 and shared-asset compatibility regression fixtures."""
from collections import Counter
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import struct
import uuid

import av
import numpy as np
import pytest
import yaml
from mcap.writer import Writer,CompressionType
from rosbags.typesys import Stores,get_typestore,get_types_from_msg
from conversion_helpers import make_conversion_bag
from convest.config import WORKSPACE,load_config
from convest import mcap_input as m
from convest.jobs import check_ready

VIDEO_DEF='''builtin_interfaces/Time timestamp
string frame_id
uint8[] data
string format
================================================================================
MSG: builtin_interfaces/Time
int32 sec
uint32 nanosec
'''


def seal_manifest(raw):
    raw=Path(raw)
    (raw/'manifest.json').write_text(json.dumps({'status':'complete','files':[
        {'path':p.relative_to(raw).as_posix(),'size':p.stat().st_size,'sha256':m.sha(p)}
        for p in sorted(raw.rglob('*')) if p.is_file() and p.name not in ('manifest.json','checksums.sha256')]}))


def to_mcap(raw,robot='sharpa',standard=False,video=False,rvl=False):
    """Convert a tiny test bag into real MCAP, optionally all-standard channels."""
    store=get_typestore(Stores.ROS2_HUMBLE)
    for k in ('ArmCommandStatus','HandTelemetryStatus'):
        store.register(get_types_from_msg((WORKSPACE/'schemas'/(k+'.msg')).read_text(),'teleop_interfaces/msg/'+k))
    store.register(get_types_from_msg(VIDEO_DEF,m.VIDEO))
    meta=yaml.safe_load((raw/'metadata.yaml').read_text())['rosbag2_bagfile_information']
    with sqlite3.connect(raw/'data.db3') as db:
        topics=list(db.execute('SELECT id,name,type FROM topics'))
        rows=list(db.execute('SELECT topic_id,timestamp,data FROM messages ORDER BY timestamp,id'))
    enc={};topic_map={};counts=Counter();schemas={};channels={};channel_names={}
    with (raw/'data.mcap').open('wb') as f:
        w=Writer(f,compression=CompressionType.ZSTD,enable_crcs=True);w.start(profile='ros2')
        for tid,name,kind in topics:
            orig=name
            if video and name in ('/cam0/color/image_raw','/cam1/color/image_raw','/cam2/color/image_raw'):
                role={'/cam0/color/image_raw':'head','/cam1/color/image_raw':'wrist_right','/cam2/color/image_raw':'wrist_left'}[name]
                name=f'/devices/rig/cameras/{role}/color/image_h264';kind=m.VIDEO
                c=av.CodecContext.create('libx264','w');c.width=c.height=8;c.pix_fmt='yuv420p';c.time_base=__import__('fractions').Fraction(1,30)
                c.options={'crf':'0','bf':'0','preset':'ultrafast','tune':'zerolatency','x264-params':'repeat-headers=1:annexb=1'};c.open();enc[tid]=c
            elif rvl and name=='/cam0/depth/image_raw':
                name='/devices/rig/cameras/head/depth/image_raw/compressedDepth';kind=m.COMPRESSED
            elif standard and kind.startswith('teleop_interfaces/'):
                name='/recording'+name;kind='std_msgs/msg/String'
            elif standard:name='/devices/rig'+name
            if name!=orig:topic_map[name]=orig
            if kind not in schemas:
                text,_=store.generate_msgdef(kind,ros_version=2)
                schemas[kind]=w.register_schema(kind,'ros2msg',text.encode())
            channels[tid]=w.register_channel(name,'cdr',schemas[kind]);channel_names[tid]=(name,kind)
        original={tid:kind for tid,_,kind in topics}
        for tid,t,data in rows:
            name,kind=channel_names[tid]
            if tid in enc:
                image=store.deserialize_cdr(data,original[tid]);pixels=np.frombuffer(image.data,dtype=np.uint8).reshape(8,8,3)
                packets=enc[tid].encode(av.VideoFrame.from_ndarray(pixels,format='rgb24'))
                assert len(packets)==1
                msg=store.types[m.VIDEO](image.header.stamp,image.header.frame_id,np.frombuffer(bytes(packets[0]),dtype=np.uint8),'h264')
                data=store.serialize_cdr(msg,m.VIDEO)
            elif kind==m.COMPRESSED:
                image=store.deserialize_cdr(data,original[tid])
                msg=store.types[kind](image.header,'16UC1; compressedDepth rvl',np.frombuffer(struct.pack('<iffIII',0,0,0,8,8,0x88100000),dtype=np.uint8))
                data=store.serialize_cdr(msg,kind)
            elif kind=='std_msgs/msg/String':
                msg=store.deserialize_cdr(data,original[tid])
                data=store.serialize_cdr(store.types[kind](json.dumps(asdict(msg))),kind)
            w.add_message(channels[tid],log_time=t,publish_time=t-100,data=bytes(data));counts[name]+=1
        w.finish()
    meta.update(storage_identifier='mcap',relative_file_paths=['data.mcap'],topics_with_message_count=[
        {'topic_metadata':{'name':name,'type':kind,'serialization_format':'cdr'},'message_count':counts[name]} for name,kind in channel_names.values()])
    (raw/'metadata.yaml').write_text(yaml.safe_dump({'rosbag2_bagfile_information':meta}))
    (raw/'data.db3').unlink()
    if standard:
        state=json.loads((raw/'collection_state.json').read_text());window=state['validation_report']
        (raw/'collection_state.json').unlink()
        (raw/'episode.json').write_text(json.dumps({'episode_uid':'fixture-'+uuid.uuid4().hex,'start_time_ns':window['source_effective_start_time_ns'],'end_time_ns':window['source_effective_end_time_ns'],'assets':{}}))
        (raw/'manifest.json').write_text(json.dumps({'status':'complete','files':[]}))
        (raw/'processing.yaml').write_text(yaml.safe_dump({'topic_map':topic_map}))
        seal_manifest(raw)
    return raw


@pytest.mark.parametrize('robot',['sharpa','wuji'])
@pytest.mark.parametrize('standard,video',[(False,False),(True,False),(True,True)])
def test_real_mcap_to_strict_existing_lerobot(tmp_path,monkeypatch,standard,video,robot):
    from convest import jobs,pipeline
    cfg=load_config(WORKSPACE/f'configs/{robot}.yaml')
    name='ep_001' if video else 'episode1'
    raw=to_mcap(make_conversion_bag(tmp_path/'input'/name,robot),robot=robot,standard=standard,video=video,rvl=video)
    original={p.name:m.sha(p) for p in raw.iterdir()}
    ready=check_ready(raw,yaml.safe_load(Path(cfg['contract']).read_text()),cfg['schema_dir'])
    assert ready['input_format']=='mcap' and ready['messages']>0
    monkeypatch.setattr(jobs,'WORKSPACE',tmp_path);monkeypatch.setattr(pipeline,'WORKSPACE',tmp_path)
    cfg['source_root']=str(raw.parent)
    assert jobs.run(cfg,tmp_path/'outputs/job',episodes=[name])==0
    result=json.loads((tmp_path/'outputs/job/summary.json').read_text())['jobs'][0]
    assert result['state']=='completed',result.get('error')
    assert json.loads((tmp_path/'outputs/job/dataset/meta/info.json').read_text())['features']['observation.state']['shape']==([116] if robot=='sharpa' else [108])
    assert json.loads((tmp_path/'outputs/job/cleaned'/name/'trim_report.json').read_text())['source']==str(raw)
    assert {p.name:m.sha(p) for p in raw.iterdir()}==original


def test_mcap_crc_and_finalization_and_format_gates(tmp_path):
    raw=to_mcap(make_conversion_bag(tmp_path/'episode1','sharpa'),standard=True)
    assert m.integrity(raw)['messages']>0
    with pytest.raises(ValueError,match='格式'):m.require_format(raw,'rosbag')
    (raw/'manifest.json').write_text('{"status":"recording"}')
    with pytest.raises(ValueError,match='complete'):m.integrity(raw)
    (raw/'manifest.json').write_text('{"status":"complete"}')
    file=raw/'data.mcap';data=bytearray(file.read_bytes());data[-12]^=1;file.write_bytes(data)
    with pytest.raises(ValueError,match='CRC'):m.integrity(raw)


def test_missing_recorded_status_is_not_fabricated(tmp_path):
    raw=to_mcap(make_conversion_bag(tmp_path/'episode1','sharpa'),standard=True)
    cfg=load_config(WORKSPACE/'configs/sharpa.yaml')
    doc=yaml.safe_load((raw/'processing.yaml').read_text())
    doc['topic_map'].pop('/recording/teleop/arm_command_status')
    (raw/'processing.yaml').write_text(yaml.safe_dump(doc))
    seal_manifest(raw)
    with pytest.raises(ValueError,match='Missing required'):
        check_ready(raw,yaml.safe_load(Path(cfg['contract']).read_text()),cfg['schema_dir'])


def test_new_default_and_old_queue_migration(tmp_path,monkeypatch):
    from convest.gui import config
    from convest.config import digest
    monkeypatch.setattr(config,'ROOT',tmp_path/'ui')
    settings=config.defaults();t=settings['tasks'][0]
    assert t['input_format']=='mcap' and '/mcap/' in config.default_upload_paths(t)['raw_root']
    # Old settings without format must retain ROSbag paths and version identity.
    del t['input_format'];t.update(config.default_upload_paths(t))
    before={k:v for k,v in t.items() if k not in ('name','enabled','auto_upload_paths','delete_lerobot','batch_group_size')}
    loaded=config.validate(settings)['tasks'][0]
    assert loaded['input_format']=='rosbag' and '/rosbag/' in loaded['raw_root']
    after={k:v for k,v in loaded.items() if k not in ('name','enabled','auto_upload_paths','delete_lerobot','batch_group_size','input_format')}
    assert digest(before)==digest(after)

# Reuse the local-only transfer simulator; no live server/data is used below.
from test_gui import gui_spec,raw_archive_spec,local_transfer,local_verify,daily_cfg,capture,service
from test_ingest import bag
from test_capture import ready
from convest.capture.worker import context as capture_context
from convest.gui import worker
from convest.training import worker as training


def test_mcap_ab_runs_existing_full_normal_speedup_delivery(gui_spec,tmp_path,monkeypatch):
    s=raw_archive_spec(gui_spec,tmp_path);cfg=s['capture'];s['task']['input_format']='mcap'
    from convest.gui import mcap_assets
    monkeypatch.setattr(mcap_assets,'transfer',local_transfer);monkeypatch.setattr(mcap_assets,'verify_backup',local_verify)
    s['task']['raw_root']=str(tmp_path/'remote/mcap/sharpa/scene')
    cfg['batch_group_size']=2
    for r in cfg['routes']:r.update(input_format='mcap',batch_group_size=2,batch_name_format='seal-mmdd-v1')
    for i in range(2):to_mcap(bag(cfg,i,1),robot=cfg['routes'][i]['robot'],standard=True,video=True)
    raw=Path(cfg['routes'][0]['source'])/'episode1'
    shared=raw.parent/'_shared';shared.mkdir();(shared/'devices.yaml').write_text('devices: []\n')
    doc=json.loads((raw/'episode.json').read_text());doc['assets']={'devices_ref':{'path':'../_shared/devices.yaml','sha256':m.sha(shared/'devices.yaml')}}
    (raw/'episode.json').write_text(json.dumps(doc));seal_manifest(raw)
    with capture_context(cfg) as cap:
        ready(cfg,cap)
        assert all(r['state']=='converted' for r in cap.rows()),cap.rows()
        for _ in range(4):assert worker.capture_upload(s,cap)
        assert all(r['raw_done'] for r in cap.db.execute("SELECT * FROM uploads WHERE json_extract(payload,'$.kind')='raw'"))
        assert not any((Path(r['source'])/'episode1').exists() for r in cfg['routes'])
        assert (Path(s['task']['raw_root'])/'A/_shared/devices.yaml').exists()
    with training.context(s['training']) as db:
        training.discover(s['training'],db)
        for _ in range(2):assert training.process_once(s['training'],db)
        for _ in range(2):assert worker.training_upload(s,db)
        assert all(r['state']=='uploaded' for r in db.execute('SELECT * FROM tasks'))
        for r in db.execute('SELECT * FROM tasks'):
            payload=json.loads(r['payload']);assert all(any(f['path'].startswith(v+'/') for f in payload['files']) for v in ('normal','speedup'))


def test_shared_assets_are_archived_and_corruption_blocks_cd_deletion(gui_spec,tmp_path,monkeypatch):
    import time
    from convest.gui import raw_archive,mcap_assets
    s=raw_archive_spec(gui_spec,tmp_path);s['task'].update(input_format='mcap',delete_raw=False,raw_root=str(tmp_path/'remote/mcap/sharpa/scene'))
    source=Path(s['task']['source'])/'C/episode9'
    to_mcap(make_conversion_bag(source,'sharpa'),standard=True)
    shared=source.parent/'_shared';shared.mkdir();(shared/'devices.yaml').write_text('devices: []\n')
    (shared/'configs').mkdir();(shared/'configs/clock_mapping.yaml').write_text('method: ntp\n')
    doc=json.loads((source/'episode.json').read_text());doc['assets']={'devices_ref':{'path':'../_shared/devices.yaml','sha256':m.sha(shared/'devices.yaml')},'config_ref':{'path':'../_shared/configs'}}
    (source/'episode.json').write_text(json.dumps(doc));seal_manifest(source)
    monkeypatch.setattr(raw_archive,'transfer',local_transfer);monkeypatch.setattr(raw_archive,'verify_backup',local_verify)
    monkeypatch.setattr(mcap_assets,'transfer',local_transfer);monkeypatch.setattr(mcap_assets,'verify_backup',local_verify)
    def forbidden(*a,**kw):raise AssertionError('C/D must not decode MCAP or convert')
    monkeypatch.setattr(raw_archive,'check_raw_ready',forbidden)
    now=time.time_ns()
    with capture_context(s['capture']) as cap,raw_archive.context(s) as q:
        raw_archive.scan(s,q,cap,now);raw_archive.scan(s,q,cap,now+2*10**9)
        row=q.get('sharpa-C','episode9');assert row['state']=='uploaded',row
        target=Path(s['task']['raw_root'])/'C/_shared'
        assert (target/'devices.yaml').read_bytes()==(shared/'devices.yaml').read_bytes()
        assert (target/'configs/clock_mapping.yaml').exists()
        s['task']['delete_raw']=True
        original=(target/'devices.yaml').read_bytes();(target/'devices.yaml').write_text('corrupt')
        raw_archive.scan(s,q,cap,now+4*10**9)
        assert source.exists() and q.get('sharpa-C','episode9')['state']=='uploaded'
        (target/'devices.yaml').write_bytes(original)
        raw_archive.scan(s,q,cap,now+6*10**9)
        assert q.get('sharpa-C','episode9')['state']=='deleted' and not source.exists()
        assert shared.exists()  # shared local resources are never deleted with an episode


def test_shared_assets_change_invalidates_readiness_and_blocks_unsafe_paths(tmp_path):
    raw=to_mcap(make_conversion_bag(tmp_path/'episode1','sharpa'),standard=True)
    shared=raw.parent/'_shared';shared.mkdir();file=shared/'devices.yaml';file.write_text('devices: []\n')
    doc=json.loads((raw/'episode.json').read_text());doc['assets']={'devices_ref':{'path':'../_shared/devices.yaml','sha256':m.sha(file)}}
    (raw/'episode.json').write_text(json.dumps(doc));entries=m.assets(raw)
    file.write_text('changed')
    with pytest.raises(ValueError,match='changed'):m.check_assets(entries)
    with pytest.raises(ValueError,match='SHA256'):m.assets(raw)
    doc['assets']['devices_ref']={'path':'/etc/passwd'};(raw/'episode.json').write_text(json.dumps(doc))
    with pytest.raises(ValueError,match='relative'):m.assets(raw)


def test_manifest_cannot_mark_missing_payload_as_complete(tmp_path):
    raw=to_mcap(make_conversion_bag(tmp_path/'episode1','sharpa'),standard=True)
    doc=json.loads((raw/'manifest.json').read_text());doc['files'].pop()
    (raw/'manifest.json').write_text(json.dumps(doc))
    with pytest.raises(ValueError,match='cover exact'):m.integrity(raw)


def test_rvl_reference_decoder_rejects_truncated_payload():
    from convest.mcap_rvl import decode_payload,RvlError
    _,image=decode_payload(struct.pack('<iffIII',0,0,0,8,8,0x88100000))
    np.testing.assert_array_equal(image,np.zeros((8,8),dtype=np.uint16))
    with pytest.raises(RvlError):decode_payload(b'truncated')
