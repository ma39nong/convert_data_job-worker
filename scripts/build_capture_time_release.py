"""Build a pinned source-only native-server upgrade and selective backfill entry."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import subprocess

PROJECT='/data/data-fruits/real/harvest-tomato/code/auto-clean-convert'


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    root=Path(__file__).resolve().parents[1]
    if subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=root,text=True).strip():
        raise ValueError('Commit tracked source changes before building a pinned release')
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    subprocess.run(['git','archive','--format=tar.gz','-o',str(out/'source.tar.gz'),commit],cwd=root,check=True)
    sha=hashlib.sha256((out/'source.tar.gz').read_bytes()).hexdigest()
    shutil.copyfile(root/'deploy/upgrade_server.sh',out/'upgrade_server.sh')
    body='''#!/usr/bin/env bash
set -euo pipefail
release_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
mode="${1:---prepare}"
[[ "$mode" == --prepare || "$mode" == --apply ]] || { echo 'Use --prepare or --apply'; exit 2; }
cd "$release_dir"
sha256sum -c SHA256SUMS
project=__PROJECT__
if [[ "$mode" == --apply ]]; then
  "$project/.venv/bin/python" - "$project" <<'PYCODE'
import json,sys
from pathlib import Path
p=Path(sys.argv[1])/'outputs/ingest-server-batches/state'
d=json.loads((p/'maintenance.json').read_text());owner=json.loads((p/'owner.json').read_text())
assert d['drain'] is True and d['fingerprint']==owner['fingerprint'], 'Pause must already be enabled'
print('Selective backfill preserves the existing pause and all successful data.',flush=True)
PYCODE
fi
bash "$release_dir/upgrade_server.sh" "$release_dir/source.tar.gz" __SHA__ "$mode"
if [[ "$mode" == --prepare ]]; then exit 0; fi
cd "$project"
export PYTHONPATH="$project/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$project/.venv/bin/python" -u -m convest.ingest.repair --config "$project/configs/ingest.server.yaml" --apply
'''.replace('__PROJECT__',PROJECT).replace('__SHA__',sha)
    (out/'apply.sh').write_text(body)
    (out/'README.md').write_text('Pinned source '+commit+'\n\nDefault --prepare validates source without changing service. --apply upgrades source, selectively retries only Wuji internal-gap exclusions and returns the receiver to its existing maintenance pause. Does not reprocess successful sources, clear queues, remove bags or change quality thresholds. Run on native server, never SSHFS. Reports: code/operations/capture-time-backfill/reports/. Failures leave maintenance enabled; inspect report before any resume.\n')
    names=['source.tar.gz','upgrade_server.sh','apply.sh','README.md']
    checks={n:hashlib.sha256((out/n).read_bytes()).hexdigest() for n in names}
    (out/'SHA256SUMS').write_text(''.join(f'{value}  {name}\n' for name,value in checks.items()))
    (out/'release.json').write_text(json.dumps({'commit':commit,'files':checks,'preserve_pause':True,'selective_backfill':True},indent=2)+'\n')
    print(json.dumps({'commit':commit,'output':str(out),'archive_sha256':sha},indent=2))


if __name__=='__main__':main()
