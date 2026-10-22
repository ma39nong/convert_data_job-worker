"""Training projection without changing native robot vectors or video bytes."""
from copy import deepcopy
import json
from pathlib import Path
import shutil

import numpy as np
import pyarrow.parquet as pq

from convest.jobs import checksum
from .adatempo.lerobot_io import (FIELDS, dump, lines, data_path, video_path,
                                 read_source, numeric_stats, aggregate_stats)
from .adatempo.lerobot_video import inspect_video
from .adatempo.lerobot_checks import same_stats, require

DIMENSIONS = {'sharpa': (116, 58), 'wuji': (108, 54)}


def projection(info, robot):
    if robot not in DIMENSIONS or info.get('robot_type') != 'dual_fr3_' + robot:
        raise ValueError('Robot type mismatch')
    for name, dimension in zip(('observation.state', 'action'), DIMENSIONS[robot]):
        feature = info['features'][name]
        names = feature.get('names', [])
        if (feature['shape'] != [dimension] or len(names) != dimension
                or len(set(names)) != dimension or feature['dtype'] != 'float32'):
            raise ValueError('Native robot dimension/names mismatch: ' + name)
    # Keep the actually recorded flags. Do not invent hand engagement values.
    fields = FIELDS - {'observation.engaged'}
    flag = ('observation.arm_engaged' if 'observation.arm_engaged' in info['features']
            else 'observation.engaged')
    fields = fields | {flag}
    cameras = {f'observation.images.cam{i}' for i in range(3)}
    keep = fields | cameras
    if not keep <= info['features'].keys():
        raise ValueError('Missing training fields: ' + str(keep - info['features'].keys()))
    result = deepcopy(info)
    result['features'] = {k: v for k, v in info['features'].items() if k in keep}
    if info['codebase_version'] != 'v2.1' or info['fps'] != 30:
        raise ValueError('Expected LeRobot v2.1, 30 Hz')
    if any(result['features'][k]['dtype'] != 'video' for k in cameras):
        raise ValueError('Expected three RGB video features')
    return result


def stats_for(root, info):
    records = []
    for entry in lines(root / 'meta/episodes.jsonl'):
        ep, n = entry['episode_index'], entry['length']
        stats = numeric_stats(pq.read_table(data_path(root, info, ep)))
        for key, feature in info['features'].items():
            if feature['dtype'] == 'video':
                stats[key] = inspect_video(video_path(root, info, ep, key), n,
                                           info['fps'], feature['shape'])
        records.append({'episode_index': ep, 'stats': stats})
    return records


def validate_normal(source, output, robot):
    source, output = Path(source), Path(output)
    original = json.loads((source / 'meta/info.json').read_text())
    info = json.loads((output / 'meta/info.json').read_text())
    require(info == projection(original, robot), 'Normal metadata differs from native projection')
    for name in ('episodes.jsonl', 'tasks.jsonl'):
        require((source / 'meta' / name).read_bytes() == (output / 'meta' / name).read_bytes(),
                'Normal tasks/episodes changed')
    read_source(output)  # indices, grid, finite values, names and every vector dimension
    total = videos = 0
    for entry in lines(output / 'meta/episodes.jsonl'):
        ep = entry['episode_index']
        table = pq.read_table(data_path(output, info, ep))
        old = pq.read_table(data_path(source, original, ep), columns=table.column_names)
        require(table.equals(old), 'Normal retained values/schema changed')
        total += len(table)
        for key, feature in info['features'].items():
            if feature['dtype'] == 'video':
                require(checksum(video_path(source, original, ep, key)) ==
                        checksum(video_path(output, info, ep, key)), 'Normal video bytes changed')
                videos += 1
    require(total == info['total_frames'] and videos == info['total_videos'],
            'Normal frame/video totals changed')
    expected = stats_for(output, info)  # full decode, dimensions, frame counts and PTS
    stats = lines(output / 'meta/episodes_stats.jsonl')
    require(len(stats) == len(expected), 'Normal statistics count mismatch')
    for a, b in zip(stats, expected):
        require(a['episode_index'] == b['episode_index'], 'Normal statistics episode mismatch')
        same_stats(a['stats'], b['stats'])
    same_stats(json.loads((output / 'meta/stats.json').read_text()),
               aggregate_stats([e['stats'] for e in expected]))
    return {'episodes': len(expected), 'frames': total, 'videos': videos,
            'native_dimensions': list(DIMENSIONS[robot]), 'all_values_and_video_bytes_checked': True,
            'full_video_decode_and_pts': True}


def export(source, output, robot):
    source, output = Path(source), Path(output)
    if any(output.iterdir()):
        raise FileExistsError('Normal staging must be empty')
    original = json.loads((source / 'meta/info.json').read_text())
    info = projection(original, robot)
    columns = [k for k, v in info['features'].items() if v['dtype'] != 'video']
    for entry in lines(source / 'meta/episodes.jsonl'):
        ep = entry['episode_index']
        dst = data_path(output, info, ep)
        dst.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pq.read_table(data_path(source, original, ep), columns=columns),
                       dst, compression='zstd', compression_level=3)
        for key, feature in info['features'].items():
            if feature['dtype'] == 'video':
                dst = video_path(output, info, ep, key)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(video_path(source, original, ep, key), dst)
    dump(output / 'meta/info.json', info)
    for name in ('tasks.jsonl', 'episodes.jsonl'):
        shutil.copyfile(source / 'meta' / name, output / 'meta' / name)
    stats = stats_for(output, info)
    (output / 'meta/episodes_stats.jsonl').write_text(''.join(json.dumps(s) + '\n' for s in stats))
    dump(output / 'meta/stats.json', aggregate_stats([e['stats'] for e in stats]))
    report = validate_normal(source, output, robot)
    dump(output / 'processing/normal.json', {
        'source': str(source), 'dropped_features': sorted(original['features'].keys() - info['features'].keys()),
        'vector_mapping': 'identity; native names, order, dimensions and values unchanged',
        'engagement': 'recorded flags only; no synthetic hand flags', 'validation': report})
    return report
