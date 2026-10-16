"""LeRobot v2.1 RGB plus streamed lossless dense sensor columns and provenance."""
import hashlib
import json
import math
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from . import lerobot_v21 as base
from convest.sources.robot_rosbag2 import modalities

DATA_PATH=base.DATA_PATH
VIDEO_PATH=base.VIDEO_PATH
write_metadata=base.write_metadata
reindex_segment=base.reindex_segment


def nested_array(values):
    values=np.ascontiguousarray(values)
    array=pa.array(values.ravel())
    for width in reversed(values.shape[1:]):
        array=pa.FixedSizeListArray.from_arrays(array,width)
    return array


def original_times(timeline,time_map):
    result=np.array(timeline,copy=True)
    if time_map:
        for (start,end,removed),boundary in zip(time_map.intervals,time_map.compact_boundaries):
            result += (timeline>=boundary)*removed
    return result


class ScalarStats:
    """Global scalar sensor stats, broadcastable over dense arrays; bounded memory."""
    def __init__(self):
        self.count=0;self.mean=0.;self.m2=0.;self.minimum=math.inf;self.maximum=-math.inf
    def add(self,value):
        a=np.asarray(value,dtype=np.float64)
        if not np.isfinite(a).all():raise ValueError('Nonfinite sensor payload')
        n=a.size;mean=float(a.mean());m2=float(np.square(a-mean).sum())
        delta=mean-self.mean;total=self.count+n
        self.m2 += m2+delta*delta*self.count*n/total
        self.mean += delta*n/total;self.count=total
        self.minimum=min(self.minimum,float(a.min()));self.maximum=max(self.maximum,float(a.max()))
    def result(self):
        return {'min':[self.minimum],'max':[self.maximum],'mean':[self.mean],
                'std':[math.sqrt(max(0,self.m2/self.count))],'count':[self.count]}



class VectorStats:
    """Independent channel statistics: never mix force and torque axes."""
    def __init__(self):
        self.count=0;self.mean=None;self.m2=None;self.minimum=None;self.maximum=None
    def add(self,value):
        a=np.asarray(value,dtype=np.float64)
        if a.ndim!=2 or not np.isfinite(a).all():raise ValueError('Invalid vector sensor')
        n=len(a);mean=a.mean(axis=0);m2=np.square(a-mean).sum(axis=0)
        if not self.count:
            self.mean=mean;self.m2=m2;self.minimum=a.min(axis=0);self.maximum=a.max(axis=0)
        else:
            delta=mean-self.mean;total=self.count+n
            self.m2+=m2+delta*delta*self.count*n/total
            self.mean+=delta*n/total
            self.minimum=np.minimum(self.minimum,a.min(axis=0));self.maximum=np.maximum(self.maximum,a.max(axis=0))
        self.count+=n
    def result(self):
        return {'min':self.minimum.tolist(),'max':self.maximum.tolist(),'mean':self.mean.tolist(),
                'std':np.sqrt(np.maximum(0,self.m2/self.count)).tolist(),'count':[self.count]}


def sensor_accumulators(robot):
    return {k:VectorStats() if s['kind']=='wrench' else ScalarStats() for k,s in modalities(robot).items()}


def stats_description(spec):
    return ('per-axis statistics over aligned frames' if spec['kind']=='wrench' else
            'global scalar over all recorded sample elements; broadcast over shape')

def sensor_metadata(bag,contract):
    result={}
    for key in ('cam0_depth_info','cam0','cam1','cam2'):
        topic=(contract['topics'][key]['topic'] if key=='cam0_depth_info' else
               contract['topics'][key]['topic'].replace('image_raw','camera_info'))
        if topic not in bag.topics:continue
        unique={}
        for t,msg in bag.records(topic):
            value={'height':int(msg.height),'width':int(msg.width),'distortion_model':msg.distortion_model,
                   'D':msg.d.tolist(),'K':msg.k.tolist(),'R':msg.r.tolist(),'P':msg.p.tolist(),
                   'frame_id':msg.header.frame_id}
            fingerprint=hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
            if fingerprint not in unique:unique[fingerprint]={'first_timestamp_ns':int(t),**value}
        result[topic]=list(unique.values())
    return result


def write_segment(root,bag,streams,aligned,bounds,ep_index,global_index,config,contract,segment=None):
    episode=base.write_segment(root,bag,streams,aligned,bounds,ep_index,global_index,config,contract,segment)
    start,end=bounds;length=end-start
    features,stats=episode['features'],episode['stats']
    if config['robot']=='sharpa':
        for d in (features,stats):d.pop('observation.engaged')
    # Preserve actual arm engagement for both robots; Sharpa hands have no status field.
    features['observation.arm_engaged']={'dtype':'float32','shape':[2],'names':['left_arm','right_arm']}
    stats['observation.arm_engaged']=base.array_stats(aligned.engaged[start:end,:2])
    sensor_specs=modalities(config['robot']);acc=sensor_accumulators(config['robot'])
    provenance_names=sorted(aligned.source_indices)
    for key in ('audit.sample_timestamp_ns','audit.receive_timestamp_ns'):
        features[key]={'dtype':'int64','shape':[len(provenance_names)],'names':provenance_names}
    features['audit.original_timestamp_ns']={'dtype':'int64','shape':[1],'names':None}
    arguments={'episode_index':ep_index,'episode_chunk':ep_index//1000}
    relative=DATA_PATH.format(**arguments);file=root/relative;temporary=file.with_suffix('.multimodal.parquet')
    original=pq.read_table(file);writer=None
    try:
        for offset in range(0,length,16):
            stop=min(length,offset+16);a,b=start+offset,start+stop
            table=original.slice(offset,stop-offset)
            if config['robot']=='sharpa':table=table.drop(['observation.engaged'])
            table=table.append_column('observation.arm_engaged',nested_array(aligned.engaged[a:b,:2]))
            for key,spec in sensor_specs.items():
                series=streams[key];indices=aligned.source_indices[key][a:b]
                values=[];last=None;cached=None
                for i in indices:
                    if i<0:raise ValueError(f'Invalid retained modality index: {key}')
                    if i!=last:
                        cached=(np.asarray(series.values[i]) if spec['kind']=='wrench' else
                                bag.raw_image(series.values[i],spec['kind']))
                        last=i
                    values.append(cached)
                values=np.stack(values)
                if str(values.dtype)!=spec['dtype']:raise ValueError(f'Unexpected dtype {key}: {values.dtype}')
                shape=list(values.shape[1:])
                feature={'dtype':spec['dtype'],'shape':shape,'names':(
                    ['force.x','force.y','force.z','torque.x','torque.y','torque.z'] if spec['kind']=='wrench' else None),
                    'sensor':spec,'statistics':stats_description(spec)}
                if key in features and features[key]!=feature:raise ValueError(f'Sensor shape changed: {key}')
                features[key]=feature;acc[key].add(values)
                table=table.append_column(key,nested_array(values))
            timestamps=[];received=[]
            for key in provenance_names:
                series=streams[key];idx=aligned.source_indices[key][a:b]
                timestamps.append(np.where(idx>=0,series.times[np.maximum(idx,0)],0))
                received.append(np.where(idx>=0,series.receive_times[np.maximum(idx,0)],0))
            table=table.append_column('audit.sample_timestamp_ns',nested_array(np.stack(timestamps,axis=1)))
            table=table.append_column('audit.receive_timestamp_ns',nested_array(np.stack(received,axis=1)))
            table=table.append_column('audit.original_timestamp_ns',pa.array(original_times(aligned.timeline[a:b],bag.compact_time_map)))
            if writer is None:writer=pq.ParquetWriter(temporary,table.schema,compression='zstd',compression_level=3)
            writer.write_table(table)
    finally:
        if writer:writer.close()
    temporary.replace(file)
    for key,accumulator in acc.items():stats[key]=accumulator.result()
    # Source timestamps intentionally have no normalization stats.
    episode['camera_calibration']=sensor_metadata(bag,contract)
    episode['provenance_note']='sample/receive times are compact; zero marks unavailable historic command when inactive. original time chooses post-cut branch at each seam.'
    return episode
