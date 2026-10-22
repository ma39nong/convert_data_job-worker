"""Native dimensions, sealed handoff, independent queues, retry and verified delivery."""
import json
from pathlib import Path
import shutil
import time
import os
import subprocess
import sys

import numpy as np
import pyarrow.parquet as pq
import pytest

from convest.config import digest
from convest.capture.worker import context as capture_context
from convest.capture import daily
from convest.capture.transfer import upload_once as capture_upload, manifest
from convest.ingest.batches import BatchPublisher
from convest.jobs import checksum
from convest.training import worker
from convest.training.adatempo.lerobot_io import read_source
from convest.training.adatempo.lerobot_checks import validate_output
from convest.training.normal import validate_normal
from convest.training.ssh_remote import run as remote_run, Conflict
from test_ingest import service, bag
from test_capture import capture, ready
from test_capture_daily import daily_cfg, due


@pytest.fixture
def training(daily_cfg, tmp_path, monkeypatch):
    target = tmp_path / 'training-server/workspace/data'
    target.mkdir(parents=True)
    cfg = {'capture': daily_cfg, 'state_root': str(tmp_path / 'state-training'),
           'output_root': str(tmp_path / 'training-output'), 'workers': 1,
           'cmax': 3.0, 'min_free_gib': 0, 'retry_seconds': 1,
           'training_target': {'transport': 'mount', 'require_mount': str(tmp_path / 'training-server'),
                               'dataset_root': str(target)}}
    cfg['fingerprint'] = digest(cfg)
    real = __import__('os').path.ismount
    monkeypatch.setattr('os.path.ismount', lambda p: p == cfg['training_target']['require_mount'] or real(p))
    return cfg


def prepare(cfg, routes=(0, 2)):
    capture = cfg['capture']
    for route in routes:
        bag(capture, route, 1)
    with capture_context(capture) as q:
        ready(capture, q)
        for _ in routes:
            assert capture_upload(capture, q)
        daily.maintain(capture, q, due(capture))
    return capture


def reset_retry(db):
    with db:
        db.execute('UPDATE tasks SET next_ns=0')


def test_four_routes_preserve_upstream_native_vectors_and_deliver_both_versions(training):
    cfg = training
    upstream = prepare(cfg, (0, 1, 2, 3))
    before = {str(p): checksum(p) for r in upstream['routes'] for p in Path(r['output']).rglob('*')
              if p.is_file() and not p.is_symlink()}
    with worker.context(cfg) as q:
        worker.discover(cfg, q)
        assert q.execute('SELECT count(*) FROM tasks').fetchone()[0] == 4
        for _ in range(4):
            assert worker.process_once(cfg, q)
        assert {r[0] for r in q.execute('SELECT state FROM tasks')} == {'local_ready'}
        for row in q.execute('SELECT * FROM tasks'):
            spec = json.loads(row['spec'])
            pair = Path(json.loads(row['payload'])['source'])
            normal, speedup = pair / 'normal', pair / 'speedup'
            a = validate_normal(spec['source'], normal, spec['robot'])
            b = validate_output(normal, speedup)
            assert a['native_dimensions'] == ([116, 58] if spec['robot'] == 'sharpa' else [108, 54])
            info, _, episodes = read_source(normal)
            assert episodes[0]['actions'].shape[1] == a['native_dimensions'][1]
            assert b['frames'] < a['frames']
            for name in ('observation.state', 'action'):
                original = json.loads((Path(spec['source']) / 'meta/info.json').read_text())
                assert info['features'][name] == original['features'][name]
            assert 'observation.arm_engaged' in info['features']
            assert 'observation.engaged' not in info['features']
            assert 'observation.depths.cam0' not in info['features']
            assert not any('tactile' in key for key in info['features'])
        for _ in range(4):
            assert worker.upload_once(cfg, q)
        assert not worker.upload_once(cfg, q)
        assert {r[0] for r in q.execute('SELECT state FROM tasks')} == {'uploaded'}
        worker.discover(cfg, q)
        assert not worker.process_once(cfg, q)
    assert all(checksum(Path(name)) == sha for name, sha in before.items())
    target = Path(cfg['training_target']['dataset_root'])
    assert len(list(target.glob('*/*/*/normal/meta/info.json'))) == 4
    assert len(list(target.glob('*/*/*/speedup/meta/info.json'))) == 4
    assert len(list(target.glob('.training-transfer/*/*.json'))) == 4
    # The original batch uploads and ledgers continue normally, on their own queue.
    with capture_context(upstream) as q:
        for _ in range(4):
            assert capture_upload(upstream, q)
        assert q.db.execute("SELECT count(*) FROM uploads WHERE state='completed'").fetchone()[0] == 8


def test_active_or_invalid_seal_never_starts_processing(training):
    cfg = training
    bag(cfg['capture'], 0, 1)
    with capture_context(cfg['capture']) as q:
        ready(cfg['capture'], q)
        with worker.context(cfg) as db:
            report = worker.discover(cfg, db)
            assert report['sharpa-A']['batch-000001']['state'] == 'waiting_seal'
            assert not worker.process_once(cfg, db)
            assert not Path(cfg['output_root']).exists()
        daily.maintain(cfg['capture'], q, due(cfg['capture']))
    route = cfg['capture']['routes'][0]
    seal = Path(route['output']) / '.ingest/seals/batch-000001.json'
    d = json.loads(seal.read_text()); d['full_video_decode'] = False; seal.write_text(json.dumps(d))
    with worker.context(cfg) as db:
        assert worker.discover(cfg, db)['sharpa-A']['state'] == 'blocked'
        assert not worker.process_once(cfg, db)


def test_speedup_retry_keeps_verified_normal_and_no_partial_pair_is_visible(training, monkeypatch):
    cfg = training
    prepare(cfg, (0,))
    import convest.training.adatempo.lerobot_main as algorithm
    real = algorithm.process
    def fail(source, destination, **kwargs):
        (destination / 'incomplete').write_text('failure')
        raise RuntimeError('simulated encoder failure')
    monkeypatch.setattr(algorithm, 'process', fail)
    with worker.context(cfg) as db:
        worker.discover(cfg, db); worker.process_once(cfg, db)
        row = db.execute('SELECT * FROM tasks').fetchone()
        assert row['state'] == 'retry' and not row['payload']
        assert not list(Path(cfg['output_root']).glob('*/*/*/pipeline.json'))
        normal = next(Path(cfg['output_root']).glob('.staging/*/normal'))
        files = manifest(normal)
        monkeypatch.setattr(algorithm, 'process', real)
        import convest.training.normal as projection
        monkeypatch.setattr(projection, 'export', lambda *a: pytest.fail('must not rerun normal'))
        reset_retry(db); worker.process_once(cfg, db)
        row = db.execute('SELECT * FROM tasks').fetchone()
        assert row['state'] == 'local_ready', row['error']
        pair = Path(json.loads(row['payload'])['source'])
        assert manifest(pair / 'normal') == files
        assert list(Path(cfg['output_root']).glob('.failed/*/incomplete'))


def test_upload_disconnect_and_conflict_never_reprocess_or_overwrite(training, monkeypatch):
    cfg = training
    prepare(cfg, (2,))
    with worker.context(cfg) as db:
        worker.discover(cfg, db); worker.process_once(cfg, db)
        row = db.execute('SELECT * FROM tasks').fetchone()
        payload = json.loads(row['payload']); attempts = row['attempts']
        with monkeypatch.context() as patch:
            patch.setattr(worker, 'check_mount', lambda _: (_ for _ in ()).throw(OSError('offline')))
            worker.upload_once(cfg, db)
        assert db.execute('SELECT state FROM tasks').fetchone()[0] == 'upload_retry'
        assert not worker.process_once(cfg, db)
        destination = Path(cfg['training_target']['dataset_root']) / payload['robot'] / payload['group'] / payload['name']
        destination.mkdir(parents=True); (destination / 'foreign').write_text('keep')
        reset_retry(db); worker.upload_once(cfg, db)
        assert db.execute('SELECT state FROM tasks').fetchone()[0] == 'conflict'
        assert (destination / 'foreign').read_text() == 'keep'
        assert db.execute('SELECT attempts FROM tasks').fetchone()[0] == attempts


def test_sealed_input_corruption_and_output_symlink_are_rejected(training):
    cfg = training
    prepare(cfg, (0,))
    with worker.context(cfg) as db:
        worker.discover(cfg, db)
        row = db.execute('SELECT * FROM tasks').fetchone()
        source = Path(json.loads(row['spec'])['source'])
        artifact = next(source.glob('data/**/*.parquet'))
        artifact.chmod(0o644); b = artifact.read_bytes(); artifact.write_bytes(b[:-1] + bytes([b[-1] ^ 1]))
        worker.process_once(cfg, db)
        assert db.execute('SELECT state FROM tasks').fetchone()[0] == 'conflict'
        assert not list(Path(cfg['output_root']).glob('*/*/*/pipeline.json'))


def request(tmp_path):
    root = tmp_path / 'workspace/data'; root.mkdir(parents=True)
    source = tmp_path / 'pair'; source.mkdir()
    for name in ('normal/info.json', 'speedup/info.json', 'pipeline.json'):
        path = source / name; path.parent.mkdir(exist_ok=True); path.write_text(name)
    return source, {'root': str(root), 'robot': 'sharpa', 'group': 'A',
                    'name': '1_1_data_lerobot--batch-000001--' + 'a' * 16,
                    'task': 'a' * 64, 'fingerprint': 'b' * 64,
                    'files': manifest(source), 'operation': 'prepare'}


def test_ssh_remote_atomic_verified_commit_and_crash_recovery(tmp_path):
    source, req = request(tmp_path)
    prepared = remote_run(req)
    stage = Path(prepared['staging'])
    shutil.copytree(source, stage, dirs_exist_ok=True)
    dst = Path(req['root']) / req['robot'] / req['group'] / req['name']
    assert not dst.exists()
    result = remote_run({**req, 'operation': 'commit'})
    assert dst.is_dir() and not stage.exists()
    assert json.loads(Path(result['receipt']).read_text())['status'] == 'completed'
    assert remote_run(req)['status'] == 'existing'
    assert remote_run({**req, 'operation': 'commit'})['status'] == 'completed'
    (dst / 'normal/info.json').write_text('corruption')
    with pytest.raises(Conflict):
        remote_run(req)


def test_ssh_remote_rejects_corruption_escape_and_symlink(tmp_path):
    source, req = request(tmp_path)
    stage = Path(remote_run(req)['staging'])
    shutil.copytree(source, stage, dirs_exist_ok=True)
    (stage / 'normal/info.json').write_text('corrupt')
    with pytest.raises(Conflict):
        remote_run({**req, 'operation': 'commit'})
    assert stage.exists()
    with pytest.raises(ValueError):
        remote_run({**req, 'name': '../../outside'})
    (stage / 'normal/info.json').unlink()
    (stage / 'normal/info.json').symlink_to(source / 'normal/info.json')
    with pytest.raises(Conflict):
        remote_run({**req, 'operation': 'commit'})


def test_owner_and_same_role_lock_prevent_concurrent_consumers(training):
    with worker.context(training) as db:
        with pytest.raises(BlockingIOError):
            with worker.context(training):
                pass
        with worker.context(training, 'upload'):
            pass
    changed = {**training, 'fingerprint': 'different'}
    with pytest.raises(ValueError, match='configuration mismatch'):
        with worker.context(changed):
            pass


def test_external_capture_checkout_keeps_its_paths_and_fingerprint(tmp_path):
    import yaml
    from convest.config import WORKSPACE
    from convest.training.config import load
    upstream = tmp_path / 'other-capture-checkout'
    for folder in ('src', 'configs', 'schemas'):
        shutil.copytree(WORKSPACE / folder, upstream / folder,
                        ignore=shutil.ignore_patterns('__pycache__', '*.egg-info'))
    config = upstream / 'configs/capture.daily.yaml'
    expected = json.loads(subprocess.check_output([sys.executable, '-c',
        'import json,sys; from convest.capture.config import load; print(json.dumps(load(sys.argv[1])))',
        str(config)], env={**os.environ, 'PYTHONPATH': str(upstream / 'src')}, cwd=upstream, text=True))
    raw = yaml.safe_load((WORKSPACE / 'configs/training.example.yaml').read_text())
    raw.update(capture_project=str(upstream))
    path = tmp_path / 'training.yaml'; path.write_text(yaml.safe_dump(raw))
    cfg = load(path)
    assert cfg['capture']['fingerprint'] == expected['fingerprint']
    assert cfg['capture']['state_root'] == str(upstream / 'outputs/capture-daily/state')
    assert cfg['capture']['local_batch_root'] == str(upstream / 'outputs/capture-daily/batches')
    assert not (upstream / 'outputs').exists()  # read-only config check


def test_large_ssh_manifest_uses_stdin_and_keeps_configured_identity(monkeypatch):
    from convest.training.ssh_transfer import remote
    rows = [{'path': f'normal/data/episode_{i:06d}.parquet', 'sha256': 'a' * 64,
             'size': 123} for i in range(5000)]
    request = {'files': rows}
    def call(args, **kw):
        assert len(kw['input']) > 128 * 1024
        assert json.loads(kw['input']) == request
        assert max(map(len, args)) < 128 * 1024
        assert args[args.index('-p') + 1] == '28'
        assert args[args.index('-l') + 1] == 'someone'
        return subprocess.CompletedProcess(args, 0, '{"status":"staging"}', '')
    monkeypatch.setattr(subprocess, 'run', call)
    assert remote({'host': 'train-alias', 'user': 'someone', 'port': 28,
                   'python': 'python3'}, request)['status'] == 'staging'


def test_two_frame_episode_is_preserved_as_one_next_action_pair(tmp_path):
    from convest.training.adatempo.algorithm import adatempo
    source = [{'id': 'short', 'qpos': np.zeros((2, 58)),
               'actions': np.ones((2, 58)), 'gripper': np.zeros(2)}]
    result, trace = adatempo(source)
    assert len(result) == 1 and np.shape(result[0]['actions']) == (1, 58)
    assert result[0]['source_index'] == [0] and result[0]['action_source_index'] == [1]
