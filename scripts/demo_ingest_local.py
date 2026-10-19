"""Build synthetic bags and exercise the real CLI, entirely below local outputs/."""
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
import yaml

WORKSPACE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(WORKSPACE/'tests'))
from conversion_helpers import make_conversion_bag
from convest.jobs import checksum
from convest.verify import verify
from convest.config import atomic_json


def main():
    root=WORKSPACE/'outputs'/('ingest-demo-'+uuid.uuid4().hex[:12]);root.mkdir(parents=True)
    data=root/'data';routes=[{'robot':r,'group':g} for r in ('sharpa','wuji') for g in ('A','B')]
    config={'schema_version':1,'data_root':str(data),'state_root':str(root/'state'),'work_root':str(root/'work'),
        'poll_seconds':1,'stable_scans':2,'stable_seconds':1,'max_checks_per_scan':4,'max_jobs_per_scan':4,
        'retry_seconds':1,'max_attempts':3,'routes':routes}
    cfg=root/'service.yaml';cfg.write_text(yaml.safe_dump(config))
    originals={}
    for route in routes:
        bag=data/'origin-data'/route['robot']/route['group']/'episode1'
        make_conversion_bag(bag,route['robot'],frames=18)
        originals[str(bag)]={p.name:checksum(p) for p in bag.iterdir()}
    command=[sys.executable,'-m','convest.ingest.cli','once','--config',str(cfg)]
    def once():
        subprocess.run(command,cwd=WORKSPACE,check=True)
    once();time.sleep(1.05);once()
    # Append a second episode to one route only, without any upload marker.
    make_conversion_bag(data/'origin-data/wuji/A/episode2','wuji',frames=18)
    once();time.sleep(1.05);once();once()  # Last scan proves idempotence.
    outputs=[]
    for route in routes:
        output=data/'clean-data-lerobot'/route['robot']/route['group'];fixed=output.resolve(strict=True)
        result=verify(fixed);wanted=2 if route=={'robot':'wuji','group':'A'} else 1
        if result['episodes']!=wanted:raise ValueError('Unexpected episode count')
        outputs.append({'route':route,'output':str(output),'fixed_generation':str(fixed),'episodes':result['episodes'],'frames':result['frames'],'verification':result['status']})
    if any(files!={p.name:checksum(p) for p in Path(bag).iterdir()} for bag,files in originals.items()):raise ValueError('Input modified')
    summary={'status':'passed','local_only':True,'server_contacted':False,'upload_marker_required':False,
        'processed_sources':5,'route_datasets':4,'outputs':outputs,'originals_unchanged':True,
        'work_cache_remaining':[p.name for p in (root/'work').iterdir() if p.is_dir()],
        'stability_note':'Demo uses 2 scans/1 second for synthetic files; production templates remain 3 scans/60 seconds.'}
    atomic_json(root/'validation.json',summary)
    print(json.dumps({'validation':str(root/'validation.json'),**summary},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
