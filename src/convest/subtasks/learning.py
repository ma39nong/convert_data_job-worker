"""Local example retrieval with visual/robot features and explicit abstention.

The score is a distance margin, not a calibrated probability of correctness.
"""
import hashlib
import json

import av
import numpy as np
from scipy.ndimage import generic_filter

from .core import LABELS, atomic_json, motion_candidates, segment
from convest.training.adatempo.lerobot_io import values


def descriptors(episode, cache_root):
    identity = episode.identity()
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    path = cache_root/'features'/f'{key}-visual-state-v1.npz'
    if path.exists():
        with np.load(path, allow_pickle=False) as f:
            return f['frames'], f['features'], str(f['schema'])
    names = episode.info['features']['observation.state']['names']
    positions = [i for i, n in enumerate(names) if n.endswith('.position')]
    if not positions: raise ValueError('缺少关节位置')
    q = values(episode.table, 'observation.state')[:, positions].astype(float)
    if not np.isfinite(q).all(): raise ValueError('关节数据非有限值')
    stride = max(1, round(episode.fps/3))
    frames = np.unique(np.r_[np.arange(0, episode.length, stride), episode.length-1])
    velocity = np.gradient(q, axis=0)*episode.fps
    blocks = [q[frames], velocity[frames]]
    # Decode all frames once; require true video length and timestamps, including cache creation.
    for camera, video in sorted(episode.cameras.items()):
        images, picked, count = [], 0, 0
        with av.open(str(video)) as container:
            for count, frame in enumerate(container.decode(video=0), 1):
                idx = count-1
                if frame.time is None or abs(float(frame.time)-idx/episode.fps) > .51/episode.fps:
                    raise ValueError(f'视频时间轴不同步: {camera}/{idx}')
                if picked < len(frames) and idx == frames[picked]:
                    rgb = frame.reformat(width=32, height=32, format='rgb24').to_ndarray().astype(float)/255
                    grid = rgb.reshape(4, 8, 4, 8, 3)
                    images.append(np.r_[grid.mean(axis=(1, 3)).ravel(), grid.std(axis=(1, 3)).ravel()])
                    picked += 1
        if count != episode.length or picked != len(frames):
            raise ValueError(f'视频与 Parquet 帧数不同步: {camera}')
        blocks.append(np.asarray(images))
    features = np.concatenate(blocks, axis=1)
    schema = hashlib.sha256(json.dumps({'positions': [names[i] for i in positions],
                       'cameras': sorted(episode.cameras), 'version': 'visual-state-v1'}, sort_keys=True).encode()).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Output directory has a process lock; rename provides an atomic feature cache.
    temporary = path.with_suffix('.tmp.npz')
    np.savez_compressed(temporary, frames=frames, features=features, schema=schema)
    temporary.replace(path)
    return frames, features, schema


def train(store, *, allow_draft=False):
    groups = {}
    for item in store.entries.values():
        episode = store.episode(item['id'])
        if (episode.root/f'processing/episode_{episode.index:06d}.json').exists(): continue
        if not store.path(episode.id).exists(): continue
        doc = store.load(episode)
        selected = [s for s in doc['segments'] if s['label'] != 'unknown' and (s['reviewed'] or allow_draft)]
        if not selected: continue
        frames, features, schema = descriptors(episode, store.output)
        group = groups.setdefault(schema, {'x': [], 'labels': [], 'sources': []})
        used = 0
        for s in selected:
            # Avoid teaching transition frames; a short segment still contributes central samples.
            padding = min(round(episode.fps*.3), (s['end_frame']-s['start_frame'])//4)
            mask = (frames >= s['start_frame']+padding) & (frames < s['end_frame']-padding)
            rows = features[mask][::max(1, int(np.count_nonzero(mask)/150))]
            group['x'].extend(rows.tolist());group['labels'].extend([s['label']]*len(rows));used += len(rows)
        # Bound memory and quadratic radius estimation as references accumulate.
        if group['labels']:
            chosen = []
            label_array = np.asarray(group['labels'])
            for label in sorted(set(group['labels'])):
                positions = np.flatnonzero(label_array == label)
                chosen.extend(positions[np.linspace(0,len(positions)-1,min(300,len(positions)),dtype=int)].tolist())
            group['x'] = [group['x'][i] for i in chosen]
            group['labels'] = [group['labels'][i] for i in chosen]
        if used:
            group['sources'].append({'episode_id': episode.id, 'revision': doc['revision'], 'identity': doc['source']})
    models = []
    for schema, g in groups.items():
        x = np.asarray(g['x'], float)
        if len(x) < 6 or len(set(g['labels'])) < 2: continue
        center = np.median(x, axis=0)
        scale = np.maximum(np.percentile(x, 90, axis=0)-np.percentile(x, 10, axis=0), .05)
        # Balance joint position, joint velocity and visual blocks by dimension.
        normalized = (x-center)/scale
        from scipy.spatial.distance import cdist
        distances = cdist(normalized, normalized)/np.sqrt(x.shape[1])
        np.fill_diagonal(distances, np.inf)
        nearest = distances.min(axis=1)
        radius = max(.8, float(np.percentile(nearest, 95))*3)
        model = {'schema_version': 1, 'feature_schema': schema, 'center': center.tolist(),
                 'scale': scale.tolist(), 'examples': normalized.tolist(), 'labels': g['labels'],
                 'sources': g['sources'], 'distance_limit': radius,
                 'reference_kind': 'unreviewed_pilot' if allow_draft else 'reviewed_examples',
                 'warning': ('参考来自 Codex 抽帧初标，未经人工确认。' if allow_draft else '')+'局部外观与姿态检索；相似度分差不是正确率，仍需人工审核'}
        atomic_json(store.output/'models'/f'{schema}.json', model)
        models.append({'schema': schema, 'samples': len(x), 'labels': sorted(set(g['labels'])),
                       'reference_episodes': len(g['sources']), 'reference_kind': model['reference_kind']})
    if not models: raise ValueError('请先确认至少两类动作的样例，再训练；Sharpa/Wuji 分开学习')
    return models


def predict(store, episode):
    frames, features, schema = descriptors(episode, store.output)
    path = store.output/'models'/f'{schema}.json'
    if not path.exists():
        return {'method': 'motion_candidates', 'warning': '尚无匹配的样例模型：仅给出动作边界，语义保持待确认',
                'segments': motion_candidates(episode)}
    model = json.loads(path.read_text())
    # Refuse stale examples after reference edits or source changes.
    for source in model['sources']:
        reference = store.episode(source['episode_id'])
        doc = store.load(reference)
        if doc['revision'] != source['revision'] or doc['source'] != source['identity']:
            raise ValueError('参考样例已修改，请重新训练')
    x = (features-np.asarray(model['center']))/np.asarray(model['scale'])
    refs = np.asarray(model['examples'])
    labels = np.asarray(model['labels'])
    classes = sorted(set(labels))
    from scipy.spatial.distance import cdist
    distances = cdist(x, refs)/np.sqrt(x.shape[1])
    # Mean of the nearest three references per class reduces one-frame outliers.
    per_class = np.stack([np.sort(distances[:, labels == c], axis=1)[:, :min(3, np.count_nonzero(labels == c))].mean(axis=1)
                          for c in classes], axis=1)
    order = np.argsort(per_class, axis=1)
    first = per_class[np.arange(len(x)), order[:, 0]]
    second = per_class[np.arange(len(x)), order[:, 1]]
    margin = np.clip((second-first)/np.maximum(second, 1e-8), 0, 1)
    ids = order[:, 0]
    ids = generic_filter(ids, lambda v: np.bincount(v.astype(int), minlength=len(classes)).argmax(), size=3, mode='nearest').astype(int)  # 3 Hz sampling: ~1 second local smoothing
    # Abstain using the selected class's distance as well as its competitors.
    chosen_distance = per_class[np.arange(len(x)), ids]
    alternative = per_class.copy(); alternative[np.arange(len(x)), ids] = np.inf
    competitor = alternative.min(axis=1)
    margin = np.clip((competitor-chosen_distance)/np.maximum(competitor, 1e-8), 0, 1)
    guessed = np.asarray([classes[i] for i in ids], dtype=object)
    guessed[(margin < .12) | (chosen_distance > model['distance_limit'])] = 'unknown'
    sample_starts = np.r_[0, ((frames[:-1]+frames[1:])//2)+1]
    runs = np.r_[0, np.flatnonzero(guessed[1:] != guessed[:-1])+1]
    run_stops = np.r_[runs[1:], len(frames)]
    segments = []
    for a, b in zip(runs, run_stops):
        start = int(sample_starts[a]);end = int(sample_starts[b]) if b < len(frames) else episode.length
        segments.append(segment(start, end, str(guessed[a]), similarity_margin=float(margin[a:b].mean())))
    return {'method': 'local_visual_state_examples_v1', 'reference_kind': model['reference_kind'],
            'in_reference_set': episode.id in [s['episode_id'] for s in model['sources']],
            'warning': model['warning'], 'segments': segments}
