"""Optional regression against the user's actual episode36, read-only."""
import os
from pathlib import Path

import pytest

from bag_worker.bag import Bag
from bag_worker.idle import inspect_bag


@pytest.mark.skipif(not os.environ.get('BAG_WORKER_REAL_EPISODE36'),reason='Set BAG_WORKER_REAL_EPISODE36 to the original episode36 directory')
def test_episode36_long_pause_and_active_motion():
    source=Path(os.environ['BAG_WORKER_REAL_EPISODE36'])
    report=inspect_bag(Bag(source))
    cuts=[tuple(map(float,p)) for p in report['ranges_seconds']]
    long_pause=[(a,b) for a,b in cuts if a<12.6 and b>17.2]
    assert len(long_pause)==1
    assert long_pause[0][1]-long_pause[0][0]>4.7
    assert all(b<=18 or a>=20 for a,b in cuts), 'Active left-arm motion at 18–20 s must remain'
    assert report['invalid_frames']<10
