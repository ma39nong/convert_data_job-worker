"""Independent row-wise alignment oracle. Does not call conversion align/prepare."""
from types import SimpleNamespace
import numpy as np
from .sources.robot_rosbag2 import read_streams, modalities


def require(condition, message):
    if not condition:
        raise ValueError(message)


def runs(mask):
    start = None
    for i, flag in enumerate(np.r_[mask, False]):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            yield start, i
            start = None


def audit_recording(bag, contract, config, item):
    streams = read_streams(bag, contract)
    start, end = item['source_start_ns'], item['source_end_ns']
    # Enumerate grid times rather than using the conversion's vectorized grid builder.
    timeline = []
    n = 0
    while start + n * 1_000_000_000 // config['fps'] <= end:
        timeline.append(start + n * 1_000_000_000 // config['fps'])
        n += 1
    timeline = np.array(timeline, dtype=np.int64)
    indices = {k: np.array([int(np.searchsorted(s.times, t, side='right'))-1 for t in timeline])
               for k, s in streams.items()}
    limit = int(config['max_staleness_ms'] * 1e6)
    def fresh(key, i):
        j = indices[key][i]
        return j >= 0 and 0 <= int(timeline[i]-streams[key].times[j]) <= limit
    states, actions, engaged = [], [], []
    low_valid = np.ones(n, bool)
    for row, t in enumerate(timeline):
        state_row, action_row, active_row = [], [], []
        for group in ('left_arm', 'right_arm', 'left_hand', 'right_hand'):
            sk, ak, status_key = f'state.{group}', f'action.{group}', f'status.{group}'
            measured, command = streams[sk], streams[ak]
            si, ci = indices[sk][row], indices[ak][row]
            low_valid[row] &= fresh(sk, row)
            q = np.asarray(measured.values[max(0, si)])
            state_row.extend(q)
            if status_key not in streams:
                low_valid[row] &= fresh(ak, row)
                action_row.extend(command.values[max(0, ci)])
                active_row.append(np.nan)
                continue
            status = streams[status_key]
            sti = indices[status_key][row]
            active, good = status.values[max(0, sti)]
            low_valid[row] &= fresh(status_key, row) and bool(good)
            active_row.append(float(active))
            onset = max(0, sti)
            while onset > 0 and bool(status.values[onset-1][0]) == bool(active):
                onset -= 1
            transition = int(status.times[onset])
            if active:
                low_valid[row] &= fresh(ak, row) and int(command.times[max(0,ci)])+20_000_000 >= transition
                action_row.extend(command.values[max(0, ci)])
            else:
                held = int(np.searchsorted(command.times, transition, side='right'))-1
                indices[ak][row] = held
                if held >= 0:
                    action_row.extend(command.values[held])
                else:
                    seed = max(0, int(np.searchsorted(measured.times, transition, side='right'))-1)
                    action_row.extend(np.asarray(measured.values[seed])[:len(q)//2])
        states.append(state_row); actions.append(action_row); engaged.append(active_row)
    camera_good = {k: np.array([fresh(k,i) for i in range(n)]) for k in ('cam0','cam1','cam2')}
    rgb_raw_good = np.logical_and.reduce(list(camera_good.values()))
    rgb_accepted = []
    upper = int(config['camera_short_gap_max_age_ms']*1e6)
    for a,b in runs(~rgb_raw_good):
        if a == 0 or b == n or b-a > config['camera_short_gap_max_frames']:
            continue
        if not low_valid[a-1:b+1].all():
            continue
        if any(indices[k][i] < 0 or not 0 <= int(timeline[i]-streams[k].times[indices[k][i]]) <= upper
               for k in camera_good for i in range(a,b)):
            continue
        cameras = [k for k,g in camera_good.items() if not g[a:b].all()]
        for k in cameras:
            camera_good[k][a:b] = True
        rgb_accepted.append({'bounds':[a,b], 'cameras':cameras})
    valid = low_valid & np.logical_and.reduce(list(camera_good.values()))
    extras = {k: np.array([fresh(k,i) for i in range(n)]) for k in modalities(config['robot'])}
    depth = 'observation.depths.cam0'
    other_valid = valid.copy()
    for k,g in extras.items():
        if k != depth:
            other_valid &= g
    depth_accepted = []
    boundaries = bag.compact_time_map.compact_boundaries if bag.compact_time_map else ()
    for a,b in runs(~extras[depth]):
        if a == 0 or b == n or b-a > config['camera_short_gap_max_frames']:
            continue
        near = [c for c in boundaries if timeline[a-1] <= c <= timeline[b]]
        allowed = any('cam0' in g['cameras'] and max(a,g['bounds'][0]) < min(b,g['bounds'][1])
                      for g in rgb_accepted)
        if not near or not allowed or not other_valid[a-1:b+1].all():
            continue
        ages = [int(timeline[i]-streams[depth].times[max(0,indices[depth][i])]) for i in range(a,b)]
        if any(indices[depth][i] < 0 for i in range(a,b)) or any(v<0 or v>upper for v in ages):
            continue
        extras[depth][a:b] = True
        depth_accepted.append({'bounds':[a,b], 'compact_boundaries_ns':list(map(int,near)),
                               'actual_depth_age_ms':[v/1e6 for v in ages],
                               'sample_timestamp_ns':streams[depth].times[indices[depth][a:b]].tolist()})
    for g in extras.values():
        valid &= g
    good = np.flatnonzero(valid)
    bounds = (int(good[0]),int(good[-1])+1) if len(good) else None
    continuous = bool(bounds and valid[slice(*bounds)].all() and bounds[1]-bounds[0]>=config['min_segment_frames'])
    result = SimpleNamespace(timeline=timeline,state=np.asarray(states,np.float32),
                             action=np.asarray(actions,np.float32),engaged=np.asarray(engaged,np.float32),
                             source_indices=indices,valid_mask=valid,bounds=bounds,continuous=continuous,
                             report={'depth_splice_tolerance':{'accepted_frames':sum(g['bounds'][1]-g['bounds'][0] for g in depth_accepted),
                                                              'gaps':depth_accepted},
                                     'independent_alignment':True})
    return streams,result
