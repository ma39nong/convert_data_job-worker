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
from convest.formats.multimodal import sensor_accumulators, original_times
from convest.pipeline import load_records
from convest.sources.robot_rosbag2 import Bag, modalities
from convest.sources.gello_rosbag2 import snapshot, discover
from convest.recipes.multimodal import prepare


def values(column,shape):
    if isinstance(column,pa.ChunkedArray):column=column.combine_chunks()
    while pa.types.is_fixed_size_list(column.type):column=column.flatten()
    return column.to_numpy(zero_copy_only=False).reshape((-1,*shape))


def verify(root,full_video=True,compare_source=False):
    root=Path(root);manifest=json.loads((root/'conversion/manifest.json').read_text());config=manifest['config']
    info=json.loads((root/'meta/info.json').read_text())
    if info['codebase_version']!='v2.1':raise ValueError('Not v2.1')
    def rows(path):return [json.loads(x) for x in (root/path).read_text().splitlines()]
    episodes=rows('meta/episodes.jsonl');stats=rows('meta/episodes_stats.jsonl');tasks=rows('meta/tasks.jsonl')
    assert len(episodes)==len(stats)==info['total_episodes']>0
    assert len(tasks)==info['total_tasks']
    records=load_records(root);source_map={ep['episode_index']:r for r in records for ep in r['episodes']}
    dimensions=(108,54) if config['robot']=='wuji' else (116,58)
    assert [info['features'][k]['shape'][0] for k in ('observation.state','action')]==list(dimensions)
    assert ('observation.engaged' in info['features'])==(config['robot']=='wuji')
    contract=yaml.safe_load(Path(config['contract']).read_text())
    total=0;sensor_count=0;results=[]
    for index,episode in enumerate(episodes):
        assert episode['episode_index']==stats[index]['episode_index']==index
        record=source_map[index];source=Path(record['source']);length=episode['length']
        assert snapshot(source)==record['source_snapshot']
        args={'episode_index':index,'episode_chunk':index//1000}
        path=root/info['data_path'].format(**args)
        with ExitStack() as stack:
            bag=aligned=streams=None
            if compare_source:
                bag=stack.enter_context(Bag(source,config['schema_dir']))
                item=discover(source)[0]
                from convest.sources import robot_rosbag2 as adapter
                streams,aligned=prepare(adapter,bag,item,config,contract)
            accumulated=sensor_accumulators(config['robot']);offset=0
            parquet=stack.enter_context(pq.ParquetFile(path))
            assert parquet.metadata.num_rows==length
            checked_shapes=set()
            for batch in parquet.iter_batches(batch_size=8):
                table=pa.Table.from_batches([batch]);count=len(table)
                for key,expected in [('frame_index',np.arange(offset,offset+count)),
                                     ('index',np.arange(total+offset,total+offset+count)),
                                     ('episode_index',np.full(count,index)),
                                     ('task_index',np.full(count,episode['task_index']))]:
                    np.testing.assert_array_equal(table[key].to_numpy(),expected)
                timeline=table['source_timestamp_ns'].to_numpy()
                origin=record['alignment']['source_start_ns']
                first_grid=((episode['source_start_ns']-origin)*info['fps']+500_000_000)//1_000_000_000
                np.testing.assert_array_equal(timeline,origin+(first_grid+np.arange(offset,offset+count))*1_000_000_000//info['fps'])
                np.testing.assert_allclose(table['timestamp'].to_numpy(),np.arange(offset,offset+count)/info['fps'],atol=1e-5)
                begin=None
                if compare_source:
                    begin=int(np.searchsorted(aligned.timeline,timeline[0]))
                    np.testing.assert_array_equal(timeline,aligned.timeline[begin:begin+count])
                    assert aligned.valid_mask[begin:begin+count].all()
                    np.testing.assert_array_equal(table['audit.original_timestamp_ns'].to_numpy(),original_times(timeline,bag.compact_time_map))
                for key,feature in info['features'].items():
                    if feature['dtype']=='video':continue
                    assert key in table.column_names
                    shape=feature['shape'] if pa.types.is_fixed_size_list(table[key].type) else []
                    actual=values(table[key],shape)
                    assert str(actual.dtype)==feature['dtype'],(key,actual.dtype,feature['dtype'])
                    assert np.isfinite(actual).all()
                    checked_shapes.add(key)
                    if key in accumulated:accumulated[key].add(actual)
                    if compare_source:
                        if key in ('observation.state','action','observation.engaged','observation.arm_engaged'):
                            expected={'observation.state':aligned.state,'action':aligned.action,
                                      'observation.engaged':aligned.engaged,'observation.arm_engaged':aligned.engaged[:,:2]}[key][begin:begin+count]
                            np.testing.assert_array_equal(actual,expected)
                        elif key in modalities(config['robot']):
                            spec=modalities(config['robot'])[key];s=streams[key]
                            chosen=s.indices(timeline);last=None;cached=None
                            for j,i in enumerate(chosen):
                                limit=config['max_staleness_ms']*1e6
                                if key=='observation.depths.cam0' and any(g['bounds'][0]<=begin+j<g['bounds'][1] for g in aligned.report['depth_splice_tolerance']['gaps']):
                                    limit=config['camera_short_gap_max_age_ms']*1e6
                                assert i>=0 and 0<=int(timeline[j]-s.times[i])<=limit
                                if i!=last:
                                    cached=s.values[i] if spec['kind']=='wrench' else bag.raw_image(s.values[i],spec['kind']);last=i
                                np.testing.assert_array_equal(actual[j],cached);sensor_count+=1
                        elif key in ('audit.sample_timestamp_ns','audit.receive_timestamp_ns'):
                            for col,name in enumerate(feature['names']):
                                s=streams[name];idx=aligned.source_indices[name][begin:begin+count]
                                selected=s.times if key=='audit.sample_timestamp_ns' else s.receive_times
                                expected=np.where(idx>=0,selected[np.maximum(idx,0)],0)
                                np.testing.assert_array_equal(actual[:,col],expected)
                offset+=count
            assert offset==length
            for key,acc in accumulated.items():
                actual=acc.result();expected=stats[index]['stats'][key]
                for field in ('min','max','mean','std','count'):
                    np.testing.assert_allclose(actual[field],expected[field],rtol=1e-9,atol=1e-9)
            for key,feature in info['features'].items():
                if feature['dtype']!='video':continue
                with av.open(str(root/info['video_path'].format(**args,video_key=key))) as container:
                    stream=container.streams.video[0]
                    assert stream.frames==length and float(stream.average_rate)==info['fps']
                    assert [stream.height,stream.width,3]==feature['shape']
                    count=0
                    for frame in container.decode(stream):
                        assert abs(float(frame.pts*frame.time_base)-count/info['fps'])<1e-6
                        count+=1
                        if not full_video:break
                    assert count==(length if full_video else 1)
        assert snapshot(source)==record['source_snapshot']
        total+=length
        results.append({'episode_index':index,'source':str(source),'frames':length,
                        'splice_audit':aligned.report.get('splice_audit') if compare_source else None,
                        'depth_splice_tolerance':aligned.report.get('depth_splice_tolerance') if compare_source else None})
        print(f'verified episode {index}: {length} frames, source_compare={compare_source}',flush=True)
    assert total==info['total_frames']
    result={'status':'passed','episodes':len(episodes),'frames':total,'full_video_decode':full_video,
            'source_comparison':compare_source,'lossless_sensor_rows_compared':sensor_count,'results':results}
    atomic_json(root/'conversion/verification.json',result)
    return result
