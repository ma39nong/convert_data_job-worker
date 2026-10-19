"""Measure RGB source fidelity at episode boundaries, middles and cleaner seams.

This is a supplementary sampled content audit, not a new quality cutoff. The
existing verifier independently checks every video frame's count and timestamps.
"""
import argparse
import json
from pathlib import Path
import av
import numpy as np
import pyarrow.parquet as pq
import yaml
from convest.audit import audit_recording
from convest.pipeline import load_records
from convest.sources.robot_rosbag2 import Bag
from convest.sources.gello_rosbag2 import discover


def check(root):
    cfg=json.loads((root/'conversion/manifest.json').read_text())['config']
    contract=yaml.safe_load(Path(cfg['contract']).read_text())
    info=json.loads((root/'meta/info.json').read_text());samples=[]
    for record in load_records(root):
        with Bag(Path(record['source']),cfg['schema_dir']) as bag:
            streams,aligned=audit_recording(bag,contract,cfg,discover(Path(record['source']))[0])
            for ep in record['episodes']:
                args={'episode_chunk':ep['episode_index']//1000,'episode_index':ep['episode_index']}
                timeline=pq.read_table(root/info['data_path'].format(**args),columns=['source_timestamp_ns'])['source_timestamp_ns'].to_numpy()
                first=int(np.searchsorted(aligned.timeline,timeline[0]));length=len(timeline)
                np.testing.assert_array_equal(timeline,aligned.timeline[first:first+length])
                selected={0,length//2,length-1}
                if bag.compact_time_map:
                    for boundary in bag.compact_time_map.compact_boundaries:
                        j=int(np.searchsorted(timeline,boundary))
                        selected.update(i for i in (j-1,j,j+1) if 0<=i<length)
                for camera in ('cam0','cam1','cam2'):
                    key='observation.images.'+camera
                    with av.open(str(root/info['video_path'].format(**args,video_key=key))) as video:
                        for i,frame in enumerate(video.decode(video.streams.video[0])):
                            if i not in selected:continue
                            source_index=aligned.source_indices[camera][first+i]
                            expected=bag.image(streams[camera].values[source_index])
                            actual=frame.to_ndarray(format='rgb24')
                            if expected.shape!=actual.shape:raise ValueError('RGB source shape differs')
                            delta=actual.astype(np.float32)-expected.astype(np.float32)
                            mse=float(np.mean(delta*delta,dtype=np.float64))
                            samples.append({'episode_index':ep['episode_index'],'camera':camera,'frame_index':i,
                                'mae':float(np.abs(delta).mean(dtype=np.float64)),
                                'psnr_db':10*float(np.log10(255**2/mse)) if mse else None})
    if not samples:raise ValueError('No RGB samples compared')
    return {'status':'completed','dataset':str(root),'scope':'sampled RGB content; all-frame structure checked separately',
            'encoding':'lossy H.264; pixel equality is not expected','quality_cutoffs_changed':False,
            'samples_compared':len(samples),'max_mae':max(s['mae'] for s in samples),
            'min_psnr_db':min((s['psnr_db'] for s in samples if s['psnr_db'] is not None),default=None),'samples':samples}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dataset',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.output.exists():raise FileExistsError('Choose a new report path')
    result=check(args.dataset.resolve(strict=True));args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open('x') as stream:json.dump(result,stream,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='samples'}))


if __name__=='__main__':main()
