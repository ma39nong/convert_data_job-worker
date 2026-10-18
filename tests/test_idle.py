import hashlib
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from rosbags.typesys import Stores, get_typestore

from bag_worker.bag import Bag, connect
from bag_worker.idle import IdleConfig, detect_runs, inspect_bag, trim_bag
from bag_worker.cli import main
from test_bag import BASE, make_bag


def detect(values, valid=None, **kwargs):
    q = np.zeros((len(values), 14))
    q[:, -1] = values  # Only one joint of the right arm moves.
    return detect_runs(np.arange(len(values))*10, q,
                       np.ones(len(values), bool) if valid is None else valid,
                       IdleConfig(position_tolerance_rad=0, motion_window_s=0, **kwargs))


def test_strictly_more_than_three_and_preserve_boundary_frames():
    assert detect([0, 1, 1, 1, 2]) == []
    assert detect([0, 1, 1, 1, 1, 2]) == [(20, 40, 4)]


def test_recording_edges_protected_and_internal_barriers_not_bridged():
    assert detect([1]*8) == []
    assert detect([0]+[1]*8) == []
    assert detect([1]*8+[2]) == []
    assert detect([0]+[1]*8+[2], [True]*5+[False]+[True]*4) == [(20,40,4)]


def test_one_arm_motion_retained():
    assert detect(list(range(20))) == []


def test_slow_drift_cannot_accumulate():
    values = np.zeros((20,14))
    values[:,0] = np.arange(20)*2e-5
    assert detect_runs(np.arange(20)*5_000_000, values, np.ones(20,bool), IdleConfig()) == []


@pytest.mark.parametrize('kwargs', [dict(position_tolerance_rad=float('nan')),
    dict(position_tolerance_rad=-1), dict(stationary_frames=1),dict(stationary_frames=3.2),
    dict(max_gap_factor=1),dict(max_pair_skew_ms=0),dict(left_topic='same',right_topic='same')])
def test_config_rejects_invalid_values(kwargs):
    with pytest.raises(ValueError):
        IdleConfig(**kwargs)


def real_fixture(root, bad=None):
    bag = make_bag(root)
    store = get_typestore(Stores.ROS2_HUMBLE)
    T=store.types['builtin_interfaces/msg/Time']; H=store.types['std_msgs/msg/Header']
    J=store.types['sensor_msgs/msg/JointState']
    # 0→1, a 6-frame stationary interval, then 1→2. Extra data spans both edges.
    vals=[0,1,1,1,1,1,1,2,3,4]
    with sqlite3.connect(bag.files[0]) as db:
        for side,tid in [('left',11),('right',12)]:
            db.execute('INSERT INTO topics VALUES(?,?,?,?,?)',(tid,f'/{side}/franka/joint_states',J.__msgtype__,'cdr',''))
            for i,v in enumerate(vals):
                stamp=BASE+i*5_000_000+(100_000 if side=='right' else 0)
                names=[f'{side}_j{j}' for j in range(7)]
                q=np.array([float(v)]+[0.0]*6)
                if i%2: names.reverse();q=q[::-1].copy()
                if bad=='nan' and i==4:q[0]=float('nan')
                if bad=='names' and i==4:names[0]='wrong'
                msg=J(H(T(*divmod(stamp,10**9)),''),names,q,np.zeros(0),np.zeros(0))
                db.execute('INSERT INTO messages(topic_id,timestamp,data) VALUES(?,?,?)',(tid,stamp,bytes(store.serialize_cdr(msg,J.__msgtype__))))
    return Bag(root)


def test_actual_cdr_named_alignment_and_full_export(tmp_path):
    bag=real_fixture(tmp_path/'source')
    before={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in bag.root.iterdir()}
    report=inspect_bag(bag, IdleConfig(motion_window_s=0))
    assert len(report['runs'])==1
    assert report['deleted_seconds']==pytest.approx(.015)
    output=tmp_path/'result'
    result=trim_bag(bag.root,output,IdleConfig(motion_window_s=0))
    assert result['auto_idle_detection']==report
    assert result['timestamps']=='compact_shared_time_mapping'
    assert Bag(output).count < bag.count
    assert before=={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in bag.root.iterdir()}
    assert json.loads((output/'trim_report.json').read_text())==result
    with pytest.raises(ValueError,match='已经裁剪'):
        trim_bag(output,tmp_path/'again')


def test_nan_breaks_run_and_changed_names_fail(tmp_path):
    assert inspect_bag(real_fixture(tmp_path/'nan','nan'),IdleConfig(motion_window_s=0))['runs']==[]
    with pytest.raises(ValueError,match='名称'):
        inspect_bag(real_fixture(tmp_path/'names','names'))


def test_missing_arm_is_error(tmp_path):
    with pytest.raises(ValueError,match='缺少'):
        inspect_bag(make_bag(tmp_path/'source'))


def test_many_intervals_and_nanosecond_boundaries(tmp_path):
    bag=make_bag(tmp_path/'source')
    ranges=[[str(i/1e6),str((i+0.5)/1e6)] for i in range(1500)]
    report=bag.export(ranges,tmp_path/'result',compact=False)
    assert report['deleted']==3
    assert report['kept']==15


def test_cli_inspect_and_existing_report(tmp_path,capsys):
    bag=real_fixture(tmp_path/'source')
    report=tmp_path/'inspect.json'
    assert main(['inspect',str(bag.root),'--report',str(report)])==0
    assert json.loads(report.read_text())['version']==3
    assert main(['inspect',str(bag.root),'--report',str(report)])==2


def test_batch_keeps_relative_paths_and_records_failure(tmp_path,capsys):
    source=tmp_path/'input';source.mkdir()
    real_fixture(source/'good'); make_bag(source/'bad')
    target=tmp_path/'out'
    assert main(['batch',str(source),'--output',str(target)])==2
    results=json.loads((target/'batch_report.json').read_text())
    assert {r['status'] for r in results}=={'completed','failed'}
    assert (target/'good/metadata.yaml').exists()
    assert not (target/'bad').exists()


def test_string_output_and_no_cuts(tmp_path):
    bag=real_fixture(tmp_path/'source')
    report=trim_bag(bag.root,str(tmp_path/'result'),IdleConfig(stationary_frames=1000))
    assert report['deleted']==0 and report['kept']==bag.count
    assert report['mapping']==[]


def test_export_exception_cleans_only_owned_partial(tmp_path):
    bag=real_fixture(tmp_path/'source')
    def fail(**kwargs):
        raise RuntimeError('injected disk failure')
    with pytest.raises(RuntimeError,match='injected'):
        trim_bag(bag.root,tmp_path/'result',progress=fail)
    assert not (tmp_path/'result').exists()
    assert not (tmp_path/'result.partial').exists()
    assert Bag(bag.root).count==bag.count


def motion_fixture(root, phase_ms=2.5, drift_rate=3e-5, moving_rate=.02, invalid_at=None):
    bag=make_bag(root)
    store=get_typestore(Stores.ROS2_HUMBLE)
    T=store.types['builtin_interfaces/msg/Time'];H=store.types['std_msgs/msg/Header']
    J=store.types['sensor_msgs/msg/JointState']
    with sqlite3.connect(bag.files[0]) as db:
        for side,tid in [('left',11),('right',12)]:
            db.execute('INSERT INTO topics VALUES(?,?,?,?,?)',(tid,f'/{side}/franka/joint_states',J.__msgtype__,'cdr',''))
            for i in range(1200):
                phase=(phase_ms/1000+.00012*np.sin(i*.7)) if side=='right' else 0
                seconds=i*.005+phase
                stamp=BASE+round(seconds*1e9)
                if invalid_at is not None and 3<=seconds<3.05:continue
                # One arm holds position throughout; only the other supplies motion boundaries.
                value=0 if side=='left' else (moving_rate*min(seconds,2)+drift_rate*np.clip(seconds-2,0,3)+moving_rate*max(seconds-5,0))
                q=np.full(7,2e-6*np.sin(i*.9));q[0]+=value
                msg=J(H(T(*divmod(stamp,10**9)),''),[f'{side}_j{j}' for j in range(7)],q,np.zeros(0),np.zeros(0))
                db.execute('INSERT INTO messages(topic_id,timestamp,data) VALUES(?,?,?)',(tid,stamp,bytes(store.serialize_cdr(msg,J.__msgtype__))))
    return Bag(root)


@pytest.mark.parametrize('phase_ms',[.1,2.5,4.9])
def test_phase_offset_noise_and_servo_drift_do_not_fragment_idle(tmp_path,phase_ms):
    report=inspect_bag(motion_fixture(tmp_path/'source',phase_ms=phase_ms))
    cuts=[tuple(map(float,p)) for p in report['ranges_seconds']]
    assert report['invalid_frames']<5
    assert len(cuts)==1
    assert 2.0<cuts[0][0]<2.2 and 4.8<cuts[0][1]<5.0
    assert cuts[0][1]-cuts[0][0]>2.6


def test_local_window_preserves_continuous_slow_motion(tmp_path):
    report=inspect_bag(motion_fixture(tmp_path/'source',drift_rate=.003,moving_rate=.003))
    assert report['runs']==[]


def test_local_window_does_not_bridge_missing_state(tmp_path):
    report=inspect_bag(motion_fixture(tmp_path/'source',invalid_at=3))
    assert report['invalid_frames']>0
    assert all(not (float(a)<3.025<float(b)) for a,b in report['ranges_seconds'])


def test_invalid_barrier_preserves_both_observed_quiet_subruns():
    # Long hold interrupted by one bad pairing. Keep the uncertainty interval,
    # cut the fully observed portions on either side, and keep moving samples.
    cuts=detect([0]+[1]*20+[2], [True]*10+[False]+[True]*11)
    assert cuts==[(20,90,9),(130,200,9)]
    assert all(not (a<=100<b) for a,b,_ in cuts)


def test_gap_after_unbounded_leading_idle_does_not_invent_motion_boundary():
    assert detect([1]*21+[2], [True]*10+[False]+[True]*11)==[]


def test_barrier_ending_hold_keeps_only_proven_portion_and_recording_tail():
    cuts=detect([0]+[1]*30, [True]*10+[False]+[True]*20)
    assert cuts==[]


def test_realistic_pairing_glitch_preserves_wait_without_threshold_changes(tmp_path):
    bag=motion_fixture(tmp_path/'source',invalid_at=3)
    report=inspect_bag(bag)
    cuts=[tuple(map(float,p)) for p in report['ranges_seconds']]
    assert len(cuts)==2
    assert cuts[0][0]>2 and cuts[0][1]<3
    assert cuts[1][0]>3.05 and cuts[1][1]<5
    assert sum(b-a for a,b in cuts)>2.5


def test_independent_coverage_audit_rejects_the_previously_dropped_hold():
    from bag_worker.idle_audit import audit_windows
    cfg=IdleConfig(position_tolerance_rad=0,motion_window_s=0)
    t=np.arange(22)*10;q=np.zeros((22,14));q[:,-1]=[0]+[1]*20+[2]
    valid=np.array([True]*10+[False]+[True]*11)
    old=audit_windows(t,q,valid,[],cfg,raise_on_error=False)
    assert old['status']=='failed' and len(old['missed_blocks'])==2
    fixed=audit_windows(t,q,valid,detect_runs(t,q,valid,cfg),cfg)
    assert fixed['status']=='passed'
    with pytest.raises(ValueError,match='Independent cleaning audit'):
        audit_windows(t,q,valid,[(20,200,20)],cfg)
