#!/usr/bin/env bash
# Run on the data server. Default installs/checks only; --start installs the service.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$project_dir"
mode="${1:---prepare}"
if [[ "$mode" != --prepare && "$mode" != --start ]]; then
  echo 'Usage: bash deploy/setup_server.sh [--prepare|--start]' >&2
  exit 2
fi
if [[ "$(findmnt -n -o FSTYPE -T "$project_dir")" == fuse.sshfs ]]; then
  echo 'Run this script on the server, not through a workstation SSHFS mount.' >&2
  exit 2
fi
if [[ "$project_dir" != /data/data-fruits/real/harvest-tomato/code/auto-clean-convert ]]; then
  echo 'Unexpected installation directory; review the service and configuration paths first.' >&2
  exit 2
fi
if [[ -n "${PYTHON_BIN:-}" ]]; then
  python_bin="$PYTHON_BIN"
elif [[ -x runtime/python/bin/python3.12 ]]; then
  if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
    echo 'Bundled runtime targets Linux x86_64; provide a native PYTHON_BIN.' >&2
    exit 2
  fi
  python_bin="$project_dir/runtime/python/bin/python3.12"
else
  python_bin=python3
fi
"$python_bin" -c 'import sys; assert (3,11) <= sys.version_info[:2] <= (3,12), "Use validated Python 3.11 or 3.12; set PYTHON_BIN if needed"'
mkdir -p outputs/deployment
# Do not mutate an environment while its receiver is running.
if systemctl is-active --quiet auto-clean-convert.service; then
  echo 'auto-clean-convert is running; refusing to reinstall or restart it.' >&2
  exit 2
fi
"$python_bin" -m venv .venv
pip_options=(--only-binary=:all:)
if [[ -d deploy/wheels ]]; then
  pip_options+=(--no-index --find-links deploy/wheels)
fi
.venv/bin/python -m pip install "${pip_options[@]}" -r deploy/requirements-server.txt
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/python -m pip check
.venv/bin/python -m pip freeze > outputs/deployment/runtime.freeze.txt
if [[ ! -e configs/ingest.server.yaml ]]; then
  cp configs/ingest.server.example.yaml configs/ingest.server.yaml
fi
.venv/bin/python -m convest.ingest.cli check-config --config configs/ingest.server.yaml
.venv/bin/python - <<'PY'
from pathlib import Path
from convest.ingest.config import load_service_config
cfg=load_service_config(Path('configs/ingest.server.yaml'))
for route in cfg['routes']:
    source=Path(route['source'])
    if not source.is_dir() or source.is_symlink():raise SystemExit(f'Missing/unsafe input: {source}')
    with __import__('os').scandir(source) as entries:
        next(entries,None)
print('Four input roots readable; no data was enqueued or processed during setup checks.')
PY
if [[ "$mode" == --prepare ]]; then
  echo 'Environment prepared. No receiver started. Use --start after server acceptance checks.'
  exit 0
fi
sudo install -m 0644 deploy/ingest-worker.service.example /etc/systemd/system/auto-clean-convert.service
sudo systemctl daemon-reload
sudo systemctl enable --now auto-clean-convert.service
systemctl --no-pager status auto-clean-convert.service
