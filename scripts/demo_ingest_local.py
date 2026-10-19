"""Build synthetic bags and exercise the real CLI, entirely below local outputs/."""
import json
import shutil
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
        'retry_seconds':1,'max_attempts':3,'initial_history':{'mode':'none'},'routes':routes}
    cfg=root/'service.yaml';cfg.write_text(yaml.safe_dump(config))
    originals={};pending=[]
    for route in routes:
        bag=data/'origin-data'/route['robot']/route['group']/'episode1'
        bag.parent.mkdir(parents=True)
        staging=root/'fixtures'/f"{route['robot']}-{route['group']}-episode1"
        make_conversion_bag(staging,route['robot'],frames=18);pending.append((staging,bag))
        originals[str(bag)]={p.name:checksum(p) for p in staging.iterdir()}
    command=[sys.executable,'-m','convest.ingest.cli','once','--config',str(cfg)]
    def once():
        subprocess.run(command,cwd=WORKSPACE,check=True)
    def counts():return json.loads((root/'state/status.json').read_text())['counts']
    once()  # Establish an empty input baseline; fixtures outside inputs stay invisible.
    if counts():raise ValueError('Discovered data outside configured inputs')
    for staging,bag in pending:
        bag.mkdir()
        for name in ('metadata.yaml','collection_state.json'):shutil.copyfile(staging/name,bag/name)
    once();time.sleep(1.05);once()
    if counts()!={'waiting_upload':4}:raise ValueError('Incomplete input passed readiness checks')
    for staging,bag in pending:
        for src in staging.iterdir():shutil.copyfile(src,bag/src.name)
    once();time.sleep(1.05);once()
    # Append the last episode directly, with no marker, callback or next episode.
    staging=root/'fixtures/wuji-A-episode2';second=data/'origin-data/wuji/A/episode2'
    make_conversion_bag(staging,'wuji',frames=18)
    originals[str(second)]={p.name:checksum(p) for p in staging.iterdir()};shutil.copytree(staging,second)
    once();time.sleep(1.05);once();once()  # Last scan proves idempotence.
    outputs=[]
    for route in routes:
        output=data/'clean-data-lerobot'/route['robot']/route['group'];fixed=output.resolve(strict=True)
        result=verify(fixed);wanted=2 if route=={'robot':'wuji','group':'A'} else 1
        if result['episodes']!=wanted:raise ValueError('Unexpected episode count')
        outputs.append({'route':route,'output':str(output),'fixed_generation':str(fixed),'episodes':result['episodes'],'frames':result['frames'],'verification':result['status']})
    if any(files!={p.name:checksum(p) for p in Path(bag).iterdir()} for bag,files in originals.items()):raise ValueError('Input modified')
    summary={'status':'passed','local_only':True,'server_contacted':False,'upload_marker_required':False,
        'uploader_protocol_required':False,'arrival_model':'partial directories, then files complete','incomplete_inputs_blocked':True,
        'processed_sources':5,'route_datasets':4,'outputs':outputs,'originals_unchanged':True,
        'work_cache_remaining':[p.name for p in (root/'work').iterdir() if p.is_dir()],
        'stability_note':'Demo uses 2 scans/1 second for synthetic files; production templates remain 3 scans/60 seconds.'}
    atomic_json(root/'validation.json',summary)
    print(json.dumps({'validation':str(root/'validation.json'),**summary},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
