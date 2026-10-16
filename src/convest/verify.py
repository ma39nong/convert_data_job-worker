"""Read final files in bounded batches; optionally compare every sensor/lowdim row to bag."""
from contextlib import ExitStack
from pathlib import Path
import json
import av
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from convest.config import atomic_json
from convest.formats.multimodal import sensor_accumulators, original_times, VectorStats, ScalarStats
from convest.pipeline import load_records
from convest.sources.robot_rosbag2 import Bag, modalities
from convest.sources.gello_rosbag2 import snapshot, discover
from convest.audit import audit_recording, require


def values(column,shape):
    if isinstance(column,pa.ChunkedArray):column=column.combine_chunks()
    while pa.types.is_fixed_size_list(column.type):column=column.flatten()
    return column.to_numpy(zero_copy_only=False).reshape((-1,*shape))



class OffsetScalarStats(ScalarStats):
    """Center integer timestamps before float statistics to avoid epoch cancellation."""
    def __init__(self):
        super().__init__()
        self.origin=None
    def add(self,value):
        if self.origin is None:self.origin=int(np.asarray(value).flat[0])
        super().add(np.asarray(value)-self.origin)
    def result(self):
        result=super().result()
        for key in ('min','max','mean'):result[key][0]+=self.origin
        return result


def safe_artifact(root,relative):
    path=(root/relative).resolve()
    require(path.is_relative_to(root.resolve()),'Artifact path escapes dataset root')
    return path


def verify_episode(root, info, episode, episode_stats, record, config, contract, total, compare_source, full_video):
    index = episode['episode_index']
    length = episode['length']
    source = Path(record['source'])
    stats = {index: episode_stats}
    sensor_count = 0
    args = {'episode_index': index, 'episode_chunk': index // 1000}
    path = safe_artifact(root, info['data_path'].format(**args))
    if compare_source:
        require(snapshot(source) == record['source_snapshot'], 'Source changed since conversion')
    with ExitStack() as stack:
        bag = aligned = streams = None
        if compare_source:
            bag = stack.enter_context(Bag(source, config['schema_dir']))
            item = discover(source)[0]
            streams, aligned = audit_recording(bag, contract, config, item)
            require(aligned.continuous, 'Source fails independent continuity audit')
            require(episode['source_start_ns'] == int(aligned.timeline[aligned.bounds[0]]) and episode['source_end_ns'] == int(aligned.timeline[aligned.bounds[1] - 1]), 'Exported bounds differ from independent source audit')
        accumulated = sensor_accumulators(config['robot'])
        vector_stats = {}
        offset = 0
        parquet = stack.enter_context(pq.ParquetFile(path))
        require(parquet.metadata.num_rows == length, 'Verification check failed: parquet.metadata.num_rows == length')
        checked_shapes = set()
        for batch in parquet.iter_batches(batch_size=8):
            table = pa.Table.from_batches([batch])
            count = len(table)
            for key, expected in [('frame_index', np.arange(offset, offset + count)), ('index', np.arange(total + offset, total + offset + count)), ('episode_index', np.full(count, index)), ('task_index', np.full(count, episode['task_index']))]:
                np.testing.assert_array_equal(table[key].to_numpy(), expected)
            timeline = table['source_timestamp_ns'].to_numpy()
            origin = record['alignment']['source_start_ns']
            first_grid = ((episode['source_start_ns'] - origin) * info['fps'] + 500000000) // 1000000000
            np.testing.assert_array_equal(timeline, origin + (first_grid + np.arange(offset, offset + count)) * 1000000000 // info['fps'])
            np.testing.assert_allclose(table['timestamp'].to_numpy(), np.arange(offset, offset + count) / info['fps'], atol=1e-05)
            begin = None
            if compare_source:
                begin = int(np.searchsorted(aligned.timeline, timeline[0]))
                np.testing.assert_array_equal(timeline, aligned.timeline[begin:begin + count])
                require(aligned.valid_mask[begin:begin + count].all(), 'Verification check failed: aligned.valid_mask[begin:begin + count].all()')
                np.testing.assert_array_equal(table['audit.original_timestamp_ns'].to_numpy(), original_times(timeline, bag.compact_time_map))
            require(set(table.column_names)=={k for k,v in info['features'].items() if v['dtype']!='video'}, 'Parquet columns differ from feature metadata')
            sample_times=values(table['audit.sample_timestamp_ns'],info['features']['audit.sample_timestamp_ns']['shape'])
            require(((sample_times==0)|(sample_times<=timeline[:,None])).all(), 'Audit contains a future sample')
            require((table['audit.original_timestamp_ns'].to_numpy()>=timeline).all(), 'Original grid predates compact grid')
            for key, feature in info['features'].items():
                if feature['dtype'] == 'video':
                    continue
                require(key in table.column_names, 'Verification check failed: key in table.column_names')
                dtype = table[key].type
                shape = []
                while pa.types.is_fixed_size_list(dtype):
                    shape.append(dtype.list_size)
                    dtype = dtype.value_type
                actual = values(table[key], shape)
                require(actual.shape == (count, *shape), f'Incorrect feature shape: {key}')
                require(shape == feature['shape'] or (shape == [] and feature['shape'] == [1]), f'Incorrect metadata shape: {key}')
                require(str(actual.dtype) == feature['dtype'], (key, actual.dtype, feature['dtype']))
                require(np.isfinite(actual).all(), 'Verification check failed: np.isfinite(actual).all()')
                checked_shapes.add(key)
                if key in episode_stats['stats'] and key not in accumulated:
                    if key not in vector_stats:
                        vector_stats[key] = VectorStats() if actual.ndim == 2 else OffsetScalarStats() if np.issubdtype(actual.dtype,np.integer) else ScalarStats()
                    vector_stats[key].add(actual)
                if key in accumulated:
                    accumulated[key].add(actual)
                if compare_source:
                    if key in ('observation.state', 'action', 'observation.engaged', 'observation.arm_engaged'):
                        expected = {'observation.state': aligned.state, 'action': aligned.action, 'observation.engaged': aligned.engaged, 'observation.arm_engaged': aligned.engaged[:, :2]}[key][begin:begin + count]
                        np.testing.assert_array_equal(actual, expected)
                    elif key in modalities(config['robot']):
                        spec = modalities(config['robot'])[key]
                        s = streams[key]
                        chosen = s.indices(timeline)
                        last = None
                        cached = None
                        for j, i in enumerate(chosen):
                            limit = config['max_staleness_ms'] * 1000000.0
                            if key == 'observation.depths.cam0' and any((g['bounds'][0] <= begin + j < g['bounds'][1] for g in aligned.report['depth_splice_tolerance']['gaps'])):
                                limit = config['camera_short_gap_max_age_ms'] * 1000000.0
                            require(i >= 0 and 0 <= int(timeline[j] - s.times[i]) <= limit, 'Verification check failed: i >= 0 and 0 <= int(timeline[j] - s.times[i]) <= limit')
                            if i != last:
                                cached = s.values[i] if spec['kind'] == 'wrench' else bag.raw_image(s.values[i], spec['kind'])
                                last = i
                            np.testing.assert_array_equal(actual[j], cached)
                            sensor_count += 1
                    elif key in ('audit.sample_timestamp_ns', 'audit.receive_timestamp_ns'):
                        for col, name in enumerate(feature['names']):
                            s = streams[name]
                            idx = aligned.source_indices[name][begin:begin + count]
                            selected = s.times if key == 'audit.sample_timestamp_ns' else s.receive_times
                            expected = np.where(idx >= 0, selected[np.maximum(idx, 0)], 0)
                            np.testing.assert_array_equal(actual[:, col], expected)
            offset += count
        require(offset == length, 'Verification check failed: offset == length')
        for key, acc in {**accumulated, **vector_stats}.items():
            actual = acc.result()
            expected = stats[index]['stats'][key]
            for field in ('min', 'max', 'mean', 'std', 'count'):
                np.testing.assert_allclose(actual[field], expected[field], rtol=1e-07, atol=1e-07)
        for key, feature in info['features'].items():
            if feature['dtype'] != 'video':
                continue
            with av.open(str(safe_artifact(root, info['video_path'].format(**args, video_key=key)))) as container:
                stream = container.streams.video[0]
                require(stream.frames == length and float(stream.average_rate) == info['fps'], "Verification check failed: stream.frames == length and float(stream.average_rate) == info['fps']")
                require([stream.height, stream.width, 3] == feature['shape'], "Verification check failed: [stream.height, stream.width, 3] == feature['shape']")
                count = 0
                for frame in container.decode(stream):
                    require(abs(float(frame.pts * frame.time_base) - count / info['fps']) < 1e-06, "Verification check failed: abs(float(frame.pts * frame.time_base) - count / info['fps']) < 1e-06")
                    count += 1
                    if not full_video:
                        break
                require(count == (length if full_video else 1), 'Verification check failed: count == (length if full_video else 1)')
    if compare_source:
        require(snapshot(source) == record['source_snapshot'], 'Source changed during verification')
    return {'status': 'passed', 'episode_index': index, 'source': str(source), 'frames': length, 'lossless_sensor_rows_compared': sensor_count, 'independent_alignment': compare_source, 'depth_splice_tolerance': aligned.report['depth_splice_tolerance'] if compare_source else None}


def verify(root, full_video=True, compare_source=False, raise_on_error=True):
    """Verify committed episodes independently; source access is strictly opt-in."""
    import fcntl
    import time
    root = Path(root).resolve()
    # Verification and conversion share the same lock, including report writes.
    with (root/'conversion/lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started=time.time_ns()
        results = []
        atomic_json(root/'conversion/verification.json',{'status':'running','source_comparison':compare_source,'started_ns':started,'results':[]})
        try:
            config = json.loads((root/'conversion/manifest.json').read_text())['config']
            info = json.loads((root/'meta/info.json').read_text())
            require(info['codebase_version']=='v2.1', 'Not a LeRobot v2.1 dataset')
            def rows(name):
                return [json.loads(s) for s in (root/'meta'/name).read_text().splitlines()]
            episodes, statistics, tasks = rows('episodes.jsonl'), rows('episodes_stats.jsonl'), rows('tasks.jsonl')
            require(len(episodes)==len(statistics)==info['total_episodes']>0, 'Episode metadata counts differ')
            require([e['episode_index'] for e in episodes]==list(range(len(episodes))), 'Episode ids are not contiguous')
            require([e['episode_index'] for e in statistics]==list(range(len(episodes))), 'Statistics ids differ')
            require(len(tasks)==info['total_tasks'] and [t['task_index'] for t in tasks]==list(range(len(tasks))), 'Task metadata differs')
            require(sum(e['length'] for e in episodes)==info['total_frames'], 'Total frame metadata differs')
            require(info['fps']==30 and info['chunks_size']==1000, 'Unsupported grid/chunk metadata')
            dimensions=(108,54) if config['robot']=='wuji' else (116,58)
            require([info['features'][k]['shape'][0] for k in ('observation.state','action')]==list(dimensions), 'Robot feature dimensions differ')
            expected_videos={f'observation.images.cam{i}' for i in range(3)}
            require({k for k,v in info['features'].items() if v['dtype']=='video'}==expected_videos, 'Three RGB video features are required')
            require(info['total_videos']==3*len(episodes), 'Video metadata count differs')
            require(info['total_chunks']==(len(episodes)+999)//1000, 'Chunk count differs')
            require(info['robot_type']==f"dual_fr3_{config['robot']}", 'Robot type differs')
            require(('observation.engaged' in info['features'])==(config['robot']=='wuji'), 'Engagement schema differs')
            require(all(k in info['features'] for k in modalities(config['robot'])), 'Required sensor feature missing')
            for key in ('timestamp','source_timestamp_ns','frame_index','index','episode_index','task_index',
                        'observation.arm_engaged','audit.original_timestamp_ns','audit.sample_timestamp_ns','audit.receive_timestamp_ns'):
                require(key in info['features'],f'Required feature missing: {key}')
            records=load_records(root)
            source_map={}
            for record in records:
                for ep in record['episodes']:
                    require(ep['episode_index'] not in source_map, 'Duplicate committed episode')
                    source_map[ep['episode_index']]=record
            require(set(source_map)==set(range(len(episodes))), 'Commit journal and metadata differ')
            # Structural verification must not depend on original config/contract paths.
            contract=yaml.safe_load(Path(config['contract']).read_text()) if compare_source else None
            total=0
            for episode,episode_stats in zip(episodes,statistics):
                index=episode['episode_index'];record=source_map[index]
                try:
                    committed=next(e for e in record['episodes'] if e['episode_index']==index)
                    require(committed['features']==info['features'], 'Feature metadata differs from committed journal')
                    require(committed['length']==episode['length'], 'Episode length differs from committed journal')
                    require(episode['length']>=2, 'Episode is too short')
                    ti=episode.get('task_index',0)
                    require(0<=ti<len(tasks) and episode['tasks']==[tasks[ti]['task']], 'Episode task differs')
                    outcome=verify_episode(root,info,episode,episode_stats,record,config,contract,total,compare_source,full_video)
                except Exception as exc:
                    outcome={'status':'failed','episode_index':index,'source':record['source'],
                             'frames':episode['length'],'error':f'{type(exc).__name__}: {exc}'}
                results.append(outcome)
                atomic_json(root/'conversion/verification.json',{'status':'running','source_comparison':compare_source,'started_ns':started,'results':results})
                atomic_json(root/f'conversion/verification/episode_{index:06d}.json',outcome)
                print(f"episode {index}: verification {outcome['status']}",flush=True)
                total+=episode['length']
            failed=[r for r in results if r['status']!='passed']
            report={'status':'failed' if failed else 'passed','episodes':len(episodes),'frames':total,
                    'passed_episodes':len(results)-len(failed),'failed_episodes':len(failed),
                    'full_video_decode':full_video,'source_comparison':compare_source,
                    'independent_alignment':compare_source,
                    'lossless_sensor_rows_compared':sum(r.get('lossless_sensor_rows_compared',0) for r in results),
                    'results':results}
        except Exception as exc:
            report={'status':'failed','source_comparison':compare_source,'results':results,
                    'global_error':f'{type(exc).__name__}: {exc}'}
        report.update(verification_version='independent-v1',started_ns=started,finished_ns=time.time_ns())
        atomic_json(root/'conversion/verification.json',report)
        from .reporting import quality_report
        try:
            quality_report(root,report)
        except Exception as exc:
            atomic_json(root/'conversion/quality_report.json',{'status':'unavailable','error':f'{type(exc).__name__}: {exc}'})
            report['quality_report_error']=f'{type(exc).__name__}: {exc}'
            report['status']='failed'
            atomic_json(root/'conversion/verification.json',report)
        if report['status']!='passed' and raise_on_error:
            raise ValueError('Dataset verification failed; see conversion/verification.json')
        return report
