"""Historical snapshots validate full cached inputs without changing batch state."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from convest.config import WORKSPACE
from convest.capture.worker import context
from test_ingest import service, bag
from test_capture import capture, ready
from test_capture_daily import daily_cfg

module = importlib.util.spec_from_file_location('historical_training', WORKSPACE / 'scripts/process_historical_training.py')
history = importlib.util.module_from_spec(module)
module.loader.exec_module(history)


def run_cache(code, request):
    result = subprocess.run([sys.executable, '-c', code], input=json.dumps(request),
                            text=True, capture_output=True)
    if result.returncode:
        raise ValueError(result.stderr)
    return json.loads(result.stdout)


def test_active_snapshot_validates_original_hashes_and_retained_chunks_without_sealing(daily_cfg, tmp_path):
    cfg = daily_cfg
    bag(cfg, 0, 1)
    with context(cfg) as q:
        ready(cfg, q)
    route = Path(cfg['routes'][0]['output'])
    before = (route / '.ingest/batches.json').read_bytes()
    spec = history.freeze(route)
    assert spec['upstream_batch_state'] == 'active'
    assert sum(len(f.get('ranges', [])) for f in spec['files']) > 0
    cached = tmp_path / 'workspace/data/sharpa/A' / spec['name']
    for area in ('meta', 'data', 'videos'):
        shutil.copytree(Path(spec['source']) / area, cached / area)
    req = {'root': str(tmp_path / 'workspace/data'), 'robot': 'sharpa', 'group': 'A',
           'name': spec['name'], 'files': spec['files']}
    result = run_cache(history.CACHE_CODE, req)
    assert result['complete_source_files_sha256'] and result['retained_source_chunks_match_verified_full_input']
    history.guard(spec)
    assert (route / '.ingest/batches.json').read_bytes() == before
    # Full cache corruption is caught even if outside the retained chunks.
    path = next((cached / 'data').rglob('*.parquet'))
    path.chmod(0o644)
    data = bytearray(path.read_bytes()); data[-1] ^= 1; path.write_bytes(data)
    with pytest.raises(ValueError, match='full SHA256'):
        run_cache(history.CACHE_CODE, req)


def test_source_change_and_failed_verification_are_rejected(daily_cfg):
    cfg = daily_cfg
    bag(cfg, 0, 1)
    with context(cfg) as q:
        ready(cfg, q)
    root = Path(cfg['routes'][0]['output'])
    spec = history.freeze(root)
    info = Path(spec['source']) / 'meta/info.json'
    info.write_text(info.read_text() + '\n')
    with pytest.raises(ValueError, match='changed'):
        history.guard(spec)
    report = Path(spec['source']) / 'conversion/verification.json'
    value = json.loads(report.read_text()); value['status'] = 'failed'; report.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='verification'):
        history.freeze(root)
    catalogue = root / '.ingest/batches.json'
    value = json.loads(catalogue.read_text()); value['batches'][0]['name'] = '../../outside'
    catalogue.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='identity'):
        history.freeze(root)


def test_remote_video_seed_copies_only_identical_videos_and_does_not_link_input(tmp_path):
    root = tmp_path / 'workspace/data'
    cache = root / 'sharpa/A/1_1_data_lerobot/videos/cam0'
    cache.mkdir(parents=True)
    source = cache / 'episode.mp4'; source.write_bytes(b'original-video')
    stage = root / 'sharpa/A/.uploading/task'; stage.mkdir(parents=True)
    before = source.stat()
    import hashlib
    files = [{'path': 'normal/videos/cam0/episode.mp4', 'size': source.stat().st_size,
              'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}]
    report = run_cache(history.SEED_CODE, {'root': str(root), 'robot': 'sharpa', 'group': 'A',
                       'name': '1_1_data_lerobot', 'staging': str(stage), 'files': files})
    target = stage / files[0]['path']
    assert report['seeded_video_files'] == 1 and target.read_bytes() == source.read_bytes()
    assert target.stat().st_ino != source.stat().st_ino
    assert source.stat().st_mtime_ns == before.st_mtime_ns
    bad = [{**files[0], 'sha256': 'a' * 64}]
    assert run_cache(history.SEED_CODE, {'root': str(root), 'robot': 'sharpa', 'group': 'A',
                'name': '1_1_data_lerobot', 'staging': str(stage), 'files': bad})['seeded_video_files'] == 0


def test_repeated_historical_run_preserves_complete_upload_manifest(daily_cfg, tmp_path, monkeypatch):
    cfg = daily_cfg
    bag(cfg, 0, 1)
    with context(cfg) as q:
        ready(cfg, q)
    spec = history.freeze(Path(cfg['routes'][0]['output']))
    target = tmp_path / 'workspace/data'
    for area in ('meta', 'data', 'videos'):
        shutil.copytree(Path(spec['source']) / area, target / 'sharpa/A' / spec['name'] / area)
    setup = {'workers': 1, 'cmax': 3, 'training_target': {'dataset_root': str(target)}}
    monkeypatch.setattr(history, 'call', lambda _, code, request: run_cache(code, request))
    delivered = []
    def send(cfg, spec, pair, task, files, workers, status):
        delivered.append(files)
        return {'status': 'completed'}
    monkeypatch.setattr(history, 'deliver', send)
    output = tmp_path / 'derived'
    history.run_batch(setup, spec, output, 1, lambda *a, **kw: None)
    history.run_batch(setup, spec, output, 1, lambda *a, **kw: None)
    assert delivered[0] == delivered[1]


def test_parallel_transfer_failure_never_publishes_and_retry_commits_complete_pair(tmp_path, monkeypatch):
    from convest.capture.transfer import manifest
    from convest.training.ssh_remote import run as remote_run
    root = tmp_path / 'workspace/data'; root.mkdir(parents=True)
    task = 'b' * 64
    pair = tmp_path / ('1_1_data_lerobot--batch-000001--' + task[:16]); pair.mkdir()
    for area in ('normal', 'speedup'):
        for number in range(4):
            path = pair / area / 'data' / f'{number}.parquet'
            path.parent.mkdir(exist_ok=True, parents=True); path.write_bytes(bytes([number]) * 100)
    (pair / 'pipeline.json').write_text('{"status":"passed"}')
    cfg = {'fingerprint': 'a' * 64, 'training_target': {'dataset_root': str(root),
           'host': 'test-host', 'python': 'python3', 'rsync': 'rsync'}}
    spec = {'group': 'A', 'name': '1_1_data_lerobot'}
    monkeypatch.setattr(history, 'remote', lambda _, request: remote_run(request))
    monkeypatch.setattr(history, 'call', lambda *a: {'seeded_video_files': 0, 'seeded_video_bytes': 0})
    failing = [True]
    def send(args, **kw):
        listing = Path(next(a.split('=', 1)[1] for a in args if a.startswith('--files-from=')))
        if failing[0] and listing.name == '0':
            raise subprocess.CalledProcessError(12, args)
        stage = Path(args[-1].split(':', 1)[1])
        for name in listing.read_text().splitlines():
            dst = stage / name; dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(pair / name, dst)
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr(history.subprocess, 'run', send)
    with pytest.raises(subprocess.CalledProcessError):
        history.deliver(cfg, spec, pair, task, manifest(pair), 4, lambda *a, **kw: None)
    target = root / 'sharpa/A' / pair.name
    assert not target.exists()
    failing[0] = False
    result = history.deliver(cfg, spec, pair, task, manifest(pair), 4, lambda *a, **kw: None)
    assert result['status'] == 'completed' and manifest(target) == manifest(pair)
