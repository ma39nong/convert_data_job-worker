#!/usr/bin/env python3
"""Install a user-owned always-on capture service; no server units are touched."""
from pathlib import Path
import argparse
import json
import os
import pwd
import subprocess
from convest.config import WORKSPACE,atomic_json
from convest.capture.config import load

UNIT='capture-convert-transfer.service'
TAG='# Managed by convert-data capture daily service v1\n'


def quote(value):
    value=str(value)
    if '\n' in value or '\r' in value:raise ValueError('Unsupported systemd argument')
    return '"'+value.replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'


def render(project,config):
    return TAG+('''[Unit]
Description=Capture computer local cleaning, 24h LeRobot batching and verified upload
StartLimitIntervalSec=0

[Service]
Type=simple
'''+f"WorkingDirectory={str(project).replace('%','%%')}\nEnvironment=PYTHONUNBUFFERED=1\n"
      +f'ExecStart={quote(project/".venv/bin/python")} -u -m convest.capture.cli run --config {quote(config)}\n'
      +'''Restart=always
RestartSec=15
KillMode=control-group
TimeoutStopSec=infinity
UMask=0022

[Install]
WantedBy=default.target
''')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True,type=Path)
    p.add_argument('--apply',action='store_true')
    args=p.parse_args();config=args.config.resolve();cfg=load(config)
    if cfg.get('delivery_mode')!='daily' or cfg['batch_hours']!=24:
        raise ValueError('This service requires the reviewed 24h daily configuration')
    if os.getuid()==0:raise ValueError('Install as the capture user, not root')
    unit=Path.home()/'.config/systemd/user'/UNIT
    content=render(WORKSPACE,config)
    if unit.is_symlink() or (unit.exists() and not unit.read_text().startswith(TAG)):
        raise ValueError('Unrelated existing unit preserved')
    staged=WORKSPACE/'outputs/capture-service'/UNIT
    staged.parent.mkdir(parents=True,exist_ok=True);staged.write_text(content)
    subprocess.run(['systemd-analyze','--user','verify',str(staged)],check=True)
    if not args.apply:
        print(content);return
    if unit.exists() and unit.read_text()!=content:
        raise ValueError('Installed service differs; stop and review before replacing')
    # Own-user linger is permitted by login1 policy on the current workstation;
    # no sudo or authentication prompt is used. Fail explicitly if unavailable.
    user=pwd.getpwuid(os.getuid()).pw_name
    subprocess.run(['loginctl','--no-ask-password','enable-linger',user],check=True)
    unit.parent.mkdir(parents=True,exist_ok=True);unit.write_text(content)
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    subprocess.run(['systemctl','--user','enable','--now',UNIT],check=True)
    result=subprocess.run(['systemctl','--user','show',UNIT,'-p','ActiveState','-p','MainPID','-p','UnitFileState'],check=True,capture_output=True,text=True)
    report={'unit':str(unit),'config':str(config),'state':dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line),
            'linger':subprocess.check_output(['loginctl','show-user',user,'-p','Linger'],text=True).strip()}
    atomic_json(WORKSPACE/'outputs/capture-service/install.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        raise SystemExit(f'{type(exc).__name__}: {exc}')
