"""Frame-based annotations; original LeRobot datasets are never modified."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import uuid

import numpy as np
import pyarrow.parquet as pq

from convest.training.adatempo.lerobot_io import data_path, video_path, values

# Fine labels determine coarse labels, so the two tracks cannot disagree.
LABELS = {
    'unknown': ('待确认', '待确认'),
    'grasp_truss': ('拿起番茄', '右手抓稳整串番茄'),
    'lift_truss': ('拿起番茄', '右手提起番茄串并移至中间位置'),
    'grasp_fruit': ('摘取番茄', '左手接近并抓紧一颗番茄，右手稳定托持番茄串'),
    'pick_and_place': ('摘取番茄', '左手摘下番茄并放入左侧果盘，右手稳定托持番茄串'),
    'fruit_drop': ('摘取番茄', '番茄意外脱落或从左手滑落，未完成受控投放'),
    'harvest_retry': ('摘取番茄', '采摘失败后重抓、恢复或调整'),
    'harvest_uncertain': ('摘取番茄', '采摘过程遮挡或脱离、持果状态待确认'),
    'return_truss': ('放回番茄', '右手将番茄串放回右侧白盘并松手'),
    'rotate_stem': ('旋转番茄杆', '右手手指移动并旋转番茄杆'),
    'return_stem': ('放回番茄', '全部摘完后右手放回番茄杆'),
    'stop': ('结束', '停止任务'),
    'idle': ('其他', '等待或暂停'),
    'retry': ('其他', '失败、重试或调整'),
}
OUTCOMES = {'unknown', 'success', 'failure', 'incomplete'}


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        with temporary.open('w') as f:
            json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def segment(start, end, label='unknown', *, reviewed=False, **extra):
    coarse, text = LABELS[label]
    return dict(start_frame=int(start), end_frame=int(end), label=label,
                coarse=coarse, text=text, reviewed=reviewed, **extra)


def validate_segments(segments, length, fps):
    if not isinstance(segments, list) or not segments or len(segments) > length:
        raise ValueError('必须用连续区间覆盖整个 episode')
    result, end = [], 0
    for item in segments:
        a, b, label = item.get('start_frame'), item.get('end_frame'), item.get('label')
        if type(a) is not int or type(b) is not int or a != end or not a < b <= length:
            raise ValueError('帧区间须连续、无重叠、无空段，采用 [开始,结束)')
        if label not in LABELS or type(item.get('reviewed', False)) is not bool:
            raise ValueError('标签或审核状态无效')
        if label == 'unknown' and item.get('reviewed'):
            raise ValueError('待确认片段不能标为审核通过')
        score = item.get('similarity_margin')
        extra = {}
        if score is not None:
            if type(score) not in (int, float) or not np.isfinite(score) or not 0 <= score <= 1:
                raise ValueError('相似度分差无效')
            extra['similarity_margin'] = float(score)
        for key in ('model_probability', 'model_confidence'):
            if key in item:
                value = item[key]
                if type(value) not in (int, float) or not np.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError('模型概率或 confidence 无效')
                extra[key] = float(value)
        result.append(segment(a, b, label, reviewed=item.get('reviewed', False),
                              start_seconds=a/fps, end_seconds=b/fps, **extra))
        end = b
    if end != length:
        raise ValueError('标签未覆盖 episode 结尾')
    return result


class Episode:
    def __init__(self, root, index):
        self.root = Path(root).resolve(strict=True)
        self.info = json.loads((self.root/'meta/info.json').read_text())
        if self.info.get('codebase_version') != 'v2.1':
            raise ValueError('当前仅支持 LeRobot v2.1')
        self.fps = float(self.info['fps'])
        if not np.isfinite(self.fps) or not 0 < self.fps <= 240:
            raise ValueError('无效 FPS')
        entries = [json.loads(s) for s in (self.root/'meta/episodes.jsonl').read_text().splitlines() if s.strip()]
        found = [e for e in entries if e['episode_index'] == index]
        if len(found) != 1:
            raise ValueError('episode 不存在或重复')
        self.index, self.entry = index, found[0]
        self.parquet = data_path(self.root, self.info, index)
        self.table = pq.read_table(self.parquet)
        self.length = len(self.table)
        if self.length < 2 or self.length != self.entry['length']:
            raise ValueError('Parquet 与元数据帧数不一致')
        if not np.array_equal(values(self.table, 'frame_index'), np.arange(self.length)):
            raise ValueError('frame_index 不是从零连续的帧号')
        if not np.all(values(self.table, 'episode_index') == index):
            raise ValueError('episode_index 不一致')
        if not np.allclose(values(self.table, 'timestamp'), np.arange(self.length)/self.fps, atol=1e-4, rtol=0):
            raise ValueError('timestamp 与 FPS 不一致')
        self.cameras = {k: video_path(self.root, self.info, index, k)
                        for k, f in self.info['features'].items() if f['dtype'] == 'video'}
        if not self.cameras:
            raise ValueError('缺少视频相机')
        self.id = hashlib.sha256((str(self.root)+'#'+str(index)).encode()).hexdigest()[:24]

    def identity(self, *, videos=True):
        files = [self.root/'meta/info.json', self.root/'meta/episodes.jsonl', self.root/'meta/tasks.jsonl', self.parquet]
        mapping_path = self.root/f'processing/episode_{self.index:06d}.json'
        if mapping_path.exists(): files.append(mapping_path)
        if json.loads((self.root/'meta/info.json').read_text()) != self.info:
            raise ValueError('源元数据已变化，请重新载入')
        entries = [json.loads(s) for s in (self.root/'meta/episodes.jsonl').read_text().splitlines() if s.strip()]
        if [e for e in entries if e['episode_index'] == self.index] != [self.entry]:
            raise ValueError('源 episode 元数据已变化，请重新载入')
        if videos:
            files += list(self.cameras.values())
        before = [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in files]
        hashes = {str(p.relative_to(self.root)): sha(p) for p in files}
        after = [(str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in files]
        if before != after:
            raise ValueError('源数据在读取期间发生变化')
        # Refuse a Parquet replaced after this Episode was loaded.
        if not pq.read_table(self.parquet).equals(self.table):
            raise ValueError('源 Parquet 已变化，请重新载入')
        result = {'dataset': str(self.root), 'episode_index': self.index,
                  'source_recording_id': self.entry.get('source_recording_id'),
                  'length': self.length, 'fps': self.fps, 'sha256': hashes}
        receipt_path = self.root/'cache-source.json'
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            manifest = receipt.get('manifest', {})
            if receipt.get('all_selected_sha256_verified') is not True or any(
                    manifest.get('files', {}).get(name, {}).get('sha256') != digest for name,digest in hashes.items()):
                raise ValueError('本地缓存与远端来源校验凭据不一致')
            result['remote_source'] = {'host':receipt['host'], 'dataset':manifest['dataset']}
        return result

    def validate_media(self, output, identity):
        """Full decode/PTS validation once for each exact source fingerprint."""
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        path = output/'media-validation'/f'{digest}.json'
        if path.exists():
            receipt = json.loads(path.read_text())
            if receipt.get('source') == identity and receipt.get('passed') is True: return receipt
        import av
        cameras = {}
        for key, video in self.cameras.items():
            count = 0
            expected = self.info['features'][key].get('shape')
            with av.open(str(video)) as container:
                for count, frame in enumerate(container.decode(video=0), 1):
                    if expected and (frame.height != expected[0] or frame.width != expected[1]):
                        raise ValueError(f'视频尺寸与元数据不一致: {key}')
                    if frame.time is None or abs(float(frame.time)-(count-1)/self.fps) > .51/self.fps:
                        raise ValueError(f'视频时间轴不同步: {key}/{count-1}')
            if count != self.length: raise ValueError(f'视频与 Parquet 帧数不同步: {key}')
            cameras[key] = {'decoded_frames':count, 'pts_checked':True}
        if self.identity() != identity: raise ValueError('视频检查期间源数据发生变化')
        receipt = {'source':identity, 'passed':True, 'cameras':cameras}
        atomic_json(path, receipt)
        return receipt

    def detail(self):
        return {'id': self.id, 'dataset': str(self.root), 'episode_index': self.index,
                'length': self.length, 'fps': self.fps, 'duration': self.length/self.fps,
                'cameras': list(self.cameras), 'source_recording_id': self.entry.get('source_recording_id')}


class Store:
    def __init__(self, roots, output):
        self.roots = [Path(p).expanduser().resolve(strict=True) for p in roots]
        self.output = Path(output).expanduser().resolve()
        if any(self.output == p or self.output.is_relative_to(p) or p.is_relative_to(self.output)
               for p in self.roots):
            raise ValueError('标注输出目录必须与输入数据根完全分离')
        self.output.mkdir(parents=True, exist_ok=True)
        # One review process owns an output directory; protects revisions across processes.
        import fcntl
        self.lockfile = (self.output/'.review.lock').open('a')
        fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.lock = threading.RLock()
        self.entries = {}
        self.scan()

    def scan(self):
        entries = {}
        for root in self.roots:
            paths = [root/'meta/info.json'] if (root/'meta/info.json').is_file() else sorted(root.rglob('meta/info.json'))
            for info_path in paths:
                dataset = info_path.parent.parent.resolve()
                if not dataset.is_relative_to(root):
                    raise ValueError('数据集软链接越过输入根')
                info = json.loads(info_path.read_text())
                for line in (dataset/'meta/episodes.jsonl').read_text().splitlines():
                    if not line.strip(): continue
                    entry = json.loads(line); ep = entry['episode_index']
                    if type(ep) is not int or ep < 0:
                        raise ValueError('无效 episode_index')
                    if not data_path(dataset, info, ep).is_file(): continue  # explicitly partial local caches
                    id_ = hashlib.sha256((str(dataset)+'#'+str(ep)).encode()).hexdigest()[:24]
                    entries[id_] = {'id': id_, 'dataset': str(dataset), 'episode_index': ep,
                                    'length': entry['length'], 'fps': info['fps'], 'source_recording_id':entry.get('source_recording_id'),
                                    'variant':'speedup' if (dataset/f'processing/episode_{ep:06d}.json').exists() else 'normal'}
        self.entries = entries
        return list(entries.values())

    def episode(self, id_):
        e = self.entries[id_]
        return Episode(e['dataset'], e['episode_index'])

    def path(self, id_):
        if id_ not in self.entries: raise ValueError('未知 episode')
        return self.output/'annotations'/f'{id_}.json'

    def load(self, episode):
        path = self.path(episode.id)
        identity = episode.identity()
        episode.validate_media(self.output, identity)
        if path.exists():
            doc = json.loads(path.read_text())
            if doc['source'] != identity:
                raise ValueError('源文件指纹变化，旧标注不能直接复用')
            return doc
        return {'schema_version': 1, 'revision': 0, 'source': identity,
                'status': 'draft', 'origin': 'manual', 'outcome': 'unknown',
                'segments': [segment(0, episode.length)]}

    def save(self, episode, doc, *, origin='manual', provenance=None):
        with self.lock:
            current = self.load(episode)
            if type(doc.get('revision')) is not int or doc['revision'] != current['revision']:
                raise ValueError('标注已在其他窗口更新，请重新载入后修改')
            if doc.get('source', current['source']) != current['source']:
                raise ValueError('标注来源身份不匹配')
            segments = validate_segments(doc['segments'], episode.length, episode.fps)
            outcome = doc.get('outcome', 'unknown')
            if outcome not in OUTCOMES: raise ValueError('无效任务结果')
            result = {'schema_version': 1, 'revision': current['revision']+1,
                      'source': current['source'], 'origin': origin, 'outcome': outcome,
                      'status': 'reviewed' if all(s['reviewed'] for s in segments) else 'draft',
                      'segments': segments, 'provenance': provenance if provenance is not None else current.get('provenance', {})}
            # Retain every revision; a failed history write must not publish the new head.
            atomic_json(self.output/'history'/episode.id/f"{result['revision']:06d}.json", result)
            atomic_json(self.path(episode.id), result)
            return result


def motion_candidates(episode, minimum_seconds=1.0):
    """Suggest change points, never infer grasp success from motion alone."""
    from scipy.ndimage import gaussian_filter1d
    from scipy.signal import find_peaks
    names = episode.info['features']['observation.state']['names']
    cols = [i for i, n in enumerate(names) if n.endswith('.position')]
    q = values(episode.table, 'observation.state')[:, cols].astype(float)
    if not cols or not np.isfinite(q).all(): raise ValueError('缺少有效关节位置')
    smooth = gaussian_filter1d(q, max(1, episode.fps*.15), axis=0)
    scale = np.maximum(np.percentile(q, 90, axis=0)-np.percentile(q, 10, axis=0), .08)
    velocity = np.diff(smooth, axis=0, prepend=smooth[:1]) / scale
    change = np.linalg.norm(np.diff(velocity, axis=0, prepend=velocity[:1]), axis=1)
    change = gaussian_filter1d(change, max(1, episode.fps*.2))
    distance = max(1, int(episode.fps*minimum_seconds))
    prominence = max(float(np.percentile(change, 75)-np.percentile(change, 25)), 1e-5)
    peaks, _ = find_peaks(change, distance=distance, prominence=prominence)
    boundaries = [0] + [int(p) for p in peaks if distance <= p <= episode.length-distance] + [episode.length]
    return [segment(a, b, reason='motion_change_only') for a, b in zip(boundaries, boundaries[1:])]


def project(normal, speedup, annotation):
    """Labels follow nearest observation source frames, matching existing RGB resampling."""
    if normal.root == speedup.root:
        raise ValueError('normal 与 speedup 必须是不同数据集')
    for key in ('source_recording_id', 'source_start_ns', 'source_end_ns'):
        if normal.entry.get(key) is None or normal.entry[key] != speedup.entry.get(key):
            raise ValueError(f'来源配对身份不一致或缺失: {key}')
    if normal.fps != speedup.fps:
        raise ValueError('normal/speedup FPS 不一致')
    segments = validate_segments(annotation['segments'], normal.length, normal.fps)
    if annotation['source'] != normal.identity():
        raise ValueError('normal 标注指纹与源数据不一致')
    mapping_path = speedup.root/f'processing/episode_{speedup.index:06d}.json'
    mapping = json.loads(mapping_path.read_text())
    if mapping.get('episode_index') != speedup.index or mapping.get('source_length') != normal.length:
        raise ValueError('来源映射 episode 或帧数不匹配')
    obs, act = np.asarray(mapping['source_index'], float), np.asarray(mapping['action_source_index'], float)
    for a in (obs, act):
        if a.shape != (speedup.length,) or not np.isfinite(a).all() or np.any(a < 0) or np.any(a > normal.length-1) or np.any(np.diff(a) <= 0):
            raise ValueError('来源帧映射长度、范围或顺序无效')
    if obs[0] != 0 or act[-1] != normal.length-1 or not np.all(act > obs) or not np.allclose(obs[1:], act[:-1], atol=1e-9, rtol=0):
        raise ValueError('speedup 下一步 action 映射不一致')
    nearest = np.rint(obs).astype(int)
    if not np.array_equal(values(speedup.table, 'source_timestamp_ns'), values(normal.table, 'source_timestamp_ns')[nearest]):
        raise ValueError('speedup 观测来源时间戳与 normal 不匹配')
    ends = np.asarray([s['end_frame'] for s in segments])
    ids = np.searchsorted(ends, nearest, side='right')
    starts = np.r_[0, np.flatnonzero(np.diff(ids))+1]
    stops = np.r_[starts[1:], speedup.length]
    projected = [segment(a, b, segments[int(ids[a])]['label']) for a, b in zip(starts, stops)]
    action_ids = np.searchsorted(ends, np.rint(act).astype(int), side='right')
    return {'schema_version': 1, 'source': speedup.identity(), 'normal_source': annotation['source'],
            'normal_revision': annotation['revision'], 'normal_status': annotation['status'],
            'status': 'draft', 'outcome': annotation.get('outcome', 'unknown'),
            'mapping_sha256': sha(mapping_path), 'policy': 'nearest_observation_source_frame',
            'collapsed_normal_segments': sorted(set(range(len(segments)))-set(ids.tolist())),
            'action_crosses_boundary_frames': np.flatnonzero(ids != action_ids).tolist(),
            'segments': validate_segments(projected, speedup.length, speedup.fps)}
