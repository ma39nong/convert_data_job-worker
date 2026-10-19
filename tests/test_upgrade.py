"""Exercise the real upgrade shell script with isolated fake systemd, no sudo."""
from pathlib import Path
import hashlib
import io
import os
import subprocess
import sys
import tarfile

import pytest

from convest.config import WORKSPACE


@pytest.fixture
def upgrade(tmp_path):
    project=tmp_path/'installed';bins=tmp_path/'bin';bins.mkdir()
    (project/'.venv/bin').mkdir(parents=True)
    (project/'src/convest/ingest').mkdir(parents=True)
    worker=project/'src/convest/ingest/worker.py';worker.write_text('OLD = True\n')
    launcher=project/'.venv/bin/python'
    launcher.write_text(f'#!/bin/bash\nif [[ "$1" == -m ]]; then exit 0; fi\nexec "{sys.executable}" "$@"\n')
    launcher.chmod(0o755)
    fake=bins/'systemctl'
    fake.write_text('''#!/usr/bin/env python3
import os,sys
from pathlib import Path
a=sys.argv[1:];log=Path(os.environ['UPGRADE_LOG']);marker=log.with_suffix('.stopped')
worker=Path(os.environ['UPGRADE_WORKER'])
with log.open('a') as f:f.write(' '.join(a)+'\\n')
if a[0]=='show':
    p=a[a.index('-p')+1]
    if p=='MainPID':print('0' if marker.exists() else '123')
    elif p=='ActiveState':
        assert worker.read_text()=='OLD = True\\n', 'Source changed before clean exit check'
        print('inactive')
    elif p=='Result':print('success')
elif a[0]=='kill':
    assert worker.read_text()=='OLD = True\\n', 'Source overwritten while running'
    marker.touch()
elif a[0]=='start':
    assert marker.exists() and worker.read_text()=='NEW = True\\n'
    assert list(worker.parents[4].glob('.auto-clean-convert-upgrade.*/previous-source.tar.gz'))
''')
    # sys.executable is independent of PATH and the isolated project's launcher.
    fake.write_text(fake.read_text().replace('#!/usr/bin/env python3',f'#!{sys.executable}'))
    fake.chmod(0o755)
    for name,body in [('findmnt','echo ext4'),('sudo','if [[ "$1" == -v ]]; then exit 0; fi\nexec "$@"')]:
        p=bins/name;p.write_text('#!/bin/bash\n'+body+'\n');p.chmod(0o755)
    script=tmp_path/'upgrade.sh'
    script.write_text((WORKSPACE/'deploy/upgrade_server.sh').read_text().replace(
        'project=/data/data-fruits/real/harvest-tomato/code/auto-clean-convert',f'project={project}'))
    archive=tmp_path/'source.tar.gz'
    def pack(unsafe=False):
        with tarfile.open(archive,'w:gz') as tar:
            for name in ['src/convest/ingest/worker.py','src/convest/ingest/control.py','src/convest/readiness.py','deploy/setup_server.sh']:
                data=b'NEW = True\n' if name.endswith('.py') else b'#!/bin/bash\nexit 0\n'
                item=tarfile.TarInfo(name);item.size=len(data);tar.addfile(item,io.BytesIO(data))
            if unsafe:
                item=tarfile.TarInfo('../escape');item.size=1;tar.addfile(item,io.BytesIO(b'x'))
        return hashlib.sha256(archive.read_bytes()).hexdigest()
    env={**os.environ,'PATH':str(bins)+':'+os.environ['PATH'],
         'UPGRADE_LOG':str(tmp_path/'systemctl.log'),'UPGRADE_WORKER':str(worker)}
    return script,archive,pack,env,worker


def test_upgrade_prepare_leaves_service_and_source_untouched(upgrade):
    script,archive,pack,env,worker=upgrade
    p=subprocess.run(['bash',str(script),str(archive),pack()],env=env,capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    assert worker.read_text()=='OLD = True\n'
    assert not Path(env['UPGRADE_LOG']).exists()


@pytest.mark.parametrize('direct',[False,True])
def test_upgrade_waits_for_clean_exit_backs_up_and_only_then_starts(upgrade,direct):
    script,archive,pack,env,worker=upgrade
    config=worker.parents[3]/'configs/ingest.server.yaml'
    config.parent.mkdir();config.write_text('copy_input: true\nstate_root: preserve-this\n')
    original=config.read_bytes()
    args=['bash',str(script),str(archive),pack(),'--apply']
    if direct:args.append('--direct-input')
    p=subprocess.run(args,env=env,capture_output=True,text=True)
    assert p.returncode==0,p.stdout+p.stderr
    assert worker.read_text()=='NEW = True\n'
    log=Path(env['UPGRADE_LOG']).read_text()
    assert log.index('kill ')<log.index('show auto-clean-convert.service -p Result')<log.index('start ')
    if direct:
        import yaml
        assert yaml.safe_load(config.read_text())=={'copy_input':False,'state_root':'preserve-this'}
        backup=list(worker.parents[4].glob('.auto-clean-convert-upgrade.*/previous-ingest.server.yaml'))
        assert len(backup)==1 and backup[0].read_bytes()==original
    else:assert config.read_bytes()==original


@pytest.mark.parametrize('kind',['bad_hash','unsafe_archive'])
def test_invalid_upgrade_never_signals_running_service(upgrade,kind):
    script,archive,pack,env,worker=upgrade
    sha=pack(unsafe=kind=='unsafe_archive')
    if kind=='bad_hash':sha='0'*64
    p=subprocess.run(['bash',str(script),str(archive),sha,'--apply'],env=env,capture_output=True,text=True)
    assert p.returncode!=0
    assert worker.read_text()=='OLD = True\n'
    assert not Path(env['UPGRADE_LOG']).exists()
