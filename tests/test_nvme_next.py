"""One queued bag, real queue/publication, no duplicate processing after restart."""
import importlib.util
import json
from pathlib import Path
import sys
import time

import pytest
from convest.config import WORKSPACE, digest
from convest.ingest.inputs import snapshot, InputChanged
from convest.ingest.worker import worker_context, scan
from convest.ingest.control import request_maintenance
from convest.ingest.batches import BatchPublisher
from convest.verify import verify
from test_ingest import service, bag

sys.path.insert(0, str(WORKSPACE / 'scripts'))
try:
    module_spec = importlib.util.spec_from_file_location('nvme_next', WORKSPACE / 'scripts/nvme_next.py')
    nvme_next = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(nvme_next)
finally:
    sys.path.remove(str(WORKSPACE / 'scripts'))


def prepare(cfg):
    with worker_context(cfg) as queue:
        scan(cfg, queue, now_ns=time.time_ns() - 5_000_000_000)
        for row in queue.rows():
            route = next(r for r in cfg['routes'] if r['id'] == row['route'])
            source = Path(route['source']) / row['episode']
            queue.observe(row['route'], row['episode'], digest(snapshot(source)), time.time_ns(), 1_000_000_000)
    request_maintenance(cfg, True)
    return json.loads((Path(cfg['state_root']) / 'maintenance.json').read_text())


@pytest.mark.parametrize('route_index', [0, 3])
def test_single_next_publishes_real_queue_on_alternate_work_root_and_resume_deduplicates(service, monkeypatch, tmp_path, route_index):
    cfg = service
    cfg['copy_input'] = False
    source = bag(cfg, route_index, 3)
    before = {p.name: nvme_next.trial.hash_file(p) for p in source.iterdir()}
    lease = prepare(cfg)
    monkeypatch.setattr(nvme_next, 'service_state', lambda: {'ActiveState': 'inactive'})
    original_work = cfg['work_root']
    # run_next uses the actual workspace location in this unit test. The cloned
    # workspace subprocess path has its own end-to-end tests in test_nvme_trial.
    import convest.config as config
    import convest.jobs as jobs
    import convest.pipeline as pipeline
    monkeypatch.setattr(config, 'WORKSPACE', Path(original_work))
    monkeypatch.setattr(jobs, 'WORKSPACE', Path(original_work))
    monkeypatch.setattr(pipeline, 'WORKSPACE', Path(original_work))
    spec = {'project': original_work, 'config': cfg, 'lease': lease,
            'report_root': str(tmp_path / 'report')}
    result = nvme_next.run_next(spec)
    assert result['status'] == 'passed' and result['queue_state'] == 'published'
    assert result['episode'] == 'episode3'
    assert result['stages_s']['publication'] > 0
    assert cfg['work_root'] == original_work
    with worker_context(cfg) as queue:
        assert queue.get(cfg['routes'][route_index]['id'], 'episode3')['state'] == 'published'
    assert verify(BatchPublisher(cfg['routes'][route_index], cfg['fingerprint']).current())['episodes'] == 1
    assert {p.name: nvme_next.trial.hash_file(p) for p in source.iterdir()} == before
    request_maintenance(cfg, False)
    with worker_context(cfg) as queue:
        assert scan(cfg, queue)['counts'] == {'published': 1}
        assert verify(BatchPublisher(cfg['routes'][route_index], cfg['fingerprint']).current())['episodes'] == 1


def test_single_next_rejects_service_that_is_still_running_before_opening_queue(service, monkeypatch, tmp_path):
    cfg = service
    source = bag(cfg, 3, 3)
    lease = prepare(cfg)
    monkeypatch.setattr(nvme_next, 'service_state', lambda: {'ActiveState': 'active'})
    with pytest.raises(ValueError, match='must be stopped'):
        nvme_next.run_next({'project': str(WORKSPACE), 'config': cfg, 'lease': lease,
                            'report_root': str(tmp_path / 'report')})
    with worker_context(cfg) as queue:
        assert queue.get('wuji-B', source.name)['state'] == 'waiting_stable'


def test_single_next_final_source_failure_never_publishes(service, monkeypatch, tmp_path):
    import convest.ingest.worker as worker
    cfg = service
    cfg['copy_input'] = False
    bag(cfg, 3, 3)
    lease = prepare(cfg)
    monkeypatch.setattr(nvme_next, 'service_state', lambda: {'ActiveState': 'inactive'})
    import convest.config as config
    import convest.jobs as jobs
    import convest.pipeline as pipeline
    monkeypatch.setattr(config, 'WORKSPACE', Path(cfg['work_root']))
    monkeypatch.setattr(jobs, 'WORKSPACE', Path(cfg['work_root']))
    monkeypatch.setattr(pipeline, 'WORKSPACE', Path(cfg['work_root']))
    def fail(*a, **kw):
        raise InputChanged('Source changed during final hash')
    monkeypatch.setattr(worker, 'verify_input_content', fail)
    with pytest.raises(RuntimeError, match='did not publish'):
        nvme_next.run_next({'project': cfg['work_root'], 'config': cfg, 'lease': lease,
                            'report_root': str(tmp_path / 'report')})
    assert BatchPublisher(cfg['routes'][3], cfg['fingerprint']).current() is None
    with worker_context(cfg) as queue:
        assert queue.get('wuji-B', 'episode3')['state'] == 'waiting_stable'
    assert json.loads((tmp_path / 'report/result.json').read_text())['status'] == 'failed'


def test_select_next_uses_priority_and_skips_source_changed_since_observation(service):
    cfg = service
    source = bag(cfg, 3, 3)
    bag(cfg, 3, 11)
    prepare(cfg)
    with worker_context(cfg) as queue:
        row, _, _, _ = nvme_next.select_next(cfg, queue.db)
        assert row['episode'] == 'episode3'
        file = source / 'collection_state.json'
        file.write_text(file.read_text() + '\n')
        row, _, _, _ = nvme_next.select_next(cfg, queue.db)
        assert row['episode'] == 'episode11'


def test_cloned_subprocess_uses_production_fingerprint_and_publishes_exactly_one_pending_bag(service, tmp_path):
    import os
    import shutil
    import subprocess
    cfg = service
    cfg['copy_input'] = False
    bag(cfg, 3, 3)
    bag(cfg, 3, 11)
    bag(cfg, 3, 27)
    lease = prepare(cfg)
    project = tmp_path / 'nvme/project'
    nvme_next.trial.clone_project(WORKSPACE, project, nvme_next.trial.code_signature(WORKSPACE))
    shutil.copyfile(WORKSPACE / 'scripts/nvme_next.py', project / 'scripts/nvme_next.py')
    fakebin = tmp_path / 'bin'
    fakebin.mkdir()
    systemctl = fakebin / 'systemctl'
    systemctl.write_text("#!/bin/sh\nprintf '%s\\n' 'ActiveState=inactive' 'MainPID=0' 'Result=success'\n")
    systemctl.chmod(0o755)
    report = tmp_path / 'report'
    spec_path = tmp_path / 'child.json'
    nvme_next.trial.write(spec_path, {'project': str(project), 'config': cfg,
                                     'lease': lease, 'report_root': str(report)})
    env = {**os.environ, 'PYTHONPATH': str(project / 'src'), 'PYTHONNOUSERSITE': '1',
           'PATH': str(fakebin) + os.pathsep + os.environ['PATH']}
    child = subprocess.run([sys.executable, str(project / 'scripts/nvme_next.py'), '--child', str(spec_path)],
                           env=env, cwd=project, text=True, capture_output=True)
    assert child.returncode == 0, child.stdout + child.stderr
    result = nvme_next.trial.read(report / 'result.json')
    assert result['status'] == 'passed' and result['episode'] == 'episode3'
    with worker_context(cfg) as queue:
        assert queue.get('wuji-B', 'episode3')['state'] == 'published'
        assert queue.get('wuji-B', 'episode11')['state'] == 'waiting_stable'
    assert verify(BatchPublisher(cfg['routes'][3], cfg['fingerprint']).current())['episodes'] == 1
    # A second sequential child keeps the service stopped and shares the real
    # queue/output, selecting a different pending bag without replaying the first.
    second_report = tmp_path / 'report-two'
    nvme_next.trial.write(spec_path, {'project': str(project), 'config': cfg,
                                     'lease': lease, 'report_root': str(second_report)})
    child = subprocess.run([sys.executable, str(project / 'scripts/nvme_next.py'), '--child', str(spec_path)],
                           env=env, cwd=project, text=True, capture_output=True)
    assert child.returncode == 0, child.stdout + child.stderr
    assert nvme_next.trial.read(second_report / 'result.json')['episode'] == 'episode11'
    with worker_context(cfg) as queue:
        assert queue.get('wuji-B', 'episode3')['state'] == 'published'
        assert queue.get('wuji-B', 'episode11')['state'] == 'published'
        assert queue.get('wuji-B', 'episode27')['state'] == 'waiting_stable'
    assert verify(BatchPublisher(cfg['routes'][3], cfg['fingerprint']).current())['episodes'] == 2
    request_maintenance(cfg, False)
    with worker_context(cfg) as queue:
        # Only observe/reconcile the completed source, without starting another.
        request_maintenance(cfg, True)
        assert scan(cfg, queue)['counts'] == {'published': 2, 'waiting_stable': 1}


def test_wait_keeps_current_input_check_alive_until_processing_before_drain(service, monkeypatch):
    cfg = service
    source = bag(cfg, 3, 20)
    prepare(cfg)
    request_maintenance(cfg, False)
    state = Path(cfg['state_root'])
    prior = nvme_next.trial.read(state / 'maintenance.json')
    monkeypatch.setattr(nvme_next, 'service_state', lambda: {'ActiveState': 'active'})
    with worker_context(cfg) as queue:
        queue.put('wuji-B', source.name, None, 'checking')
        def enter_processing(seconds):
            assert nvme_next.trial.read(state / 'maintenance.json') == prior
            queue.put('wuji-B', source.name, None, 'processing')
        monkeypatch.setattr(nvme_next.time, 'sleep', enter_processing)
        nvme_next.wait_for_current_processing_boundary(state, [False], prior)
        assert queue.get('wuji-B', source.name)['state'] == 'processing'
        assert nvme_next.trial.read(state / 'maintenance.json') == prior


def test_wait_never_overwrites_new_external_maintenance(service):
    cfg = service
    source = bag(cfg, 3, 20)
    prepare(cfg)
    request_maintenance(cfg, False)
    state = Path(cfg['state_root'])
    prior = nvme_next.trial.read(state / 'maintenance.json')
    with worker_context(cfg) as queue:
        queue.put('wuji-B', source.name, None, 'checking')
        request_maintenance(cfg, True)
        external = nvme_next.trial.read(state / 'maintenance.json')
        with pytest.raises(RuntimeError, match='Maintenance changed'):
            nvme_next.wait_for_current_processing_boundary(state, [False], prior)
        assert nvme_next.trial.read(state / 'maintenance.json') == external
        assert queue.get('wuji-B', source.name)['state'] == 'checking'
