"""Small real CDR/SQLite bags with both robot contracts and every sensor type."""
from pathlib import Path
import json
import sqlite3
import numpy as np
import yaml
from rosbags.typesys import Stores,get_typestore,get_types_from_msg
from convest.config import WORKSPACE
from convest.sources.robot_rosbag2 import modalities


def make_conversion_bag(root,robot='wuji',frames=12):
    root=Path(root);root.mkdir(parents=True)
    contract=yaml.safe_load((WORKSPACE/'schemas'/('gello_contract.yaml' if robot=='wuji' else 'sharpa_contract.yaml')).read_text())
    store=get_typestore(Stores.ROS2_HUMBLE)
    for name in ('ArmCommandStatus','HandTelemetryStatus'):
        store.register(get_types_from_msg((WORKSPACE/f'schemas/{name}.msg').read_text(),f'teleop_interfaces/msg/{name}'))
    Time=store.types['builtin_interfaces/msg/Time'];Header=store.types['std_msgs/msg/Header']
    Joint=store.types['sensor_msgs/msg/JointState'];Image=store.types['sensor_msgs/msg/Image']
    specs={v['topic']:v['type'] for k,v in contract['topics'].items() if k not in ('cam0_depth_info',) and 'camera_info' not in v['topic']}
    # The contract includes no tactile topics: extras are mandatory at the new target boundary.
    specs.update({v['topic']:('geometry_msgs/msg/WrenchStamped' if v['kind']=='wrench' else 'sensor_msgs/msg/Image')
                  for v in modalities(robot).values()})
    db=sqlite3.connect(root/'data.db3')
    db.executescript('CREATE TABLE topics(id INTEGER PRIMARY KEY,name TEXT,type TEXT,serialization_format TEXT,offered_qos_profiles TEXT); CREATE TABLE messages(id INTEGER PRIMARY KEY,topic_id INTEGER,timestamp INTEGER,data BLOB); CREATE INDEX timestamp_idx ON messages(timestamp);')
    start=1_000_000_000;names=contract['source_joint_names'];counts={}
    topic_keys={v['topic']:k for k,v in contract['topics'].items()}
    for tid,(topic,kind) in enumerate(specs.items(),1):
        db.execute('INSERT INTO topics VALUES(?,?,?,?,?)',(tid,topic,kind,'cdr',''))
        for i in range(frames):
            t=start+i*1_000_000_000//30;header=Header(Time(*divmod(t,10**9)),'sensor_frame')
            if kind.endswith('/JointState'):
                key=topic_keys[topic]
                if key=='validated_arm_action':joints=names['validated_left_arm']+names['validated_right_arm']
                else:
                    group=key.removesuffix('_state').removesuffix('_action')
                    field=(f'measured_{group}' if key.endswith('_state') and (group.endswith('arm') or robot=='sharpa') else
                           f'command_{group}' if robot=='sharpa' and key.endswith('_action') else group)
                    joints=names[field]
                # Reverse wire order; values retain a deterministic association with each name.
                joints=list(reversed(joints));q=np.arange(len(joints),dtype=np.float64)/100+i/50
                msg=Joint(header,joints,q,np.zeros(len(q)),np.zeros(0))
            elif kind.endswith('/Image'):
                depth='/depth/' in topic;data=np.full((8,8),1000+i if depth else 20+i,dtype=np.uint16 if depth else np.uint8)
                tactile='/tactile/' in topic
                if not depth and not tactile:data=np.repeat(data[...,None],3,axis=2)
                msg=Image(header,8,8,'16UC1' if depth else 'mono8' if tactile else 'rgb8',0,
                          16 if depth else 8 if tactile else 24,np.frombuffer(data.tobytes(),np.uint8).copy())
            elif kind.endswith('/ArmCommandStatus'):
                msg=store.types[kind](header,'gello','session',i,['left','right'],[])
            elif kind.endswith('/HandTelemetryStatus'):
                T=store.types[kind]
                for side in ('left','right'):
                    msg=T(header,'session',i,side,Time(*divmod(t,10**9)),0,0,0,True,True,True,0.,0.,'measured',0,0,0,0,0,0)
                    db.execute('INSERT INTO messages(topic_id,timestamp,data) VALUES(?,?,?)',(tid,t+100,bytes(store.serialize_cdr(msg,kind))))
                continue
            elif kind.endswith('/WrenchStamped'):
                V=store.types['geometry_msgs/msg/Vector3'];W=store.types['geometry_msgs/msg/Wrench']
                msg=store.types[kind](header,W(V(i+.1,i+.2,i+.3),V(i+.4,i+.5,i+.6)))
            else:raise ValueError(kind)
            db.execute('INSERT INTO messages(topic_id,timestamp,data) VALUES(?,?,?)',(tid,t+100,bytes(store.serialize_cdr(msg,kind))))
        counts[topic]=db.execute('SELECT count(*) FROM messages WHERE topic_id=?',(tid,)).fetchone()[0]
    # Telemetry emits two messages per frame.
    counts={topic:db.execute('SELECT count(*) FROM messages WHERE topic_id=?',(tid,)).fetchone()[0]
            for tid,topic in db.execute('SELECT id,name FROM topics')}
    total=sum(counts.values());db.commit();db.close()
    end=start+(frames-1)*1_000_000_000//30
    meta={'rosbag2_bagfile_information':{'version':5,'storage_identifier':'sqlite3','relative_file_paths':['data.db3'],
          'duration':{'nanoseconds':end-start},'starting_time':{'nanoseconds_since_epoch':start+100},
          'message_count':total,'topics_with_message_count':[{'topic_metadata':{'name':t,'type':typ,'serialization_format':'cdr','offered_qos_profiles':''},'message_count':counts[t]} for t,typ in specs.items()],
          'compression_format':'','compression_mode':''}}
    (root/'metadata.yaml').write_text(yaml.safe_dump(meta))
    (root/'collection_state.json').write_text(json.dumps({'state':'finalized','finalized':True,'failures':[],
       'validation_report':{'source_effective_start_time_ns':start,'source_effective_end_time_ns':end}}))
    return root
