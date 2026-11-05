#!/usr/bin/env bash
set -euo pipefail
TASK_SUBTASKS_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TASK_SUBTASKS_ROOT"
exec .venv/bin/python -m convest.subtasks.server "$@"
