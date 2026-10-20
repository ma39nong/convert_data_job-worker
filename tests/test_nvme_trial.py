"""Isolated trial timing/equivalence and maintenance boundaries."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from types import SimpleNamespace

import pytest
import yaml
from convest.config import WORKSPACE
from convest.ingest.inputs import check_input, snapshot
from convest.ingest.config import load_service_config
from conversion_helpers import make_conversion_bag

spec = importlib.util.spec_from_file_location('nvme_trial', WORKSPACE / 'scripts/nvme_trial.py')
trial = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trial)


def test_timing_buckets_are_nonoverlapping_and_include_final_event():
    events = [{'phase': 'readiness.start', 'elapsed_s': 1},
              {'phase': 'readiness.hashing', 'elapsed_s': 3},
              {'phase': 'job.trimming', 'elapsed_s': 5},
              {'phase': 'job.converting', 'elapsed_s': 8},
              {'phase': 'job.output_verification', 'elapsed_s': 12},
              {'phase': 'original_final.hashing', 'elapsed_s': 17},
              {'phase': 'publishing', 'elapsed_s': 19},
              {'phase': 'cleanup', 'elapsed_s': 22}]
    result = trial.durations(events, 23)
    assert result['input_check'] == 4
    assert result['cleaning'] == 3
    assert result['conversion'] == 4
    assert result['output_verification'] == 5
    assert result['final_source_hash'] == 2
    assert result['publication'] == 3
    assert result['cleanup'] == 1
    assert sum(result.values()) == 23


def test_restore_only_clears_our_own_maintenance_request(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    state.mkdir()
    lease = {'drain': True, 'requested_ns': 100}
    trial.write(state / 'maintenance.json', lease)
    commands = []
    monkeypatch.setattr(trial, 'cli_command', lambda *args: commands.append(args[-1]))
    assert trial.restore_receiver(tmp_path, tmp_path / 'config', state, None) == 'previous_maintenance_preserved'
    changed = {**lease, 'requested_ns': 101}
    trial.write(state / 'maintenance.json', changed)
    assert trial.restore_receiver(tmp_path, tmp_path / 'config', state, lease) == 'maintenance_changed_externally'
    assert not commands
    trial.write(state / 'maintenance.json', lease)
    assert trial.restore_receiver(tmp_path, tmp_path / 'config', state, lease) == 'resume_requested'
    assert commands == ['resume']


def test_failing_drain_restores_owned_request_and_ignores_stale_drained_status(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    state.mkdir()
    import sqlite3
    with sqlite3.connect(state / 'queue.sqlite3') as db:
        db.execute('create table jobs(state text)')
    trial.write(state / 'status.json', {'maintenance': 'drained', 'updated_ns': 50})
    lease = {'drain': True, 'requested_ns': 100}
    calls = []
    def command(project, config, command):
        calls.append(command)
        if command == 'drain':
            trial.write(state / 'maintenance.json', lease)
    monkeypatch.setattr(trial, 'cli_command', command)
    monkeypatch.setattr(trial.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='active\n'))
    with pytest.raises(InterruptedError):
        trial.pause_receiver(tmp_path, tmp_path / 'config', state, [True])
    assert calls == ['drain', 'resume']


def test_choose_nvme_refuses_root_disk_and_low_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(trial.os, 'access', lambda *a: True)
    monkeypatch.setattr(trial.shutil, 'disk_usage', lambda p: SimpleNamespace(free=100))
    monkeypatch.setattr(trial.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='/dev/sda4 ext4\n'))
    with pytest.raises(ValueError, match='No writable NVMe'):
        trial.choose_nvme([tmp_path], 50)
    monkeypatch.setattr(trial.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='/dev/nvme0n1p1 ext4\n'))
    with pytest.raises(ValueError, match='No writable NVMe'):
        trial.choose_nvme([tmp_path], 101)
    assert trial.choose_nvme([tmp_path], 100)[0] == tmp_path.resolve()


@pytest.mark.parametrize('robot', ['wuji', 'sharpa'])
def test_cloned_project_runs_real_receiver_with_identical_values_and_safe_cleanup(tmp_path, robot):
    """Exercise the actual subprocess/import/path/receiver flow on two locations.

    This validates correctness and isolation, not NVMe performance on CI.
    """
    config = tmp_path / 'production.yaml'
    source = make_conversion_bag(tmp_path / 'inputs/origin-rosbag' / robot / 'A/episode62', robot, frames=18)
    config.write_text(yaml.safe_dump({'schema_version': 1, 'data_root': str(tmp_path / 'inputs'),
                                    'state_root': str(tmp_path / 'production-state'),
                                    'work_root': 'outputs/trial-unit-unused-' + uuid.uuid4().hex,
                                    'initial_history': {'mode': 'all'},
                                    'routes': [{'robot': robot, 'group': 'A'}]}))
    original = {p.name: trial.hash_file(p) for p in source.iterdir()}
    cfg = load_service_config(config)
    proof = check_input(source, cfg['routes'][0], snapshot(source))
    signature = trial.code_signature(WORKSPACE)
    results = []
    for variant in ['data', 'nvme']:
        project = tmp_path / variant / 'project'
        trial.clone_project(WORKSPACE, project, signature)
        run_root = tmp_path / 'runs' / variant
        args = {'project': str(project), 'production_config': str(config), 'route': robot + '-A',
                'episode': source.name, 'trial': 'unit', 'name': variant, 'variant': variant,
                'run_root': str(run_root), 'expected_identity': proof['content_identity']}
        spec = tmp_path / (variant + '.json')
        trial.write(spec, args)
        env = {**os.environ, 'PYTHONPATH': str(project / 'src'), 'PYTHONNOUSERSITE': '1'}
        completed = subprocess.run([sys.executable, str(project / 'scripts/nvme_trial.py'), '--child', str(spec)],
                                   cwd=project, env=env, capture_output=True, text=True)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        result = trial.read(run_root / 'result.json')
        assert result['status'] == 'passed'
        assert result['source_hash_owner'] == 'ingest.final_original_hash'
        assert result['stages_s']['publication'] > 0
        assert abs(sum(result['stages_s'].values()) - result['total_s']) < .0001
        assert not [p for p in (project / 'outputs' / variant).iterdir() if p.is_dir()]
        results.append(result)
    assert trial.compare_results(results)['status'] == 'passed'
    summary = trial.summarize(results)
    assert set(summary['variants']) == {'data', 'nvme'}
    assert {p.name: trial.hash_file(p) for p in source.iterdir()} == original
    assert not (tmp_path / 'production-state').exists()
    assert not (tmp_path / 'inputs/data-lerobot').exists()


def test_only_one_native_trial_can_hold_the_coordinator_lock(tmp_path):
    project = tmp_path / 'code/project'
    project.mkdir(parents=True)
    lock = trial.acquire_trial_lock(project)
    try:
        with pytest.raises(RuntimeError, match='already running'):
            trial.acquire_trial_lock(project)
    finally:
        lock.close()
    trial.acquire_trial_lock(project).close()


def test_nvme_diagnostic_separates_permissions_from_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(trial.os, 'access', lambda *a: False)
    monkeypatch.setattr(trial.shutil, 'disk_usage', lambda p: SimpleNamespace(free=200))
    monkeypatch.setattr(trial.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='/dev/nvme0n1p1 ext4\n'))
    row = trial.inspect_nvme([tmp_path], 100)[0]
    assert row['reason'] == 'directory_not_writable_or_traversable'
    assert row['free_GiB'] is not None and row['permissions']
    row = trial.inspect_nvme([tmp_path], 300)[0]
    assert row['reason'] == 'insufficient_free_space'
    assert not row['usable']


def test_nvme_can_use_writable_child_when_mount_root_is_not_writable(tmp_path, monkeypatch):
    child = tmp_path / 'user'
    child.mkdir()
    monkeypatch.setattr(trial.os, 'access', lambda p, mode: Path(p) == child)
    monkeypatch.setattr(trial.shutil, 'disk_usage', lambda p: SimpleNamespace(free=200))
    monkeypatch.setattr(trial.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='/dev/nvme0n1p1 ext4\n'))
    selected, mount = trial.choose_nvme([tmp_path, child], 100)
    assert selected == child and mount.startswith('/dev/nvme')
