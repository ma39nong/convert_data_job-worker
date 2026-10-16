"""Run with an existing LeRobot v2.1 interpreter; no training or environment changes."""
from pathlib import Path
import os
WORKSPACE=Path(__file__).resolve().parents[1]
for name,relative in [('HF_HOME','.cache/huggingface'),('HF_DATASETS_CACHE','.cache/huggingface/datasets'),('XDG_CACHE_HOME','.cache')]:
    os.environ[name]=str(WORKSPACE/relative)
os.environ['HF_HUB_OFFLINE']='1'
import argparse
import json
import numpy as np
import pyarrow.parquet as pq
from lerobot.common.datasets.lerobot_dataset import LeRobotDataset


def main():
    p=argparse.ArgumentParser();p.add_argument('root',type=Path);args=p.parse_args()
    root=args.root.resolve();info=json.loads((root/'meta/info.json').read_text())
    cfg=json.loads((root/'conversion/manifest.json').read_text())['config']
    ds=LeRobotDataset(cfg['repo_id'],root=root,video_backend='pyav',
                     delta_timestamps={'action':[i/info['fps'] for i in range(50)]})
    checked=[]
    for ep in range(ds.meta.total_episodes):
        start=int(ds.episode_data_index['from'][ep]);end=int(ds.episode_data_index['to'][ep])
        path=root/info['data_path'].format(episode_index=ep,episode_chunk=ep//1000)
        native=pq.ParquetFile(path)
        sensor_keys=[k for k in info['features'] if k.startswith(('observation.depths.','observation.tactile.'))]
        for idx in sorted({start,(start+end)//2,end-1}):
            local=idx-start;group=0;group_offset=0
            while group_offset+native.metadata.row_group(group).num_rows<=local:
                group_offset+=native.metadata.row_group(group).num_rows;group+=1
            native_row=native.read_row_group(group,columns=sensor_keys).slice(local-group_offset,1)
            row=ds[idx];raw=ds.hf_dataset[idx]
            assert row['action'].shape==(50,info['features']['action']['shape'][0])
            for name,feature in info['features'].items():
                if feature['dtype']=='video':
                    assert list(row[name].shape)==[3,feature['shape'][0],feature['shape'][1]]
                elif name.startswith(('observation.depths.','observation.tactile.')):
                    assert list(row[name].shape)==feature['shape']
                    value=row[name].numpy()
                    # Native parquet numeric values survive the standard HF/Torch transform.
                    column=native_row[name].combine_chunks()
                    import pyarrow as pa
                    while pa.types.is_fixed_size_list(column.type):column=column.flatten()
                    expected=column.to_numpy().reshape(feature['shape'])
                    np.testing.assert_array_equal(value,expected)
            if idx==end-1:
                assert row['action_is_pad'][1:].all()
                expected=raw['action'].numpy()
                np.testing.assert_array_equal(row['action'].numpy(),np.repeat(expected[None],50,axis=0))
            checked.append(idx)
    result={'status':'passed','loader':'installed LeRobot v2.1','interpreter':os.sys.executable,
            'video_backend':'pyav','episodes':ds.meta.total_episodes,'frames':len(ds),'sampled_indices':checked,
            'all_sensor_features_readable':True,'action_horizon':50,'tail_padding_checked':True,
            'note':'Standard loader verified; no training or depth/tactile model integration performed.'}
    (root/'conversion/loader_verification.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result))

if __name__=='__main__':main()
