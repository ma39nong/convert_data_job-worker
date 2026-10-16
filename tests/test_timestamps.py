import sqlite3

import numpy as np
import pytest
import yaml
from rosbags.typesys import Stores, get_typestore, get_types_from_msg

from bag_worker.bag import Bag, connect
from bag_worker.timestamps import TimeMap, TimestampRewriter, make_typestore
from test_bag import BASE, make_bag


def test_piecewise_mapping():
    m=TimeMap(BASE,[(10,20),(30,35)])
    assert [m(BASE+t)-BASE for t in [-5,0,9,10,15,19,20,21,29,30,34,35,40]] == [-5,0,9,10,10,10,10,11,19,20,20,20,25]


@pytest.mark.parametrize('split',[False,True])
def test_default_compact_across_all_topics_and_files(tmp_path,split):
    bag=make_bag(tmp_path/'source',split)
    report=bag.export([[1,3],[4,4.5]],tmp_path/'compact')
    result=Bag(tmp_path/'compact')
    assert report['compact'] is True
    assert report['unmodified_payload_types']=={'custom/msg/Opaque':9}
    assert result.count==9
    for refs in result.refs.values():
        assert [r[0] for r in refs]==[BASE,BASE+10**9,BASE+2_500_000_000]
    meta=yaml.safe_load((tmp_path/'compact/metadata.yaml').read_text())['rosbag2_bagfile_information']
    assert meta['duration']['nanoseconds']==2_500_000_000


@pytest.mark.parametrize('little_endian',[False,True])
def test_nested_timestamps_duration_zero_and_payload_unchanged(little_endian):
    store=get_typestore(Stores.ROS2_HUMBLE)
    store.register(get_types_from_msg('''std_msgs/Header header
builtin_interfaces/Time[] samples
builtin_interfaces/Duration exposure
float64[] joints
uint8[] pixels
string description
''','test_msgs/msg/Sample'))
    T=store.types['builtin_interfaces/msg/Time'];D=store.types['builtin_interfaces/msg/Duration']
    H=store.types['std_msgs/msg/Header'];M=store.types['test_msgs/msg/Sample']
    message=M(H(T(105,123),'camera'),[T(0,0),T(106,456),T(103,789)],D(12,345),np.array([.25,-2.5]),np.arange(127,dtype=np.uint8),'测试相机')
    raw=bytes(store.serialize_cdr(message,message.__msgtype__,little_endian=little_endian))
    mapping=TimeMap(100*10**9,[(2*10**9,4*10**9)])
    rw=TimestampRewriter(store,mapping)
    out=rw.rewrite(raw,message.__msgtype__)
    result=store.deserialize_cdr(out,message.__msgtype__)
    assert result.header.stamp==T(103,123)
    assert result.samples==[T(0,0),T(104,456),T(102,0)]
    assert result.exposure==message.exposure
    assert result.header.frame_id=='camera'
    assert result.description==message.description
    assert np.array_equal(result.joints,message.joints)
    assert np.array_equal(result.pixels,message.pixels)
    assert len(raw)==len(out)
    assert sum(a!=b for a,b in zip(raw,out))<=3*8
    assert rw.changed==3 and rw.clamped==1


def test_unknown_type_not_guessed_and_bad_encoding_rejected():
    store=get_typestore(Stores.ROS2_HUMBLE)
    rw=TimestampRewriter(store,TimeMap(0,[(1,2)]))
    assert not rw.supported('unknown/msg/Status')
    with pytest.raises(ValueError,match='缺少'):
        rw.rewrite(b'\0'*100,'unknown/msg/Status')
    with pytest.raises(ValueError,match='CDR1'):
        rw.rewrite(b'\0\x07\0\0','std_msgs/msg/Header')


def test_custom_definitions_loaded_from_folder(tmp_path):
    msgdir=tmp_path/'types'/'robot_interfaces'/'msg';msgdir.mkdir(parents=True)
    (msgdir/'Status.msg').write_text('std_msgs/Header header\nfloat64 position\n')
    store=make_typestore(custom_dir=tmp_path/'types')
    assert 'robot_interfaces/msg/Status' in store.types
    assert TimestampRewriter(store,TimeMap(0,[])).supported('robot_interfaces/msg/Status')


def test_arbitrary_topic_names_and_custom_timestamp_export(tmp_path):
    bag=make_bag(tmp_path/'source')
    definition='std_msgs/Header header\nfloat64 position\n'
    store=make_typestore([('robot_msgs/msg/Feedback',definition,'ros2msg')])
    M=store.types['robot_msgs/msg/Feedback'];H=store.types['std_msgs/msg/Header'];T=store.types['builtin_interfaces/msg/Time']
    with sqlite3.connect(bag.files[0]) as db:
        db.execute('CREATE TABLE message_definitions(id INTEGER PRIMARY KEY,topic_type TEXT,encoding TEXT,encoded_message_definition TEXT,type_description_hash TEXT)')
        db.execute('INSERT INTO message_definitions VALUES(1,?,?,?,?)',('robot_msgs/msg/Feedback','ros2msg',definition,''))
        db.execute("UPDATE topics SET type='robot_msgs/msg/Feedback',name='/arbitrary/feedback' WHERE id=1")
        for ident,t in db.execute('SELECT id,timestamp FROM messages WHERE topic_id=1').fetchall():
            s,n=divmod(t,10**9)
            data=store.serialize_cdr(M(H(T(s,n),'frame'),42.5),'robot_msgs/msg/Feedback')
            db.execute('UPDATE messages SET data=? WHERE id=?',(data,ident))
    bag=Bag(bag.root)
    report=bag.export([[1,3]],tmp_path/'edited')
    assert report['time_fields_updated']==3
    with connect(tmp_path/'edited/edited_0.db3') as db:
        for stamp,data in db.execute('SELECT timestamp,data FROM messages WHERE topic_id=1 ORDER BY timestamp'):
            msg=store.deserialize_cdr(data,'robot_msgs/msg/Feedback')
            assert stamp==msg.header.stamp.sec*10**9+msg.header.stamp.nanosec
            assert msg.position==42.5


def test_jointstate_detected_without_fixed_topic_name(tmp_path):
    bag=make_bag(tmp_path/'source')
    with sqlite3.connect(bag.files[0]) as db:
        db.execute("UPDATE topics SET name='/my_robot/q',type='sensor_msgs/msg/JointState' WHERE id=1")
    bag=Bag(bag.root)
    assert bag.joints==['/my_robot/q']
