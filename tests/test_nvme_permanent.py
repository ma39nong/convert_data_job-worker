"""Permanent work placement guards; no root mount is performed by these tests."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys

import pytest
from convest.config import WORKSPACE

sys.path.insert(0, str(WORKSPACE / 'scripts'))
try:
    spec = importlib.util.spec_from_file_location('nvme_permanent', WORKSPACE / 'scripts/nvme_permanent.py')
    permanent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(permanent)
finally:
    sys.path.remove(str(WORKSPACE / 'scripts'))

OWNER = {'owner': 'convest-ingest-v1', 'kind': 'work', 'fingerprint': 'test'}


def work(path):
    path.mkdir()
    (path / 'owner.json').write_text(json.dumps(OWNER))
    return path


def setup(tmp_path, monkeypatch):
    source, target = tmp_path / 'nvme-work', work(tmp_path / 'original-work')
    units = {tmp_path / 'etc' / 'work.mount': permanent.TAG + '[Mount]\n',
             tmp_path / 'etc' / 'dropin.conf': permanent.TAG + '[Unit]\n'}
    calls = []
    mounted = [False]
    def command(*args):
        calls.append(args)
        if args[:3] == ('sudo', 'install', '-d'):
            Path(args[-1]).mkdir(parents=True, exist_ok=True)
        elif args[:2] == ('sudo', 'install'):
            shutil.copyfile(args[-2], args[-1])
        elif args[:3] == ('sudo', 'systemctl', 'start'):
            mounted[0] = True
        elif args[:3] == ('sudo', 'systemctl', 'stop'):
            mounted[0] = False
        elif args[:2] == ('sudo', 'rm'):
            Path(args[-1]).unlink()
        return ''
    monkeypatch.setattr(permanent, 'command', command)
    monkeypatch.setattr(permanent, 'bound', lambda *_: mounted[0])
    monkeypatch.setattr(permanent.os.path, 'ismount', lambda p: mounted[0] if Path(p) == target else False)
    return source, target, units, calls, mounted


def test_apply_and_rollback_preserve_original_marker_and_nvme_data(tmp_path, monkeypatch):
    source, target, units, calls, mounted = setup(tmp_path, monkeypatch)
    original = (target / 'owner.json').read_bytes()
    permanent.switch_idle(source, target, OWNER, units, tmp_path / 'report')
    assert mounted[0] and all(p.read_text() == text for p, text in units.items())
    assert json.loads((source / 'owner.json').read_text()) == OWNER
    assert (target / 'owner.json').read_bytes() == original
    assert source.stat().st_mode & 0o777 == 0o700
    permanent.switch_idle(source, target, OWNER, units, tmp_path / 'rollback', rollback=True)
    assert not mounted[0] and all(not p.exists() for p in units)
    assert (source / 'owner.json').exists()  # rollback never deletes NVMe data
    assert (target / 'owner.json').read_bytes() == original


@pytest.mark.parametrize('location', ['original', 'nvme'])
def test_unfinished_work_is_never_hidden_or_deleted(tmp_path, monkeypatch, location):
    source, target, units, calls, _ = setup(tmp_path, monkeypatch)
    path = target if location == 'original' else work(source)
    (path / 'unfinished-job').mkdir()
    (path / 'unfinished-job' / 'bag.db3').write_bytes(b'preserve')
    with pytest.raises(ValueError, match='not empty'):
        permanent.switch_idle(source, target, OWNER, units, tmp_path / 'report')
    assert (path / 'unfinished-job' / 'bag.db3').read_bytes() == b'preserve'
    assert not calls


def test_unrelated_units_are_never_overwritten(tmp_path, monkeypatch):
    source, target, units, calls, _ = setup(tmp_path, monkeypatch)
    existing = next(iter(units))
    existing.parent.mkdir()
    existing.write_text('another administrator setting')
    with pytest.raises(ValueError, match='differs'):
        permanent.switch_idle(source, target, OWNER, units, tmp_path / 'report')
    assert existing.read_text() == 'another administrator setting' and not calls


@pytest.mark.parametrize('bad', ['wrong_marker', 'symlink', 'foreign_owner'])
def test_unowned_nvme_directory_is_rejected(tmp_path, monkeypatch, bad):
    source, target, units, calls, _ = setup(tmp_path, monkeypatch)
    if bad == 'symlink':
        other = work(tmp_path / 'other')
        source.symlink_to(other)
    else:
        work(source)
        if bad == 'wrong_marker':
            (source / 'owner.json').write_text('{}')
        else:
            original_stat = Path.stat
            def stat(p, *a, **kw):
                result = original_stat(p, *a, **kw)
                if p == source:
                    parts = list(result)
                    parts[4] = os.getuid() + 1
                    return os.stat_result(parts)
                return result
            monkeypatch.setattr(Path, 'stat', stat)
    with pytest.raises(ValueError):
        permanent.switch_idle(source, target, OWNER, units, tmp_path / 'report')
    assert not calls


def test_service_depends_on_mount_and_checks_exact_source(tmp_path, monkeypatch):
    source, target = tmp_path / 'nvme', tmp_path / 'data-work'
    monkeypatch.setattr(permanent, 'command', lambda *a: 'data-work.mount')
    content = list(permanent.units(source, target).values())
    assert f'RequiresMountsFor={source}' in content[0]
    assert 'Options=bind' in content[0]
    assert 'Requires=data-work.mount' in content[1] and 'After=data-work.mount' in content[1]
    assert f'ExecStartPre=/usr/bin/test {source} -ef {target}' in content[1]
    assert 'mountpoint -q' in content[1]
