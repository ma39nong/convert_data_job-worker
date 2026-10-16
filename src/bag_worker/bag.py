"""Read-only SQLite ROS 2 inspection, synchronized filtering and compact export."""
from __future__ import annotations

import bisect
import copy
import json
import math
import shutil
import sqlite3
import struct
from collections import Counter
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

import numpy as np
import yaml

from .timestamps import TimeMap, TimestampRewriter, make_typestore


@contextmanager
def connect(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        yield db
    finally:
        db.close()


class CDR:
    """The standard Image / CompressedImage / JointState CDR1 layouts only."""
    def __init__(self, data):
        if data[:2] not in (b'\x00\x00', b'\x00\x01'):
            raise ValueError('预览暂不支持此序列化格式（需要 CDR1）。裁剪仍保留原始消息。')
        self.data, self.pos = memoryview(data)[4:], 0
        self.endian = '<' if data[1] == 1 else '>'

    def number(self, fmt, size):
        self.pos = (self.pos + size - 1) // size * size
        value = struct.unpack_from(self.endian + fmt, self.data, self.pos)[0]
        self.pos += size
        return value

    def u32(self):
        return self.number('I', 4)

    def string(self):
        n = self.u32()
        value = bytes(self.data[self.pos:self.pos + n - 1]).decode('utf-8', errors='replace')
        self.pos += n
        return value

    def header(self):
        sec, ns = self.number('i', 4), self.u32()
        return sec * 1_000_000_000 + ns, self.string()

    def byte_array(self):
        n = self.u32()
        value = self.data[self.pos:self.pos+n]
        if len(value) != n:
            raise ValueError('消息数据不完整')
        self.pos += n
        return value


def decode_joint(data):
    c = CDR(data)
    stamp, _ = c.header()
    names = [c.string() for _ in range(c.u32())]
    values = []
    for _ in range(3):
        n = c.u32()
        values.append([c.number('d', 8) for _ in range(n)])
    return {'stamp_ns': str(stamp), 'names': names, 'position': values[0],
            'velocity': values[1], 'effort': values[2]}


def normalized_ranges(ranges, duration, allow_empty=False):
    if not isinstance(ranges, list) or (not ranges and not allow_empty):
        raise ValueError('请添加 有效的需要删除的时间段')
    result = []
    for pair in ranges:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError('时间段应包含开始与结束秒数')
        try:
            a, b = (Decimal(str(v)) for v in pair)
        except InvalidOperation:
            raise ValueError('时间必须是有效数字') from None
        if not a.is_finite() or not b.is_finite() or not (0 <= a < b <= Decimal(str(duration))):
            raise ValueError(f'时间段必须满足 0 ≤ 开始 < 结束 ≤ {duration} 秒')
        result.append((int(a * 10**9), int(b * 10**9)))
    merged = []
    for a, b in sorted(result):
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


class Bag:
    def __init__(self, path, custom_dir=None):
        p = Path(path).expanduser().resolve()
        if p.name == 'metadata.yaml':
            p = p.parent
        if not p.exists():
            raise ValueError('路径不存在，请选择本机上的 ros2bag 文件夹或 .db3 文件')
        self.root = p if p.is_dir() else p.parent
        self.metadata = None
        if p.is_dir():
            meta = p / 'metadata.yaml'
            if meta.exists():
                self.metadata = yaml.safe_load(meta.read_text())['rosbag2_bagfile_information']
                if self.metadata.get('storage_identifier') != 'sqlite3':
                    raise ValueError('当前支持 SQLite3（.db3）ros2bag，暂不支持 MCAP')
                if self.metadata.get('compression_mode'):
                    raise ValueError('请先解压 ros2bag；当前不支持 rosbag 层文件/消息压缩')
                self.files = [(p / v).resolve() for v in self.metadata['relative_file_paths']]
                if any(not x.is_relative_to(p) for x in self.files):
                    raise ValueError('metadata.yaml 中的数据文件路径超出 bag 文件夹')
            else:
                self.files = sorted(p.glob('*.db3'))
        else:
            self.files = [p] if p.suffix == '.db3' else []
        if not self.files:
            raise ValueError('没有找到 .db3 数据文件')
        if any(not f.is_file() for f in self.files):
            raise ValueError('bag 的分卷文件缺失，请导入完整文件夹')
        self.path = str(p)
        self.name = p.stem
        self.topics = {}
        self.refs = {}
        self.file_stats = []
        definitions = []
        distro = 'humble'
        self.signatures = [(f.stat().st_size, f.stat().st_mtime_ns) for f in self.files]
        self.start = 2**63 - 1
        self.end = 0
        self.count = 0
        for fi, file in enumerate(self.files):
            with connect(file) as db:
                db.row_factory = sqlite3.Row
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'schema' in tables:
                    info = db.execute('SELECT ros_distro FROM schema LIMIT 1').fetchone()
                    if info and info[0]:
                        distro = info[0]
                if 'message_definitions' in tables:
                    definitions.extend(tuple(r) for r in db.execute(
                        'SELECT topic_type,encoded_message_definition,encoding FROM message_definitions'))
                local_topics = {}
                for row in db.execute('SELECT * FROM topics'):
                    topic = dict(row)
                    name = topic['name']
                    local_topics[topic['id']] = name
                    if name in self.topics and self.topics[name]['type'] != topic['type']:
                        raise ValueError('分卷之间存在名称相同但类型不同的话题')
                    self.topics.setdefault(name, topic)
                    self.refs.setdefault(name, [])
                n, lo, hi = 0, None, None
                for row in db.execute('SELECT id, topic_id, timestamp FROM messages ORDER BY timestamp'):
                    ident, tid, stamp = row
                    self.refs[local_topics[tid]].append((stamp, fi, ident))
                    lo = stamp if lo is None else lo
                    hi = stamp
                    n += 1
                self.file_stats.append((n, lo, hi))
                if n:
                    self.start = min(self.start, lo)
                    self.end = max(self.end, hi)
                    self.count += n
        if not self.count:
            raise ValueError('这个 bag 没有消息')
        for refs in self.refs.values():
            refs.sort()
        self.duration = math.ceil((self.end-self.start+1)/1_000_000) / 1000
        self.cameras = sorted(n for n, t in self.topics.items()
                              if t['type'] in ('sensor_msgs/msg/Image', 'sensor_msgs/msg/CompressedImage')
                              and 'depth' not in n.lower())
        # Prefer state topics if present, but recognize all JointState names in other bags.
        joint_topics = sorted(n for n,t in self.topics.items() if t['type']=='sensor_msgs/msg/JointState')
        state_topics = [n for n in joint_topics if 'joint_states' in n]
        self.joints = state_topics or joint_topics
        self.store = make_typestore(definitions, custom_dir or (self.root/'message_types' if (self.root/'message_types').is_dir() else None), distro)
        inspector = TimestampRewriter(self.store, TimeMap(self.start, []))
        self.unknown_types = sorted({t['type'] for t in self.topics.values() if not inspector.supported(t['type'])})
        self.labels = {}
        state_path = self.root/'collection_state.json'
        if state_path.is_file():
            try:
                identities = json.loads(state_path.read_text()).get('device_identities', {})
                for topic in self.cameras:
                    camera = topic.strip('/').split('/')[0]
                    semantic = identities.get(camera+'_semantic')
                    if semantic:
                        self.labels[topic] = {'head':'头部相机','left_wrist':'左腕相机','right_wrist':'右腕相机'}.get(semantic,semantic)
            except (ValueError, OSError):
                pass
        self.check_unchanged()

    def check_unchanged(self):
        if [(f.stat().st_size, f.stat().st_mtime_ns) for f in self.files] != self.signatures:
            raise ValueError('源文件已改变，请停止录制后重新导入')

    def summary(self):
        return {'name': self.name, 'path': self.path, 'duration': self.duration,
                'start_ns': str(self.start), 'count': self.count,
                'size': sum(x.stat().st_size for x in self.files), 'files': len(self.files),
                'cameras': self.cameras, 'joints': self.joints,
                'unknown_types': self.unknown_types,
                'labels': self.labels,
                'topics': [{'name': n, 'type': t['type'], 'count': len(self.refs[n]),
                            'hz': round(len(self.refs[n]) / self.duration, 1)}
                           for n, t in sorted(self.topics.items())]}

    def nearest(self, name, seconds):
        if name not in self.refs or not self.refs[name]:
            raise ValueError('该话题没有消息')
        refs = self.refs[name]
        target = self.start + int(float(seconds)*1e9)
        i = bisect.bisect_left(refs, (target, -1, -1))
        candidates = refs[max(0, i-1):min(len(refs), i+1)]
        stamp, fi, ident = min(candidates, key=lambda r: abs(r[0]-target))
        with connect(self.files[fi]) as db:
            data = db.execute('SELECT data FROM messages WHERE id=?', (ident,)).fetchone()[0]
        return data, (stamp-self.start)/1e9

    def plan(self, ranges, compact=True):
        merged = normalized_ranges(ranges, self.duration, allow_empty=True)
        topics = []
        for name, refs in sorted(self.refs.items()):
            deleted = sum(bisect.bisect_left(refs, (self.start+b, -1, -1)) -
                          bisect.bisect_left(refs, (self.start+a, -1, -1)) for a, b in merged)
            topics.append({'name': name, 'before': len(refs), 'deleted': deleted,
                           'kept': len(refs)-deleted})
        deleted = sum(t['deleted'] for t in topics)
        return {'ranges': [[a/1e9, b/1e9] for a, b in merged],
                'deleted_seconds': sum(b-a for a, b in merged)/1e9,
                'deleted': deleted, 'kept': self.count-deleted, 'topics': topics,
                'compact': compact,
                'unknown_types': self.unknown_types if compact else [],
                'mapping': [{'source_start_ns': str(self.start+a), 'source_end_ns': str(self.start+b),
                             'removed_ns': str(b-a)} for a,b in merged]}

    def export(self, ranges, output: Path, progress=lambda **kw: None, compact=True, annotations=None, detection=None):
        """Keep source read-only; compact record times and typed ROS Time fields."""
        self.check_unchanged()
        plan = self.plan(ranges, compact)
        if not plan['kept']:
            raise ValueError('所选区间会删除全部消息，请至少保留一段数据')
        output = output.resolve()
        if output == self.root or output.is_relative_to(self.root):
            raise ValueError('导出位置不能位于原始 bag 内部')
        if output.exists():
            raise ValueError('导出目录已存在，请使用新的目录名称')
        output.parent.mkdir(parents=True, exist_ok=True)
        # Conservative bound: the original data size plus room for metadata and indices.
        if shutil.disk_usage(output.parent).free < sum(f.stat().st_size for f in self.files) + 32*1024**2:
            raise ValueError('磁盘可用空间不足，请更换导出位置或释放空间')
        partial = output.with_name(output.name + '.partial')
        partial.mkdir()  # Exclusive: never remove someone else's work.
        merged = normalized_ranges(ranges, self.duration, allow_empty=True)
        mapping = TimeMap(self.start, merged)
        rewriter = TimestampRewriter(self.store, mapping)
        unmodified_types = Counter()
        condition = 'COALESCE((SELECT r.end FROM trim_ranges r WHERE r.start <= messages.timestamp ORDER BY r.start DESC LIMIT 1), -9223372036854775808) <= messages.timestamp'
        allcounts = Counter()
        fileinfo = []
        kept_total = 0
        try:
            for fi, source in enumerate(self.files):
                target = partial / f'{output.name}_{fi}.db3'
                with connect(source) as src, sqlite3.connect(target) as dst:
                    dst.execute('PRAGMA journal_mode=DELETE')
                    dst.execute('PRAGMA synchronous=FULL')
                    tables = src.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
                    for name, sql in tables:
                        dst.execute(sql)
                        if name not in ('messages', 'metadata'):
                            quoted = '"' + name.replace('"', '""') + '"'
                            rows = src.execute(f'SELECT * FROM {quoted}')
                            for row in rows:
                                dst.execute(f'INSERT INTO {quoted} VALUES ({",".join("?" for _ in row)})', row)
                    cols = [r[1] for r in src.execute('PRAGMA table_info(messages)')]
                    tid_col, stamp_col, data_col = (cols.index(x) for x in ('topic_id','timestamp','data'))
                    local_types = dict(src.execute('SELECT id,type FROM topics'))
                    placeholders = ','.join('?' for _ in cols)
                    src.execute('CREATE TEMP TABLE trim_ranges(start INTEGER PRIMARY KEY, end INTEGER)')
                    src.executemany('INSERT INTO trim_ranges VALUES (?,?)', [(self.start+a, self.start+b) for a,b in merged])
                    cursor = src.execute('SELECT * FROM messages WHERE ' + condition + ' ORDER BY timestamp,id')
                    count = 0
                    while True:
                        # Keep memory bounded even with large uncompressed image messages.
                        rows = cursor.fetchmany(24)
                        if not rows:
                            break
                        if compact:
                            adjusted=[]
                            for row in rows:
                                values=list(row)
                                values[stamp_col]=mapping(row[stamp_col])
                                msgtype=local_types[row[tid_col]]
                                if rewriter.supported(msgtype):
                                    values[data_col]=rewriter.rewrite(row[data_col],msgtype)
                                else:
                                    unmodified_types[msgtype]+=1
                                adjusted.append(values)
                            rows=adjusted
                        dst.executemany(f'INSERT INTO messages VALUES ({placeholders})', rows)
                        count += len(rows)
                        progress(progress=min(.96, (kept_total+count)/plan['kept']*.96),
                                 message=f'正在写入 {kept_total+count:,} / {plan["kept"]:,} 条保留消息')
                    for name, sql in src.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"):
                        dst.execute(sql)
                    dst.commit()
                    n, lo, hi = dst.execute('SELECT COUNT(*),MIN(timestamp),MAX(timestamp) FROM messages').fetchone()
                    if n:
                        fileinfo.append({'path': target.name, 'starting_time': {'nanoseconds_since_epoch': lo},
                                         'duration': {'nanoseconds': hi-lo}, 'message_count': n})
                        for name, cnt in dst.execute('SELECT t.name,COUNT(*) FROM messages m JOIN topics t ON m.topic_id=t.id GROUP BY t.name'):
                            allcounts[name] += cnt
                    if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError('导出数据库完整性检查失败')
                if not n:
                    target.unlink()
                kept_total += n
            if kept_total != plan['kept']:
                raise ValueError('导出条数与裁剪计划不一致，已取消输出')
            self.check_unchanged()
            meta = copy.deepcopy(self.metadata) if self.metadata else {
                'version': 5, 'storage_identifier': 'sqlite3', 'compression_format': '', 'compression_mode': ''}
            lo = min(x['starting_time']['nanoseconds_since_epoch'] for x in fileinfo)
            hi = max(x['starting_time']['nanoseconds_since_epoch'] + x['duration']['nanoseconds'] for x in fileinfo)
            meta.update(starting_time={'nanoseconds_since_epoch': lo}, duration={'nanoseconds': hi-lo},
                        message_count=kept_total, relative_file_paths=[x['path'] for x in fileinfo], files=fileinfo)
            original_topics = {x['topic_metadata']['name']: x['topic_metadata'] for x in meta.get('topics_with_message_count', [])}
            meta['topics_with_message_count'] = []
            for name, topic in self.topics.items():
                tm = copy.deepcopy(original_topics.get(name, {k: v for k, v in topic.items() if k != 'id'}))
                meta['topics_with_message_count'].append({'topic_metadata': tm, 'message_count': allcounts[name]})
            wrapped = {'rosbag2_bagfile_information': meta}
            (partial / 'metadata.yaml').write_text(yaml.safe_dump(wrapped, sort_keys=False), encoding='utf-8')
            # Keep the database metadata table consistent when the source schema provides it.
            for item in fileinfo:
                with sqlite3.connect(partial / item['path']) as db:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone():
                        db.execute('INSERT INTO metadata(id,metadata_version,metadata) VALUES(1,?,?)',
                                   (meta['version'], yaml.safe_dump(meta, sort_keys=False)))
            report = {'source': self.path, 'output': str(output), 'time_basis': 'bag_record_timestamp',
                      'interval_semantics': '[start,end)', 'source_start_ns': str(self.start),
                      'timestamps': 'compact_shared_time_mapping' if compact else 'preserved',
                      'time_fields_updated': rewriter.changed,
                      'time_fields_clamped_inside_removed_ranges': rewriter.clamped,
                      'unmodified_payload_types': dict(unmodified_types),
                      'payload_policy': 'Only typed builtin_interfaces/Time bytes change; other payload bytes remain identical. Unknown types keep payloads unchanged.',
                      **plan}
            if detection is not None:
                report['auto_idle_detection'] = detection
            (partial / 'trim_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            if annotations:
                annotation_document = {
                    'version': 1,
                    'time_basis': 'bag_record_timestamp',
                    'timestamps': report['timestamps'],
                    'interval_semantics': '[start,end)',
                    'recommended_lerobot_column': 'language_persistent',
                    'coverage': 'gap_free',
                    **annotations,
                }
                (partial / 'subtasks.yaml').write_text(
                    yaml.safe_dump(annotation_document, allow_unicode=True, sort_keys=False), encoding='utf-8')
            state = self.root / 'collection_state.json'
            if state.exists():
                # This sidecar describes the source recording, not the edited bag.
                shutil.copy2(state, partial / 'source_collection_state.json')
            if output.exists():
                raise ValueError('导出目录已被占用，请换一个名称')
            partial.rename(output)
            progress(progress=1, message='裁剪完成，已生成新的 ros2bag', output=str(output), report=report)
            return report
        except Exception:
            shutil.rmtree(partial)
            raise
