"""MCAP input compatibility for the existing, strict 30 Hz conversion pipeline.

The recording is read-only. A disposable SQLite processing view decodes H264/RVL
before idle cuts, preserving capture/receive times and the original archive.
This is deliberately not the native_indexed_v2 publication format.
"""
from collections import Counter, deque
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import zlib

import numpy as np
import yaml

VIDEO = 'foxglove_msgs/msg/CompressedVideo'
IMAGE = 'sensor_msgs/msg/Image'
COMPRESSED = 'sensor_msgs/msg/CompressedImage'


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(8*1024**2),b''):h.update(b)
    return h.hexdigest()


def is_mcap(path):
    p=Path(path)
    if p.is_file():return p.suffix=='.mcap'
    if any(p.glob('*.mcap')):return True
    meta=p/'metadata.yaml'
    if meta.is_file():
        try:doc=yaml.safe_load(meta.read_text())
        except yaml.YAMLError:return False
        return isinstance(doc,dict) and isinstance(doc.get('rosbag2_bagfile_information'),dict) and doc['rosbag2_bagfile_information'].get('storage_identifier')=='mcap'
    return any(p.glob('*.mcap'))


def require_format(path, expected):
    if ('mcap' if is_mcap(path) else 'rosbag')!=expected:
        raise ValueError('原包格式与任务选择不符；请分别建立 ROSbag 和 MCAP 任务')


def lifecycle(path):
    p=Path(path)
    if (p/'manifest.json').exists():
        if json.loads((p/'manifest.json').read_text()).get('status')!='complete':
            raise ValueError('MCAP manifest.status 尚非 complete，保留并等待收尾')
    if (p/'collection_state.json').exists():
        state=json.loads((p/'collection_state.json').read_text())
        if state.get('state')!='finalized' or state.get('finalized') is not True:
            raise ValueError('MCAP 录制尚未 finalized')
        return state
    if not (p/'manifest.json').is_file():raise ValueError('MCAP 缺少完成标记 manifest.json')
    episode=json.loads((p/'episode.json').read_text())
    start,end=episode.get('start_time_ns'),episode.get('end_time_ns')
    if type(start) is not int or type(end) is not int or not 0<start<end:
        raise ValueError('MCAP episode 起止采集时间无效')
    if not episode.get('episode_uid'):raise ValueError('MCAP 缺少全局 episode_uid')
    # Only lifecycle/window is adapted here. No engagement or validity is invented.
    return {'state':'finalized','finalized':True,'failures':[],
            'source_recording_id':episode['episode_uid'],
            'validation_report':{'source_effective_start_time_ns':start,'source_effective_end_time_ns':end}}


def assets(path):
    """Resolve only explicitly referenced, read-only shared resources; no symlinks."""
    p=Path(path).absolute();f=p/'episode.json'
    if not f.exists():return []
    result=[]
    for key,ref in json.loads(f.read_text()).get('assets',{}).items():
        if not isinstance(ref,dict) or not isinstance(ref.get('path'),str):raise ValueError('Invalid MCAP asset reference')
        rel=Path(ref['path'])
        if rel.is_absolute():raise ValueError('MCAP assets must use relative paths')
        target=(p/rel).resolve(strict=True)
        # Shared assets must be scoped to _shared or assets/models, not arbitrary host files.
        if not target.is_relative_to(p.resolve()) and '_shared' not in target.parts and not ('assets' in target.parts and 'models' in target.parts):
            raise ValueError('MCAP asset is outside shared/model roots')
        from convest.capture.transfer import ensure_real_path,manifest
        ensure_real_path(p/rel)
        rows=manifest(target) if target.is_dir() else [{'path':target.name,'size':target.stat().st_size,'sha256':sha(target)}]
        if target.is_file() and ref.get('sha256') and rows[0]['sha256']!=ref['sha256']:
            raise ValueError('MCAP shared asset SHA256 mismatch: '+key)
        result.append({'key':key,'relative':rel.as_posix(),'source':str(target),
                       'directory':target.is_dir(),'files':rows})
    return result


def check_assets(entries):
    from convest.capture.transfer import manifest,ensure_real_path
    for entry in entries:
        p=Path(entry['source']);ensure_real_path(p)
        actual=manifest(p) if entry['directory'] else [{'path':p.name,'size':p.stat().st_size,'sha256':sha(p)}]
        if actual!=entry['files']:raise ValueError('MCAP shared assets changed; original retained')


def metadata(path):
    p=Path(path).resolve()
    meta=yaml.safe_load((p/'metadata.yaml').read_text())['rosbag2_bagfile_information']
    if meta['storage_identifier']!='mcap' or meta.get('compression_format') or meta.get('compression_mode'):
        raise ValueError('Expected MCAP without outer rosbag compression')
    files=[]
    for rel in meta['relative_file_paths']:
        q=p/rel
        from convest.capture.transfer import ensure_real_path
        ensure_real_path(q)
        if not q.resolve().is_relative_to(p) or q.suffix!='.mcap' or not q.is_file():raise ValueError('Unsafe or missing MCAP volume')
        files.append(q)
    if not files or len(set(files))!=len(files):raise ValueError('Missing or duplicate MCAP volumes')
    if {x.resolve() for x in p.rglob('*.mcap')}!=set(files):raise ValueError('MCAP volumes differ from metadata file list')
    return meta,files


def summary_crc(path):
    with Path(path).open('rb') as f:
        f.seek(-37,2);footer=f.read()
        if footer[0]!=2 or footer[-8:]!=b'\x89MCAP0\r\n':raise ValueError('MCAP incomplete footer')
        length,start,offset,expected=struct.unpack('<QQQI',footer[1:29])
        if length!=20 or not start or not expected:raise ValueError('MCAP requires indexed summary and Summary CRC')
        end=f.tell()-37
        if not 8<=start<=end or (offset and not start<=offset<=end):raise ValueError('Invalid MCAP summary bounds')
        f.seek(start);left=end-start;crc=0
        while left:
            b=f.read(min(left,8*1024**2))
            if not b:raise ValueError('Truncated MCAP summary')
            crc=zlib.crc32(b,crc);left-=len(b)
        if zlib.crc32(footer[:25],crc)!=expected:raise ValueError('MCAP Summary CRC mismatch')


def integrity(path, progress=None):
    from mcap.reader import make_reader
    from mcap.stream_reader import StreamReader
    from mcap.records import Chunk
    if any(p.name.endswith(('.partial','.tmp','-wal','-shm')) for p in Path(path).rglob('*')):raise ValueError('MCAP has unfinished temporary files')
    state=lifecycle(path)
    if state.get('failures'):raise ValueError('MCAP capture has recorded failures')
    meta,files=metadata(path);counts=Counter();volumes=[];topic_types={}
    for p in files:
        if progress:progress('mcap_crc',file=p.name)
        summary_crc(p)
        with p.open('rb') as f:
            chunks=0
            for r in StreamReader(f,emit_chunks=True,validate_crcs=True).records:
                if isinstance(r,Chunk):
                    chunks+=1
                    if r.compression!='zstd' or not r.uncompressed_crc:raise ValueError('MCAP requires Zstd chunks with CRC')
            if not chunks:raise ValueError('MCAP has no chunks')
        with p.open('rb') as f:
            reader=make_reader(f,validate_crcs=True)
            if reader.get_header().profile!='ros2':raise ValueError('MCAP profile must be ros2')
            summary=reader.get_summary()
            if not summary or not summary.statistics or not summary.chunk_indexes or any(not x.message_index_offsets for x in summary.chunk_indexes):raise ValueError('MCAP indexes/statistics missing')
            for schema,ch,msg in reader.iter_messages(log_time_order=False):
                if schema is None or schema.encoding!='ros2msg' or ch.message_encoding!='cdr':raise ValueError('MCAP needs embedded ROS2 CDR schemas')
                if ch.topic in topic_types and topic_types[ch.topic]!=schema.name:raise ValueError('MCAP topic type changed between channels/volumes')
                topic_types[ch.topic]=schema.name
                if schema.name in (VIDEO,IMAGE,COMPRESSED,'sensor_msgs/msg/JointState','geometry_msgs/msg/WrenchStamped'):
                    if len(msg.data)<12 or msg.data[:2] not in (b'\x00\x01',b'\x00\x00'):raise ValueError('MCAP requires ROS2 CDR1 stamped messages')
                    sec,ns=struct.unpack_from('<iI' if msg.data[1] else '>iI',msg.data,4)
                    if ns>=10**9 or sec*10**9+ns!=msg.publish_time:raise ValueError('MCAP capture/publish time mismatch')
                counts[ch.topic]+=1
        volumes.append({'name':p.relative_to(Path(path)).as_posix(),'bytes':p.stat().st_size,'sha256':sha(p)})
    expected={x['topic_metadata']['name']:x['message_count'] for x in meta['topics_with_message_count']}
    if sum(counts.values())!=meta['message_count'] or any(counts.get(k,0)!=v for k,v in expected.items()) or set(counts)-set(expected):
        raise ValueError('MCAP message counts differ from metadata')
    external=assets(path)
    manifest_file=Path(path)/'manifest.json'
    if manifest_file.exists():
        entries=json.loads(manifest_file.read_text()).get('files')
        if not isinstance(entries,list):raise ValueError('MCAP manifest requires files list')
        actual={q.relative_to(path).as_posix() for q in Path(path).rglob('*') if q.is_file() and q.relative_to(path).as_posix() not in ('manifest.json','checksums.sha256')}
        listed=[row['path'] for row in entries]
        if len(set(listed))!=len(listed) or set(listed)!=actual:raise ValueError('MCAP manifest does not cover exact recording payload')
        known={row['name']:row['sha256'] for row in volumes}
        for row in entries:
            rel=Path(row['path']);q=Path(path)/rel
            if rel.is_absolute() or '..' in rel.parts or q.is_symlink():raise ValueError('Unsafe MCAP manifest path')
            if (known.get(row['path']) or sha(q))!=row['sha256'] or q.stat().st_size!=row.get('size',row.get('bytes')):raise ValueError('MCAP manifest file mismatch')
    from convest.sources.gello_rosbag2 import snapshot
    return {'source':str(Path(path).resolve()),'snapshot':snapshot(path),'volumes':volumes,'messages':sum(counts.values()),
            'metadata_sha256':sha(Path(path)/'metadata.yaml'),
            'collection_state_sha256':sha(Path(path)/'collection_state.json') if (Path(path)/'collection_state.json').exists() else None,
            'external_assets':external,'input_format':'mcap'}


def mapping(path,contract):
    p=Path(path);doc={}
    local=p/'processing.yaml'
    if local.is_file():doc=yaml.safe_load(local.read_text()) or {}
    else:
        for entry in assets(p):
            if entry['key']=='config_ref':
                f=Path(entry['source'])/'processing.yaml'
                if f.is_file():doc=yaml.safe_load(f.read_text()) or {}
    topic_map=doc.get('topic_map',{})
    if not isinstance(topic_map,dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in topic_map.items()):raise ValueError('Invalid MCAP topic_map')
    # Camera roles can be taken from explicit devices.yaml declarations.
    for entry in assets(p):
        if entry['key']!='devices_ref':continue
        devices=yaml.safe_load(Path(entry['source']).read_text()) or {}
        for dev in devices.get('devices',[]):
            for ch in dev.get('channels',[]):
                role=ch.get('role');topic=ch.get('topic','')
                camera={'head':'cam0','wrist_left':'cam2','wrist_right':'cam1'}.get(role)
                if camera and topic.endswith('/color/image_h264'):
                    topic_map.setdefault(topic,contract['topics'][camera]['topic'])
    return {**doc,'topic_map':topic_map}


def normalize(path, output, contract, schema_dir, progress=None):
    """Create a temporary, owned processing view. Never publish it as original."""
    from mcap.reader import make_reader
    from bag_worker.timestamps import make_typestore
    p=Path(path);out=Path(output);out.mkdir(parents=True,exist_ok=False)
    meta,files=metadata(p);definitions=[]
    for f in files:
        with f.open('rb') as stream:
            s=make_reader(stream).get_summary()
            definitions.extend((x.name,x.data.decode(),'ros2msg') for x in s.schemas.values())
    by_name={}
    for name,text,_ in definitions:
        if name in by_name and by_name[name]!=text:raise ValueError('MCAP schema definition changed between volumes')
        by_name[name]=text
    store=make_typestore(definitions)
    # Internal legacy status definitions, used only for recorded String JSON status.
    from rosbags.typesys import get_types_from_msg
    for kind in ('ArmCommandStatus','HandTelemetryStatus'):
        store.register(get_types_from_msg((Path(schema_dir)/(kind+'.msg')).read_text(),'teleop_interfaces/msg/'+kind))
    cfg=mapping(p,contract);aliases=cfg['topic_map'];renames=cfg.get('joint_name_map',{})
    topics={};counts=Counter();dbpath=out/'processing.db3'
    with sqlite3.connect(dbpath) as db:
        db.executescript('CREATE TABLE topics(id INTEGER PRIMARY KEY,name TEXT,type TEXT,serialization_format TEXT,offered_qos_profiles TEXT); CREATE TABLE messages(id INTEGER PRIMARY KEY,topic_id INTEGER,timestamp INTEGER,data BLOB); CREATE INDEX timestamp_idx ON messages(timestamp); CREATE TABLE message_definitions(id INTEGER PRIMARY KEY,topic_type TEXT,encoding TEXT,encoded_message_definition TEXT,type_description_hash TEXT);')
        for i,(name,text,encoding) in enumerate(definitions):db.execute('INSERT INTO message_definitions VALUES(?,?,?,?,?)',(i,name,encoding,text,''))
        def put(topic,kind,received,payload):
            if topic in topics and topics[topic][1]!=kind:raise ValueError('MCAP mapped topics have conflicting types')
            if topic not in topics:
                tid=len(topics)+1;topics[topic]=(tid,kind);db.execute('INSERT INTO topics VALUES(?,?,?,?,?)',(tid,topic,kind,'cdr',''))
            db.execute('INSERT INTO messages(topic_id,timestamp,data) VALUES(?,?,?)',(topics[topic][0],received,bytes(payload)));counts[topic]+=1
        decoders={};pending={}
        for f in files:
            with f.open('rb') as stream:
                for schema,ch,msg in make_reader(stream,validate_crcs=True).iter_messages(log_time_order=True):
                    kind=schema.name;topic=aliases.get(ch.topic,ch.topic);payload=msg.data
                    if kind==VIDEO:
                        import av
                        value=store.deserialize_cdr(payload,kind)
                        if value.format!='h264':raise ValueError('Only H264 CompressedVideo supported')
                        stamp=value.timestamp.sec*10**9+value.timestamp.nanosec
                        if stamp!=msg.publish_time:raise ValueError('MCAP video capture/publish time mismatch')
                        if ch.topic not in decoders:
                            decoders[ch.topic]=av.CodecContext.create('h264','r');pending[ch.topic]=deque()
                        pending[ch.topic].append((topic,msg.log_time,value.timestamp,value.frame_id))
                        frames=decoders[ch.topic].decode(av.Packet(bytes(value.data)))
                        for frame in frames:
                            target,received,timestamp,frame_id=pending[ch.topic].popleft()
                            rgb=frame.to_ndarray(format='rgb24');header=store.types['std_msgs/msg/Header'](timestamp,frame_id)
                            image=store.types[IMAGE](header,frame.height,frame.width,'rgb8',0,frame.width*3,np.frombuffer(rgb.tobytes(),dtype=np.uint8))
                            put(target,IMAGE,received,store.serialize_cdr(image,IMAGE))
                        continue
                    value=None
                    if kind==COMPRESSED and topic=='/cam0/depth/image_raw':
                        value=store.deserialize_cdr(payload,kind)
                        if value.format!='16UC1; compressedDepth rvl':raise ValueError('Depth needs 16UC1 compressedDepth RVL')
                        from .mcap_rvl import decode_payload
                        _,pixels=decode_payload(bytes(value.data))
                        image=store.types[IMAGE](value.header,*pixels.shape,'16UC1',0,pixels.shape[1]*2,np.frombuffer(pixels.astype('<u2').tobytes(),dtype=np.uint8))
                        kind=IMAGE;payload=store.serialize_cdr(image,kind)
                    elif kind=='sensor_msgs/msg/JointState' and renames:
                        value=store.deserialize_cdr(payload,kind);value.name=[renames.get(n,n) for n in value.name]
                        payload=store.serialize_cdr(value,kind)
                    elif kind=='std_msgs/msg/String' and topic in (contract['topics'][k]['topic'] for k in ('arm_command_status','hand_telemetry_status') if k in contract['topics']):
                        # Require all recorded status fields; never default validity/engagement.
                        data=json.loads(store.deserialize_cdr(payload,kind).data)
                        if topic==contract['topics']['arm_command_status']['topic']:
                            if (not isinstance(data.get('accepted_sides'),list) or any(x not in ('left','right') for x in data['accepted_sides']) or len(set(data['accepted_sides']))!=len(data['accepted_sides']) or not isinstance(data.get('faults'),list) or not all(isinstance(x,str) for x in data['faults'])):raise ValueError('Invalid recorded arm status JSON')
                        elif any(type(data.get(k)) is not bool for k in ('engaged','command_valid','state_valid')):raise ValueError('Invalid recorded hand status JSON')
                        key='arm_command_status' if topic==contract['topics']['arm_command_status']['topic'] else 'hand_telemetry_status'
                        kind=contract['topics'][key]['type']
                        def obj(name,values):
                            args={}
                            for field,desc in store.fielddefs[name][1]:
                                v=values[field]
                                if desc[0].name=='NAME':v=obj(desc[1],v)
                                args[field]=v
                            return store.types[name](**args)
                        payload=store.serialize_cdr(obj(kind,data),kind)
                    put(topic,kind,msg.log_time,payload)
        if any(pending.values()):raise ValueError('H264 decoder did not produce one frame per recorded access unit (B frames unsupported)')
        # Export the definitions actually used by the processing view.
        for _,kind in topics.values():
            text,_=store.generate_msgdef(kind,ros_version=2)
            db.execute('INSERT INTO message_definitions(topic_type,encoding,encoded_message_definition,type_description_hash) VALUES(?,?,?,?)',(kind,'ros2msg',text,''))
        bounds=db.execute('SELECT min(timestamp),max(timestamp) FROM messages').fetchone()
    info={'version':5,'storage_identifier':'sqlite3','relative_file_paths':['processing.db3'],
          'duration':{'nanoseconds':bounds[1]-bounds[0]},'starting_time':{'nanoseconds_since_epoch':bounds[0]},
          'message_count':sum(counts.values()),'topics_with_message_count':[{'topic_metadata':{'name':name,'type':kind,'serialization_format':'cdr','offered_qos_profiles':''},'message_count':counts[name]} for name,(_,kind) in topics.items()]}
    (out/'metadata.yaml').write_text(yaml.safe_dump({'rosbag2_bagfile_information':info}))
    (out/'collection_state.json').write_text(json.dumps(lifecycle(p)))
    if progress:progress('mcap_processing_view_ready',messages=sum(counts.values()))


@contextmanager
def processing_view(path,contract,schema_dir):
    with tempfile.TemporaryDirectory(prefix='mcap-processing-') as root:
        target=Path(root)/Path(path).name
        normalize(path,target,contract,schema_dir)
        yield target
