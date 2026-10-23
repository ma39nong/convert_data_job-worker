#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$project_dir"
if [[ ! -x "$project_dir/.venv/bin/python" ]]; then
  echo '请先按 README 建立 .venv 并安装 conversion、training 依赖。' >&2
  exit 1
fi
exec "$project_dir/.venv/bin/python" -m convest.gui.server "$@"
