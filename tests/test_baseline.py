"""Regression against the accepted corrected episode36 artifact, including its short cut."""
from pathlib import Path
import json
import os

import pytest

from bag_worker.bag import Bag
from bag_worker.idle import default_config,inspect_bag


def test_default_is_original_baseline():
    config=default_config()
    assert not {'priority_policy','command_topic','hold_bridge_max_s'} & vars(config).keys()
    assert config.position_tolerance_rad==1e-4 and config.motion_window_s==.1


@pytest.mark.skipif(not os.environ.get('BAG_WORKER_REAL_ROOT'),reason='Set BAG_WORKER_REAL_ROOT for baseline regression')
@pytest.mark.parametrize('episode,count,seconds',[
    ('episode34',3,5.665938982),('episode35',6,5.805741843),('episode36',2,4.935757577),
    ('episode41',7,5.623356216),('episode54',10,19.444528337),('episode55',16,18.179064967)])
def test_default_reproduces_original_all_long_and_short_intervals(episode,count,seconds,monkeypatch):
    # No hand or command reader is allowed into this default path.
    import bag_worker.idle as idle
    original=idle.read_arm
    def arm_only(bag,topic,expected_joints=7):
        assert topic in ('/left/franka/joint_states','/right/franka/joint_states')
        return original(bag,topic,expected_joints)
    monkeypatch.setattr(idle,'read_arm',arm_only)
    bag=Bag(Path(os.environ['BAG_WORKER_REAL_ROOT'])/episode)
    report=inspect_bag(bag)
    assert report['algorithm']=='windowed_motion_causal_state' and report['version']==2
    assert 'priority_policy' not in report and 'hold_confirmation' not in report
    assert len(report['runs'])==count
    assert abs(report['deleted_seconds']-seconds)<1e-12
    if episode=='episode36':
        old=json.loads((Path(__file__).parents[1]/'docs/smoke_trim_report.json').read_text())
        assert [(int(x['start_ns']),int(x['end_ns'])) for x in report['runs']]==[(int(x['source_start_ns']),int(x['source_end_ns'])) for x in old['mapping']]
        assert len(report['runs'])==2  # Includes the ~15 ms hold; no 2 s classifier.
