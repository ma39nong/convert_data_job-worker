"""Match real dual-arm state frames and detect bounded stationary runs."""
from dataclasses import asdict, dataclass
from decimal import Decimal
from contextlib import ExitStack
from pathlib import Path

import numpy as np

from .bag import Bag, connect, decode_joint


@dataclass(frozen=True)
class IdleConfig:
    left_topic: str = '/left/franka/joint_states'
    right_topic: str = '/right/franka/joint_states'
    position_tolerance_rad: float = 1e-4
    motion_window_s: float = 0.1
    stationary_frames: int = 3  # Trigger only when the run has MORE frames.
    max_gap_factor: float = 3.0
    max_pair_skew_ms: float | None = None  # auto: 1.5 times the slower arm period (causal age)

    def __post_init__(self):
        if not np.isfinite(self.position_tolerance_rad) or self.position_tolerance_rad < 0:
            raise ValueError('position_tolerance_rad 必须为有限非负值')
        if not np.isfinite(self.motion_window_s) or self.motion_window_s < 0:
            raise ValueError('motion_window_s 必须为有限非负值')
        if type(self.stationary_frames) is not int or self.stationary_frames < 2:
            raise ValueError('stationary_frames 必须为至少 2 的整数')
        if not np.isfinite(self.max_gap_factor) or self.max_gap_factor <= 1:
            raise ValueError('max_gap_factor 必须大于 1')
        if self.max_pair_skew_ms is not None and (not np.isfinite(self.max_pair_skew_ms) or self.max_pair_skew_ms <= 0):
            raise ValueError('max_pair_skew_ms 必须为正数')
        if self.left_topic == self.right_topic:
            raise ValueError('左右臂话题不能相同')


def read_arm(bag, topic, expected_joints=7):
    if topic not in bag.topics or bag.topics[topic]['type'] != 'sensor_msgs/msg/JointState':
        raise ValueError(f'缺少双臂实测 JointState 话题：{topic}')
    rows = []
    names = None
    with ExitStack() as stack:
        dbs = [stack.enter_context(connect(p)) for p in bag.files]
        for stamp, fi, ident in bag.refs[topic]:
            msg = decode_joint(dbs[fi].execute('SELECT data FROM messages WHERE id=?', (ident,)).fetchone()[0])
            current = msg['names']
            if len(current) != expected_joints or len(set(current)) != expected_joints or len(msg['position']) != expected_joints:
                raise ValueError(f'{topic} 需要恰好 {expected_joints} 个有名称的机械臂关节')
            names = sorted(current) if names is None else names
            if set(current) != set(names):
                raise ValueError(f'{topic} 关节名称发生变化')
            values = np.array([msg['position'][current.index(n)] for n in names])
            # Non-finite samples become barriers, rather than being skipped.
            rows.append((stamp, values))
    if len(rows) < 2:
        raise ValueError(f'{topic} 至少需要两帧状态')
    # Equal record timestamps retain the last received message.
    unique = dict(rows)
    ts = np.array(list(unique), dtype=np.int64)
    q = np.array(list(unique.values()))
    if len(ts) < 2:
        raise ValueError(f'{topic} 至少需要两个不同时间戳')
    return ts, q, names


def detect_runs(times, positions, valid, config):
    """Classify motion over a local time window, then remove internal idle runs.

    A fixed noise envelope over an unbounded recording confuses slow servo drift
    with motion. Instead, every centered window must fit the configured envelope.
    Missing data invalidates the entire window, so holding stale data never proves
    stillness. With a zero window, compare adjacent samples (test/explicit mode).
    """
    count = len(times)
    valid = np.array(valid, dtype=bool, copy=True)
    stationary = np.zeros(count, dtype=bool)
    if not count:
        return []
    half_window = round(config.motion_window_s * 1e9 / 2)
    invalid_prefix = np.r_[0, np.cumsum(~valid)]
    for i in range(count):
        if half_window:
            lo = max(0, int(np.searchsorted(times, int(times[i])-half_window, side='right'))-1)
            hi = min(count-1, int(np.searchsorted(times, int(times[i])+half_window)))
            complete = times[lo] <= times[i]-half_window and times[hi] >= times[i]+half_window
        else:
            lo, hi, complete = max(0,i-1), i, True
        if not complete or invalid_prefix[hi+1] != invalid_prefix[lo]:
            valid[i] = False
            continue
        stationary[i] = np.max(np.ptp(positions[lo:hi+1], axis=0)) <= config.position_tolerance_rad

    cuts = []
    start = None
    bounded_left = False
    for i in range(count):
        if not valid[i]:
            start, bounded_left = None, False
            continue
        if start is None:
            start = i
            bounded_left = not stationary[i]
            continue
        if stationary[i]:
            continue
        if bounded_left and i-start > config.stationary_frames:
            a,b = int(times[start+1]),int(times[i-1])
            if a < b:
                cuts.append((a,b,i-start))
        start, bounded_left = i, True
    return cuts


def default_config():
    """Approved episode36 v2 baseline, with no duration split or hand veto."""
    return IdleConfig()


def inspect_bag(bag: Bag, config=None):
    config=default_config() if config is None else config
    arms = [read_arm(bag, t) for t in (config.left_topic, config.right_topic)]
    periods = [int(np.median(np.diff(a[0]))) for a in arms]
    # Use the slower measured state stream as the native frame reference.
    ref = int(periods[1] > periods[0])
    times, q, _ = arms[ref]
    other_t, other_q, _ = arms[1-ref]
    # Use only previously received state. A half-period nearest-neighbour gate
    # rejects healthy 200 Hz streams whose phases happen to differ by ~2.5 ms.
    indices = np.searchsorted(other_t, times, side='right')-1
    available = indices >= 0
    indices = np.maximum(indices, 0)
    age = times-other_t[indices]
    max_skew = int(config.max_pair_skew_ms*1e6) if config.max_pair_skew_ms is not None else round(max(periods)*1.5)
    positions = np.concatenate([q, other_q[indices]], axis=1)
    valid = available & (age >= 0) & (age <= max_skew) & np.isfinite(positions).all(axis=1)
    # Brief reuse due to asynchronous arrival is normal; freshness and actual
    # source-time gaps, rather than repeated index alone, establish validity.
    gaps = np.r_[False, (np.diff(times) > periods[ref]*config.max_gap_factor) |
                 (np.diff(other_t[indices]) > periods[1-ref]*config.max_gap_factor)]
    valid &= ~gaps
    cuts = detect_runs(times, positions, valid, config)
    ranges = [[str(Decimal(a-bag.start)/10**9), str(Decimal(b-bag.start)/10**9)] for a,b,_ in cuts]
    report = {
        'version': 2, 'algorithm': 'windowed_motion_causal_state', 'config': asdict(config), 'frame_basis': 'native_measured_arm_state',
        'reference_topic': (config.left_topic, config.right_topic)[ref],
        'time_basis': 'bag_record_timestamp', 'pairing': 'causal_past_state',
        'max_pair_skew_ns': max_skew, 'motion_window_s': config.motion_window_s,
        'frames': len(times), 'invalid_frames': int((~valid).sum()),
        'edge_policy': 'only_motion_bounded_internal_runs_keep_first_and_last',
        'arms': {topic: {'frames': len(a[0]), 'joint_names': a[2],
                        'median_period_ns': period, 'median_hz': 1e9/period,
                        'p99_period_ns': int(np.percentile(np.diff(a[0]), 99))}
                 for topic,a,period in zip((config.left_topic,config.right_topic), arms, periods)},
        'runs': [{'start_ns': str(a), 'end_ns': str(b), 'stationary_frames': n} for a,b,n in cuts],
        'ranges_seconds': ranges, 'deleted_seconds': sum(b-a for a,b,_ in cuts)/1e9,
    }
    bag.check_unchanged()
    return report


def trim_bag(source, output, config=None, custom_dir=None, progress=lambda **kw: None):
    bag = Bag(source, custom_dir)
    if (bag.root/'trim_report.json').exists():
        raise ValueError('输入已经裁剪；请使用原始 bag，避免重复压缩及来源映射歧义')
    report = inspect_bag(bag, config)
    return bag.export(report['ranges_seconds'], Path(output), progress=progress, detection=report)
