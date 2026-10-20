#!/usr/bin/env python3
"""Isolated, sequential /data versus NVMe workspace trial, using the real receiver.

Run with the server's existing venv. Production code/configuration are never
replaced; the receiver drains between jobs and resumes only our own request.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

STAGES = ('input_check', 'cleaning', 'conversion', 'output_verification',
          'final_source_hash', 'archiving', 'publication', 'cleanup', 'other')


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)


def stage(phase):
    if phase.startswith('readiness.'):
        return 'input_check'
    if phase == 'job.trimming':
        return 'cleaning'
    if phase == 'job.converting':
        return 'conversion'
    if phase == 'job.output_verification':
        return 'output_verification'
    if phase == 'original_final_hash' or phase.startswith('original_final.'):
        return 'final_source_hash'
    return {'archiving': 'archiving', 'publishing': 'publication',
            'cleanup': 'cleanup'}.get(phase, 'other')


def durations(events, elapsed):
    result = dict.fromkeys(STAGES, 0.0)
    previous = 0.0
    label = 'other'
    for event in events:
        result[label] += max(0, event['elapsed_s'] - previous)
        previous, label = event['elapsed_s'], stage(event['phase'])
    result[label] += max(0, elapsed - previous)
    return {k: round(v, 6) for k, v in result.items()}


def hash_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def run_one(spec):
    # A fresh subprocess imports the cloned code. Path restrictions and cleanup
    # ownership remain exactly those of the production implementation.
    from convest.config import WORKSPACE, digest
    from convest.ingest.config import load_service_config
    import convest.ingest.worker as worker
    from convest.ingest.inputs import check_input, snapshot
    from convest.ingest.batches import BatchPublisher
    project = Path(spec['project']).resolve()
    if WORKSPACE != project:
        raise ValueError(f'Wrong code imported: {WORKSPACE}; expected {project}')
    cfg = load_service_config(spec['production_config'])
    route = next(r for r in cfg['routes'] if r['id'] == spec['route'])
    route = dict(route)
    source = Path(route['source']) / spec['episode']
    run_root = Path(spec['run_root']).resolve()
    route['output'] = str(run_root / 'published')
    route['conversion'] = {**route['conversion'], 'output_root': route['output']}
    cfg.update(state_root=str(run_root / 'state'),
               work_root=str(project / 'outputs' / spec['name']),
               routes=[route], copy_input=False,
               fingerprint=digest({'trial': spec['trial'], 'name': spec['name']}))
    events = []
    started = time.monotonic()
    report = {'name': spec['name'], 'variant': spec['variant'], 'project': str(project),
              'source': str(source), 'status': 'failed'}
    original_reporter = worker.progress_reporter

    def reporter(*args, **kwargs):
        underlying = original_reporter(*args, **kwargs)
        prefix = kwargs.get('prefix', '')

        def progress(phase, **detail):
            events.append({'phase': prefix + phase,
                           'elapsed_s': time.monotonic() - started, **detail})
            underlying(phase, **detail)
        return progress

    worker.progress_reporter = reporter
    try:
        with worker.worker_context(cfg) as queue:
            queue.put(route['id'], spec['episode'], None, 'checking')
            progress = reporter(cfg, route, spec['episode'], prefix='readiness.')
            progress('start')
            proof = check_input(source, route, snapshot(source), progress)
            if proof['content_identity'] != spec['expected_identity']:
                raise ValueError('Source differs from the previously published recording')
            queue.put(route['id'], spec['episode'], proof['content_identity'],
                      'queued', proof=proof)
            worker.process(cfg, queue, route, source, proof)
            outcome = queue.get(route['id'], spec['episode'])
            if outcome['state'] != 'published':
                raise ValueError(f"Trial not published: {outcome['state']}: {outcome['error']}")
        elapsed = time.monotonic() - started
        # Publisher.current() may reconcile metadata: do this outside timing.
        published = BatchPublisher(route, cfg['fingerprint']).current()
        archive = run_root / 'state/attempts' / digest([route['id'], spec['episode']]) / '0001'
        job = read(archive / 'jobs' / (spec['episode'] + '.json'))
        if not job.get('original_sha256_verified') or job['state'] != 'completed':
            raise ValueError('Final source verification missing')
        report.update(status='passed', total_s=elapsed,
                      stages_s=durations(events, elapsed), published=str(published),
                      archive=str(archive), source_identity=proof['content_identity'],
                      source_files=proof['files'],
                      source_hash_owner=job.get('source_hash_verification_owner'))
    finally:
        worker.progress_reporter = original_reporter
        report['events'] = events
        write(run_root / 'result.json', report)
    return report


def compare_results(results):
    import pyarrow.parquet as pq
    reference = results[0]
    root = Path(reference['published'])
    info = read(root / 'meta/info.json')
    mapping = read(Path(reference['archive']) / 'trim_report.json')['mapping']
    source_identity = reference['source_identity']
    expected_files = sorted(str(p.relative_to(root)) for folder in ('data', 'videos')
                            for p in (root / folder).rglob('*') if p.is_file())
    for result in results[1:]:
        other = Path(result['published'])
        other_info = read(other / 'meta/info.json')
        if any(other_info[key] != info[key] for key in ('features', 'total_frames', 'total_episodes')):
            raise ValueError('Output feature/frame/episode mismatch')
        if result['source_identity'] != source_identity:
            raise ValueError('Input identity changed between trial runs')
        if read(Path(result['archive']) / 'trim_report.json')['mapping'] != mapping:
            raise ValueError('Trim mapping mismatch')
        files = sorted(str(p.relative_to(other)) for folder in ('data', 'videos')
                       for p in (other / folder).rglob('*') if p.is_file())
        if files != expected_files:
            raise ValueError('Training artifact list mismatch')
        for relative in files:
            a, b = root / relative, other / relative
            if a.suffix == '.parquet':
                if not pq.read_table(a).equals(pq.read_table(b)):
                    raise ValueError('Parquet values differ: ' + relative)
            elif hash_file(a) != hash_file(b):
                raise ValueError('RGB video bytes differ: ' + relative)
    return {'status': 'passed', 'frames': info['total_frames'],
            'episodes': info['total_episodes'], 'artifacts_compared': len(expected_files),
            'checks': ['same original input identity', 'same trim mapping',
                       'exact Parquet values', 'exact RGB video SHA256',
                       'normal full source/video/depth/tactile/publication verification per run']}


def summarize(results):
    import statistics
    groups = {}
    for variant in ('data', 'nvme'):
        group = [r for r in results if r['variant'] == variant]
        groups[variant] = {'runs': len(group),
                           'mean_total_s': statistics.mean(r['total_s'] for r in group),
                           'mean_stages_s': {k: statistics.mean(r['stages_s'][k] for r in group)
                                             for k in STAGES}}
    savings = {k: groups['data']['mean_stages_s'][k] - groups['nvme']['mean_stages_s'][k]
               for k in STAGES}
    return {'variants': groups, 'stage_seconds_saved': savings,
            'total_seconds_saved': groups['data']['mean_total_s'] - groups['nvme']['mean_total_s'],
            'note': 'Positive saved seconds favor NVMe; cache and other server workloads are uncontrolled.'}


def code_signature(project):
    entries = {}
    for folder in ('src', 'configs', 'schemas'):
        for p in sorted((project / folder).rglob('*')):
            if p.is_symlink():
                raise ValueError('Code snapshot contains symlink: ' + str(p))
            if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc':
                entries[str(p.relative_to(project))] = hash_file(p)
    return entries


def clone_project(source, target, expected):
    target.mkdir(parents=True, exist_ok=False)
    for relative in expected:
        p = target / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, p)
        if hash_file(p) != expected[relative]:
            raise ValueError('Code changed while cloning: ' + relative)
    script = target / 'scripts/nvme_trial.py'
    script.parent.mkdir(exist_ok=True)
    shutil.copyfile(Path(__file__).resolve(), script)
    if code_signature(source) != expected:
        raise ValueError('Production code changed during snapshot')


def cli_command(project, config, command):
    subprocess.run([str(project / '.venv/bin/python'), '-m', 'convest.ingest.cli',
                    command, '--config', str(config)], cwd=project, check=True)


def pause_receiver(project, config, state, cancelled):
    service = subprocess.run(['systemctl', 'is-active', 'auto-clean-convert.service'],
                             text=True, capture_output=True)
    if service.stdout.strip() != 'active':
        raise ValueError('Receiver service is not active; inspect it before this trial')
    maintenance = state / 'maintenance.json'
    prior = read(maintenance) if maintenance.exists() else None
    lease = None
    if not prior or not prior.get('drain'):
        cli_command(project, config, 'drain')
        lease = read(maintenance)
    requested_ns = (lease or prior or {}).get('requested_ns', 0)
    try:
        print('Waiting for current production scan/job to finish; no new trial runs yet.', flush=True)
        tick = 0
        while True:
            status = read(state / 'status.json') if (state / 'status.json').exists() else {}
            with sqlite3.connect((state / 'queue.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
                active = db.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('checking','staging','processing')").fetchone()[0]
            if (status.get('maintenance') == 'drained' and active == 0
                    and status.get('updated_ns', 0) >= requested_ns):
                return lease
            if cancelled[0]:
                raise InterruptedError('Trial cancelled while waiting for maintenance')
            if tick % 6 == 0:
                live = subprocess.run(['systemctl', 'is-active', 'auto-clean-convert.service'],
                                      capture_output=True, text=True)
                if live.stdout.strip() != 'active':
                    raise RuntimeError('Receiver stopped before reaching the maintenance boundary')
                print('Waiting for drain; active jobs:', active, flush=True)
            tick += 1
            time.sleep(5)
    except BaseException:
        restore_receiver(project, config, state, lease)
        raise


def restore_receiver(project, config, state, lease):
    if lease is None:
        return 'previous_maintenance_preserved'
    path = state / 'maintenance.json'
    if not path.exists() or read(path) != lease:
        print('Maintenance changed externally; preserving it. Resume manually if appropriate.', flush=True)
        return 'maintenance_changed_externally'
    cli_command(project, config, 'resume')
    return 'resume_requested'


def default_nvme_candidates():
    import pwd
    username = pwd.getpwuid(os.getuid()).pw_name
    return [Path(base) / suffix for base in ('/data1', '/data2')
            for suffix in ('', username, 'auto-clean-convert-trials/' + username)]


def inspect_nvme(candidates, needed):
    import stat
    checks = []
    for candidate in candidates:
        root = Path(candidate).resolve()
        row = {'candidate': str(candidate), 'resolved': str(root),
               'required_GiB': round(needed / 2**30, 2), 'usable': False}
        checks.append(row)
        try:
            if not root.is_dir():
                row['reason'] = 'directory_missing_or_inaccessible'
                continue
            st = root.stat()
            row.update(owner_uid=st.st_uid, owner_gid=st.st_gid,
                       permissions=stat.filemode(st.st_mode),
                       writable=os.access(root, os.W_OK | os.X_OK))
            mount = subprocess.run(['findmnt', '-n', '-T', str(root), '-o', 'SOURCE,FSTYPE'],
                                   capture_output=True, text=True, check=True).stdout.strip()
            row['mount'] = mount
            row['free_GiB'] = round(shutil.disk_usage(root).free / 2**30, 2)
            if not mount.split() or not mount.split()[0].startswith('/dev/nvme'):
                row['reason'] = 'not_a_direct_NVMe_mount'
            elif shutil.disk_usage(root).free < needed:
                row['reason'] = 'insufficient_free_space'
            elif not row['writable']:
                row['reason'] = 'directory_not_writable_or_traversable'
            else:
                row.update(usable=True, reason='ready')
        except (OSError, subprocess.CalledProcessError) as exc:
            row['reason'] = str(exc)
    return checks


def choose_nvme(candidates, needed):
    checks = inspect_nvme(candidates, needed)
    for row in checks:
        if row['usable']:
            return Path(row['resolved']), row['mount']
    raise ValueError('No writable NVMe location with sufficient free space. '
                     'Preflight details: ' + json.dumps(checks, ensure_ascii=False))


def acquire_trial_lock(project):
    import fcntl
    path = project.parent / 'operations/.nvme-trial.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError('Another NVMe trial is already running')
    return lock


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, default=Path('/data/data-fruits/real/harvest-tomato/code/auto-clean-convert'))
    parser.add_argument('--robot', choices=['sharpa', 'wuji'], default='sharpa')
    parser.add_argument('--group', choices=['A', 'B'], default='A')
    parser.add_argument('--episode', default='episode62')
    parser.add_argument('--nvme-root', type=Path)
    parser.add_argument('--check-nvme', action='store_true',
                        help='Only report NVMe mount, permissions and capacity; never drain or run')
    parser.add_argument('--order', choices=['ab', 'abba'], default='abba', help='a=/data, b=NVMe; strictly sequential')
    parser.add_argument('--run', action='store_true', help='Without this flag, only check prerequisites')
    parser.add_argument('--child', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        run_one(read(args.child))
        return 0
    import re
    import yaml
    project = args.project.resolve()
    if not re.fullmatch(r'episode[0-9]+', args.episode):
        raise ValueError('Expected an exact episode name')
    if args.robot == 'sharpa' and args.group == 'A' and args.episode in ('episode53', 'episode54', 'episode55'):
        raise ValueError('These sources were explicitly excluded by the user')
    filesystem = subprocess.run(['findmnt', '-n', '-T', str(project), '-o', 'FSTYPE'],
                                capture_output=True, text=True, check=True).stdout.strip()
    if filesystem.startswith('fuse'):
        raise ValueError('Run on the server itself, not a workstation mount')
    config = project / 'configs/ingest.server.yaml'
    cfg = yaml.safe_load(config.read_text())
    def resolve(value):
        p = Path(value)
        return p.resolve() if p.is_absolute() else (project / p).resolve()
    state = resolve(cfg['state_root'])
    source = resolve(cfg['data_root']) / 'origin-rosbag' / args.robot / args.group / args.episode
    with sqlite3.connect((state / 'queue.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        previous = db.execute('SELECT state,identity FROM jobs WHERE route=? AND episode=?',
                              (f'{args.robot}-{args.group}', args.episode)).fetchone()
    if previous is None or previous[0] != 'published':
        raise ValueError('Choose an already published, unchanged source for this isolated trial')
    size = sum(p.stat().st_size for p in source.rglob('*') if p.is_file())
    if not size or not (source / 'collection_state.json').is_file():
        raise ValueError('Original source is unavailable')
    reserve = 10 * 10**9
    candidates = [args.nvme_root] if args.nvme_root else default_nvme_candidates()
    if args.check_nvme:
        print(json.dumps({'writes_performed': False, 'service_changed': False,
                          'checks': inspect_nvme(candidates, size * 3 + reserve)},
                         ensure_ascii=False, indent=2))
        return 0
    nvme, mount = choose_nvme(candidates, size * 3 + reserve)
    if shutil.disk_usage(project).free < size * (3 + 2 * len(args.order)) + reserve:
        raise ValueError('Insufficient /data free space for trial artifacts')
    if 'defer_source_final_hash' not in (project / 'src/convest/jobs.py').read_text():
        raise ValueError('Expected the verified single-final-hash receiver version')
    trial = 'nvme-' + datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    output = project.parent / 'experiments' / trial
    nvme_base = nvme / 'auto-clean-convert-trials' / trial
    print(json.dumps({'source': str(source), 'bytes': size, 'nvme_root': str(nvme),
                      'nvme_mount': mount, 'order': args.order,
                      'production_service': 'drain before trial; restore own request afterward',
                      'outputs': str(output), 'writes_performed': False}, ensure_ascii=False, indent=2), flush=True)
    if not args.run:
        return 0
    trial_lock = acquire_trial_lock(project)
    output.mkdir(parents=True, exist_ok=False)
    cancelled = [False]
    def stop(signum, frame):
        cancelled[0] = True
        print('Cancellation requested; waiting for the current trial bag to finish safely.', flush=True)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    lease = None
    results = []
    summary = {'trial': trial, 'status': 'failed', 'order': args.order, 'source': str(source),
               'nvme_root': str(nvme), 'production_outputs_untouched': True,
               'limits': ['Source input and final trial publication remain on /data.',
                          'Same source, same code, workers=1, same validation; no source copy.',
                          'ABBA reduces order bias, but caches and external workloads are not controlled.',
                          'Isolated outputs measure one-source publication, not a growing production batch.',
                          'Elapsed stages exclude maintenance wait and setup/output equality comparison.']}
    try:
        lease = pause_receiver(project, config, state, cancelled)
        if cancelled[0]:
            raise InterruptedError('Trial cancelled')
        signature = code_signature(project)
        paths = {'a': output / 'projects/data', 'b': nvme_base / 'project'}
        for target in paths.values():
            clone_project(project, target, signature)
        write(output / 'code_hashes.json', signature)
        for index, letter in enumerate(args.order, 1):
            if cancelled[0]:
                raise InterruptedError('Trial cancelled between runs')
            variant = 'data' if letter == 'a' else 'nvme'
            name = f'{index:02d}-{variant}'
            spec = {'trial': trial, 'name': name, 'variant': variant,
                    'project': str(paths[letter]), 'production_config': str(config),
                    'route': f'{args.robot}-{args.group}', 'episode': args.episode,
                    'expected_identity': previous[1], 'run_root': str(output / 'runs' / name)}
            spec_path = output / (name + '.json')
            write(spec_path, spec)
            env = {**os.environ, 'PYTHONPATH': str(paths[letter] / 'src'), 'PYTHONNOUSERSITE': '1'}
            command = [str(project / '.venv/bin/python'), str(paths[letter] / 'scripts/nvme_trial.py'), '--child', str(spec_path)]
            print('Starting sequential trial:', name, flush=True)
            with (output / (name + '.log')).open('w') as log:
                child = subprocess.Popen(command, cwd=paths[letter], env=env, stdout=log,
                                         stderr=subprocess.STDOUT, start_new_session=True)
                while child.poll() is None:
                    print('Trial running:', name, '; log:', str(output / (name + '.log')), flush=True)
                    time.sleep(30)
                if child.returncode:
                    raise RuntimeError(f'Trial {name} failed; see {output / (name + ".log")}')
            results.append(read(Path(spec['run_root']) / 'result.json'))
            print('Trial completed:', name, 'seconds:', round(results[-1]['total_s'], 2), flush=True)
        summary['equality'] = compare_results(results)
        summary.update(status='passed', timing=summarize(results))
    finally:
        # A retained maintenance request belonging to another operator is never
        # cleared. Test outputs stay isolated and raw inputs are never removed.
        summary['results'] = results
        try:
            summary['receiver_restore'] = restore_receiver(project, config, state, lease)
        finally:
            write(output / 'summary.json', summary)
            print('REPORT ' + str(output / 'summary.json'), flush=True)
            trial_lock.close()
    print(json.dumps({'status': summary['status'], 'equality': summary['equality'],
                      'timing': summary['timing'], 'receiver_restore': summary['receiver_restore']},
                     ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        raise SystemExit(2)
