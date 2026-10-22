#!/usr/bin/env python3
"""Install the separate derivative worker, leaving the capture service untouched."""
import argparse
from pathlib import Path
import os
import pwd
import subprocess

from convest.config import WORKSPACE
from convest.training.config import load

UNIT = 'capture-normal-speedup.service'
TAG = '# Managed by convert-data native normal/speedup v1\n'


def quote(value):
    value = str(value)
    if '\n' in value or '\r' in value:
        raise ValueError('Unsupported systemd argument')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def render(project, config):
    if '\n' in str(project) or '\r' in str(project):
        raise ValueError('Unsupported systemd directory')
    return TAG + '''[Unit]
Description=Local sealed 24h LeRobot normal/speedup and verified training-server upload
StartLimitIntervalSec=0

[Service]
Type=simple
''' + f'WorkingDirectory={str(project).replace("%", "%%")}\nExecStart={quote(project / ".venv/bin/python")} -u -m convest.training.cli run --config {quote(config)}\n' + '''Environment=PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 NUMBA_NUM_THREADS=1
Restart=always
RestartSec=30
Nice=10
CPUQuota=200%
MemoryHigh=4G
MemoryMax=8G
KillMode=mixed
TimeoutStopSec=infinity
UMask=0022

[Install]
WantedBy=default.target
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if os.getuid() == 0:
        raise ValueError('Install as the recording user, not root')
    config = args.config.resolve()
    load(config)
    content = render(WORKSPACE, config)
    stage = WORKSPACE / 'outputs/training-service' / UNIT
    stage.parent.mkdir(parents=True, exist_ok=True)
    stage.write_text(content)
    subprocess.run(['systemd-analyze', '--user', 'verify', str(stage)], check=True)
    if not args.apply:
        print(content)
        return
    unit = Path.home() / '.config/systemd/user' / UNIT
    if unit.is_symlink() or (unit.exists() and unit.read_text() != content):
        raise ValueError('Different installed unit preserved; stop/review before replacing')
    user = pwd.getpwuid(os.getuid()).pw_name
    subprocess.run(['loginctl', '--no-ask-password', 'enable-linger', user], check=True)
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(content)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', '--now', UNIT], check=True)
    subprocess.run(['systemctl', '--user', 'show', UNIT, '-p', 'ActiveState', '-p', 'MainPID'], check=True)


if __name__ == '__main__':
    main()
