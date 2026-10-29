"""Fault-injected OSS transit protocol; never touch business data or real mounts."""
import copy
import json
from pathlib import Path
import shutil
import pytest
from convest.capture.transfer import manifest
from convest.config import digest
from convest.gui import config, oss_remote, oss_transfer


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(oss_remote, 'require_oss_mount', lambda root: None)
    monkeypatch.setattr(oss_remote, 'require_final_mount', lambda root: None)
    source = tmp_path / 'source'
    (source / 'meta').mkdir(parents=True)
    (source / 'meta/info.json').write_text('{"test":true}')
    (source / 'video.bin').write_bytes(b'payload' * 100)
    final = tmp_path / 'cpfs'
    transit = tmp_path / 'oss'
    destination = final / 'tomato_sharpa/normal/scene/A/1029_1_2_normal_lerobot'
    files = manifest(source)
    identity = 'integration'
    q = {'destination': str(destination), 'files': files,
         'token': digest({'destination': str(destination), 'files': files, 'identity': identity}),
         'final_root': str(final), 'transit_root': str(transit)}
    return source, q


def invoke(q, operation):
    return oss_remote.run({**q, 'operation': operation})


def uploaded(source, q):
    r = invoke(q, 'prepare')
    shutil.copytree(source, r['staging'], dirs_exist_ok=True)
    return Path(r['staging'])


def test_delivers_final_receipt_and_cleans_only_owned_batch(setup):
    source, q = setup
    foreign = Path(q['transit_root']) / 'unrelated/model'
    foreign.parent.mkdir(parents=True); foreign.write_text('keep')
    transit = uploaded(source, q)
    result = invoke(q, 'commit')
    assert result['transit_cleaned'] and not transit.exists()
    assert manifest(Path(q['destination'])) == q['files']
    receipt = json.loads(Path(result['receipt']).read_text())
    assert receipt['files'] == q['files']
    assert foreign.read_text() == 'keep'
    assert invoke(q, 'prepare') == result


def test_incomplete_or_corrupted_oss_never_publishes_or_deletes(setup):
    source, q = setup
    transit = uploaded(source, q)
    data = transit / 'video.bin'
    data.write_bytes(b'wrong')
    with pytest.raises(ValueError, match='SHA256'): invoke(q, 'commit')
    assert data.exists() and not Path(q['destination']).exists()
    shutil.copytree(source, transit, dirs_exist_ok=True)
    assert invoke(q, 'commit')['transit_cleaned']


def test_retry_after_final_publish_does_not_reupload_and_recovers_partial_cleanup(setup, monkeypatch):
    source, q = setup
    transit = uploaded(source, q)
    real_unlink = Path.unlink
    calls = []
    def interrupt(p, *args, **kwargs):
        if p.is_relative_to(transit):
            calls.append(str(p))
            if len(calls) == 2: raise OSError('simulated disconnect during cleanup')
        return real_unlink(p, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', interrupt)
    with pytest.raises(OSError, match='disconnect'): invoke(q, 'commit')
    assert Path(q['destination']).exists() and transit.exists()
    assert len(list(transit.rglob('*'))) > 0
    monkeypatch.setattr(Path, 'unlink', real_unlink)
    assert invoke(q, 'prepare')['transit_cleaned']
    assert not transit.exists()
    assert manifest(Path(q['destination'])) == q['files']


def test_conflicting_existing_final_or_unowned_transit_is_preserved(setup):
    source, q = setup
    transit = Path(q['transit_root']) / Path(q['destination']).relative_to(q['final_root'])
    transit.mkdir(parents=True); (transit / 'foreign').write_text('keep')
    with pytest.raises(ValueError, match='非本次'): invoke(q, 'prepare')
    assert (transit / 'foreign').read_text() == 'keep'
    shutil.rmtree(transit)
    uploaded(source, q)
    final = Path(q['destination']); final.mkdir(parents=True)
    (final / 'foreign').write_text('keep final')
    with pytest.raises(ValueError, match='冲突'): invoke(q, 'commit')
    assert transit.exists() and (final / 'foreign').exists()


def test_final_receipt_failure_retains_transit(setup, monkeypatch):
    from convest.gui import remote_agent
    source, q = setup
    transit = uploaded(source, q)
    real_atomic = remote_agent.atomic
    def failure(path, value):
        if path.name == q['token'] + '.json': raise OSError('receipt write failed')
        return real_atomic(path, value)
    monkeypatch.setattr(remote_agent, 'atomic', failure)
    with pytest.raises(OSError, match='receipt'): invoke(q, 'commit')
    assert transit.exists() and Path(q['destination']).exists()
    monkeypatch.setattr(remote_agent, 'atomic', real_atomic)
    assert invoke(q, 'prepare')['transit_cleaned']


def test_new_unknown_files_and_symlinks_prevent_cleanup(setup):
    source, q = setup
    transit = uploaded(source, q)
    (transit / 'foreign').write_text('keep')
    with pytest.raises(ValueError, match='冲突'): invoke(q, 'commit')
    (transit / 'foreign').unlink()
    (transit / 'extra-empty-dir').mkdir()
    with pytest.raises(ValueError, match='未知子目录'): invoke(q, 'commit')
    (transit / 'extra-empty-dir').rmdir()
    (transit / 'video.bin').unlink()
    (transit / 'video.bin').symlink_to(source / 'video.bin')
    with pytest.raises(ValueError, match='软链接'): invoke(q, 'commit')


def test_absent_mount_rejects_before_any_write(setup, monkeypatch):
    _, q = setup
    def absent(_): raise ValueError('mount absent')
    monkeypatch.setattr(oss_remote, 'require_oss_mount', absent)
    with pytest.raises(ValueError, match='mount absent'): invoke(q, 'prepare')
    assert not Path(q['final_root']).exists() and not Path(q['transit_root']).exists()


def test_legacy_already_published_data_not_sent_back_to_oss(setup):
    source, q = setup
    shutil.copytree(source, q['destination'])
    result = invoke(q, 'prepare')
    assert result['transit_cleaned']
    assert not Path(result['transit']).exists()


def test_client_retries_server_cleanup_without_resending(setup, monkeypatch):
    source, q = setup
    sent = []
    def call(target, request, **kwargs): return oss_remote.run(request, kwargs.get('progress', lambda **kw: None))
    def send(target, src, request, pending, total, progress):
        import io
        sent.append(pending)
        data = b''.join((Path(src) / name).read_bytes() for name in pending)
        oss_remote.receive({**request, 'paths': pending}, io.BytesIO(data))
    monkeypatch.setattr(oss_transfer, 'call', call)
    monkeypatch.setattr(oss_transfer, 'stream_files', send)
    delivery = {'mode': 'oss', 'transit_root': q['transit_root'], 'final_root': q['final_root']}
    phases = []
    for _ in range(2):
        r = oss_transfer.transfer({}, source, q['destination'], q['files'], 'integration',
                                  lambda **kw: phases.append(kw['phase']), delivery=delivery)
        assert r['transit_cleaned']
    assert len(sent) == 1 and 'oss_copy' in phases and 'oss_verify' in phases and 'oss_cleanup' in phases


def test_delivery_policy_upgrade_keeps_queue_fingerprints(tmp_path, monkeypatch):
    root_test = config.WORKSPACE / 'outputs' / ('gui-oss-test-' + __import__('uuid').uuid4().hex)
    monkeypatch.setattr(config, 'ROOT', root_test)
    s = config.load_settings(); t = s['tasks'][0]
    original = copy.deepcopy(s); original.pop('training_delivery')
    upgraded = config.validate(original)
    assert upgraded['training_delivery']['mode'] == 'oss'
    root, spec = config.runtime(upgraded, t)
    direct = copy.deepcopy(upgraded); direct['training_delivery']['mode'] = 'direct'
    old_root, old = config.runtime(direct, t)
    assert old_root == root
    assert spec['capture']['fingerprint'] == old['capture']['fingerprint']
    assert spec['training']['fingerprint'] == old['training']['fingerprint']
    shutil.rmtree(root_test)


def test_interrupted_stream_retains_completed_objects_and_resumes_only_missing(setup):
    import io
    source, q = setup
    prepared = invoke(q, 'prepare')
    names = prepared['pending']
    full_first = (source / names[0]).read_bytes()
    with pytest.raises(OSError, match='中断'):
        oss_remote.receive({**q, 'paths': names}, io.BytesIO(full_first + b'partial'))
    retry = invoke(q, 'prepare')
    assert names[0] not in retry['pending']
    assert names[1] in retry['pending']
    data = b''.join((source / name).read_bytes() for name in retry['pending'])
    oss_remote.receive({**q, 'paths': retry['pending']}, io.BytesIO(data))
    assert invoke(q, 'commit')['transit_cleaned']


def test_cpfs_mount_absent_preserves_everything(setup, monkeypatch):
    _, q = setup
    def absent(_): raise ValueError('CPFS absent')
    monkeypatch.setattr(oss_remote, 'require_final_mount', absent)
    with pytest.raises(ValueError, match='CPFS absent'): invoke(q, 'prepare')
    assert not Path(q['final_root']).exists()


def test_mapping_escape_and_overlapping_roots_rejected(setup):
    _, q = setup
    with pytest.raises(ValueError): invoke({**q, 'destination': str(Path(q['final_root']).parent / 'outside')}, 'prepare')
    with pytest.raises(ValueError, match='不能重叠'):
        invoke({**q, 'transit_root': q['final_root']}, 'prepare')


def test_ssh_progress_events_arrive_before_final_response():
    import sys
    from convest.gui.transport import progress_process
    events = []
    code = "import json,sys;json.load(sys.stdin);print(json.dumps({'event':'progress','phase':'oss_verify'}),flush=True);print(json.dumps({'status':'completed'}))"
    result = progress_process([sys.executable, '-c', code], {}, 5, lambda **kw: events.append(kw))
    assert events == [{'phase': 'oss_verify'}]
    assert result.returncode == 0 and json.loads(result.stdout)['status'] == 'completed'


def test_phase_failure_preserves_stage_and_never_reports_cleanup(setup, monkeypatch):
    source, q = setup
    uploaded(source, q)
    real_copy = oss_remote.shutil.copyfile
    phases = []
    def failure(*args): raise OSError('copy failure')
    monkeypatch.setattr(oss_remote.shutil, 'copyfile', failure)
    with pytest.raises(OSError, match='copy failure'):
        oss_remote.run({**q, 'operation': 'commit'}, lambda **kw: phases.append(kw['phase']))
    assert phases[-1] == 'oss_copy' and 'oss_cleanup' not in phases
    assert not Path(q['destination']).exists()
    monkeypatch.setattr(oss_remote.shutil, 'copyfile', real_copy)
    assert invoke(q, 'commit')['transit_cleaned']
