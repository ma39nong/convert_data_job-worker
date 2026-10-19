#!/usr/bin/env bash
# Run this script from OUTSIDE the active checkout, on the server itself.
# Usage: bash upgrade_server.sh SOURCE.tar.gz EXPECTED_SHA256 [--prepare|--apply]
set -euo pipefail
project=/data/data-fruits/real/harvest-tomato/code/auto-clean-convert
unit=auto-clean-convert.service
archive="$(realpath -- "${1:?Provide the verified source archive}")"
expected="${2:?Provide its expected SHA256}"
mode="${3:---prepare}"
[[ "$mode" == --prepare || "$mode" == --apply ]] || { echo 'Use --prepare or --apply' >&2; exit 2; }
[[ "$expected" =~ ^[0-9a-f]{64}$ ]] || { echo 'Invalid SHA256' >&2; exit 2; }
[[ -d "$project/.venv" ]] || { echo 'Existing native installation required' >&2; exit 2; }
[[ "$(findmnt -n -o FSTYPE -T "$project")" != fuse.sshfs ]] || { echo 'Run on the server' >&2; exit 2; }
printf '%s  %s\n' "$expected" "$archive" | sha256sum -c -
stage="$(mktemp -d "$project/../.auto-clean-convert-upgrade.XXXXXXXX")"
# Deliberately preserve stage/backup if later validation fails.
"$project/.venv/bin/python" - "$archive" "$stage" <<'PY'
from pathlib import Path, PurePosixPath
import sys,tarfile
root=Path(sys.argv[2])
with tarfile.open(sys.argv[1]) as tar:
    for member in tar.getmembers():
        path=PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or not path.parts:
            raise SystemExit('Unsafe archive path')
        if path.parts[0] in {'.venv','.git','outputs','runtime'} or member.name=='configs/ingest.server.yaml':
            raise SystemExit('Archive must contain source only')
        if not (member.isfile() or member.isdir()):raise SystemExit('Archive links/special entries refused')
    tar.extractall(root,filter='data')
for file in (root/'src').rglob('*.py'):
    compile(file.read_bytes(),str(file),'exec')
for required in ['src/convest/ingest/worker.py','src/convest/ingest/control.py','src/convest/readiness.py']:
    if not (root/required).is_file():raise SystemExit('Incomplete source release')
print('Source archive and Python syntax validated; service not changed yet.')
PY
bash -n "$stage/deploy/setup_server.sh"
"$project/.venv/bin/python" -m pip check
if [[ "$mode" == --prepare ]]; then
  echo "Release validated at $stage. Service unchanged; use --apply to request graceful exit and upgrade."
  exit 0
fi
# Obtain sudo before requesting drain; never send SIGKILL on a timeout.
sudo -v
pid="$(systemctl show "$unit" -p MainPID --value)"
if [[ "$pid" != 0 ]]; then
  sudo systemctl kill --kill-who=main --signal=SIGTERM "$unit"
  echo "Requested graceful exit of PID $pid; current scan/job may take a long time."
  while [[ "$(systemctl show "$unit" -p MainPID --value)" == "$pid" ]]; do
    sleep 10
  done
fi
[[ "$(systemctl show "$unit" -p ActiveState --value)" == inactive ]] || {
  echo 'Service did not exit cleanly; source not replaced.' >&2; exit 2;
}
[[ "$(systemctl show "$unit" -p Result --value)" == success ]] || {
  echo 'Service failure requires inspection; source not replaced.' >&2; exit 2;
}
backup="$stage/previous-source.tar.gz"
"$project/.venv/bin/python" - "$project" "$stage" "$backup" <<'PY'
from pathlib import Path
import shutil,sys,tarfile
project,stage,backup=map(Path,sys.argv[1:])
files=[p for p in stage.rglob('*') if p.is_file()]
# Save every file that will be replaced. Runtime, queue and data are untouched.
with tarfile.open(backup,'w:gz') as tar:
    for file in files:
        relative=file.relative_to(stage);target=project/relative
        if target.is_symlink():raise SystemExit('Refuse symlink in installed source')
        if target.exists():tar.add(target,arcname=str(relative),recursive=False)
for file in files:
    target=project/file.relative_to(stage)
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(file,target)
print('Source updated; rollback archive:',backup)
PY
cd "$project"
.venv/bin/python -m convest.ingest.cli check-config --config configs/ingest.server.yaml
.venv/bin/python -m pip check
sudo systemctl start "$unit"
systemctl --no-pager -l status "$unit"
echo "Upgrade complete. Existing maintenance requests remain in force; resume explicitly if needed."
echo "Source backup: $backup"
