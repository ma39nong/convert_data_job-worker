#!/usr/bin/env python3
"""Process one or two queued recordings sequentially on NVMe, then restore the receiver.

The existing ABBA coordinator is asked to finish its current run and exit.
The service exits gracefully before this one-shot consumer acquires its queue
lock. Only the temporary work root changes, without editing production config.
"""
import argparse
import datetime
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

import nvme_trial as trial

UNIT = 'auto-clean-convert.service'


def service_state():
    result = subprocess.run(['systemctl', 'show', UNIT, '-p', 'ActiveState',
                             '-p', 'MainPID', '-p', 'Result'], check=True,
                            capture_output=True, text=True)
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def select_next(cfg, db, now=None):
    """Choose from already observed stable queued inputs; do not invent readiness."""
    from convest.config import digest
    from convest.ingest.inputs import snapshot
    now = time.time_ns() if now is None else now
    db.row_factory = sqlite3.Row
    rows = db.execute("""SELECT j.*, o.signature, o.stable_count, o.stable_since_ns,
        o.last_scan_ns, o.priority FROM jobs j JOIN observations o
        ON j.route=o.route AND j.episode=o.episode
        WHERE j.state IN ('waiting_stable','waiting_upload','retry','queued')
        ORDER BY o.priority DESC,j.updated_ns,CAST(substr(j.episode,8) AS INTEGER),j.route""").fetchall()
    routes = {r['id']: r for r in cfg['routes']}
    for row in rows:
        row = dict(row)
        route = routes.get(row['route'])
        if not route or row['next_ns'] > now:
            continue
        if row['stable_count'] < cfg['stable_scans'] or now - row['stable_since_ns'] < cfg['stable_seconds'] * 10**9:
            continue
        if row['state'] == 'retry' and row['attempts'] >= cfg['max_attempts']:
            continue
        source = Path(route['source']) / row['episode']
        try:
            snap = snapshot(source)
        except (OSError, ValueError):
            continue
        if digest(snap) != row['signature']:
            continue
        return row, route, source, snap
    raise ValueError('No unchanged, observed stable queued source is eligible for this one-shot trial')


def run_next(spec):
    from convest.config import WORKSPACE
    import convest.ingest.worker as worker
    from convest.ingest.inputs import check_input, require_same
    cfg = spec['config']
    if WORKSPACE != Path(spec['project']).resolve():
        raise ValueError('One-shot imported code from the wrong workspace')
    if service_state().get('ActiveState') != 'inactive':
        raise ValueError('Production service must be stopped before opening its real queue')
    state = Path(cfg['state_root'])
    if trial.read(state / 'maintenance.json') != spec['lease']:
        raise ValueError('Maintenance ownership changed')
    if trial.read(state / 'owner.json')['fingerprint'] != cfg['fingerprint']:
        raise ValueError('Production state fingerprint mismatch')
    # State, source contracts, output routes, scheduling and fingerprint are the
    # actual production configuration. Only work_root differs in this process.
    cfg = {**cfg, 'work_root': str(WORKSPACE / 'outputs/one-shot-work')}
    result = {'status': 'failed', 'workspace': cfg['work_root'], 'official_queue': True}
    events = []
    real_reporter = worker.progress_reporter
    started = time.monotonic()

    def guard():
        if trial.read(state / 'maintenance.json') != spec['lease']:
            raise RuntimeError('Maintenance request changed during one-shot processing')

    def reporter(*args, **kwargs):
        underlying = real_reporter(*args, **kwargs)
        prefix = kwargs.get('prefix', '')
        def progress(phase, **detail):
            guard()
            events.append({'phase': prefix + phase, 'elapsed_s': time.monotonic() - started, **detail})
            underlying(phase, **detail)
        return progress

    worker.progress_reporter = reporter
    try:
        with worker.worker_context(cfg) as queue:
            row, route, source, snap = select_next(cfg, queue.db)
            result.update(route=route['id'], episode=row['episode'], source=str(source))
            trial.write(Path(spec['report_root']) / 'selected.json', result)
            print('Selected next queued source:', route['id'], row['episode'], flush=True)
            queue.put(route['id'], row['episode'], row['identity'], 'checking', error=None)
            progress = reporter(cfg, route, row['episode'], prefix='readiness.')
            progress('start')
            try:
                proof = check_input(source, route, snap, progress)
                guard()
                require_same(source, snap)
            except Exception as exc:
                queue.put(route['id'], row['episode'], None, 'waiting_upload',
                          error=f'{type(exc).__name__}: {exc}',
                          next_ns=time.time_ns() + cfg['retry_seconds'] * 10**9)
                raise
            proof['stability'] = {k: row[k] for k in
                                  ('route', 'episode', 'signature', 'stable_count',
                                   'stable_since_ns', 'last_scan_ns', 'priority')}
            proof['stability'].update(required_scans=cfg['stable_scans'], required_seconds=cfg['stable_seconds'])
            if row['identity'] != proof['content_identity']:
                queue.put(route['id'], row['episode'], proof['content_identity'], 'queued',
                          attempts=0, proof=proof, error=None)
            worker.process(cfg, queue, route, source, proof)
            final = queue.get(route['id'], row['episode'])
            result.update(queue_state=final['state'], error=final['error'])
            if final['state'] not in ('published', 'excluded'):
                raise RuntimeError(f"One-shot did not publish: {final['state']}: {final['error']}")
            result['status'] = 'passed' if final['state'] == 'published' else 'quality_excluded'
            if final['state'] == 'published':
                result['generation'] = json.loads(final['proof'])['generation']
    finally:
        worker.progress_reporter = real_reporter
        result.update(total_s=time.monotonic() - started, events=events)
        result['stages_s'] = trial.durations(events, result['total_s'])
        trial.write(Path(spec['report_root']) / 'result.json', result)
    return result


def legacy_coordinators(script):
    """Identify only our own exact old trial script, never the service/children."""
    result = []
    expected = str(script.resolve())
    for p in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            if p.parent.stat().st_uid != os.getuid():
                continue
            argv = p.read_bytes().decode().split('\0')
            if expected in argv and '--run' in argv and '--child' not in argv:
                fields = (p.parent / 'stat').read_text().rsplit(')', 1)[1].split()
                result.append((int(p.parent.name), fields[19]))
        except (OSError, ValueError, IndexError):
            continue
    if len(result) > 1:
        raise RuntimeError('Multiple old trial coordinators found; inspect before proceeding')
    return result


def still_same_process(pid, start):
    try:
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        return fields[19] == start and fields[0] != 'Z'
    except (OSError, IndexError):
        return False


def wait_for_current_processing_boundary(state, cancelled, expected_maintenance):
    """Do not stop a currently checking bag before it can enter processing.

    Once it is processing, the normal drain/SIGTERM path lets that whole bag
    finish. Failed input checks also end the current attempt naturally.
    """
    def active():
        with sqlite3.connect((state / 'queue.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
            return db.execute("SELECT route,episode,state FROM jobs WHERE state IN ('checking','processing','staging')").fetchall()
    rows = active()
    if len(rows) > 1:
        raise RuntimeError('Multiple active queue entries; inspect before switching workspace')
    if not rows or rows[0][2] != 'checking':
        return
    route, episode, _ = rows[0]
    print('Letting the current input check continue into normal processing:', route, episode, flush=True)
    tick = 0
    while True:
        if cancelled[0]:
            raise InterruptedError('Cancelled before requesting maintenance')
        current = trial.read(state / 'maintenance.json') if (state / 'maintenance.json').exists() else None
        if current != expected_maintenance:
            raise RuntimeError('Maintenance changed while waiting; preserving the external request')
        with sqlite3.connect((state / 'queue.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
            row = db.execute('SELECT state FROM jobs WHERE route=? AND episode=?', (route, episode)).fetchone()
        if not row or row[0] != 'checking':
            return
        if tick % 6 == 0:
            if service_state().get('ActiveState') != 'active':
                raise RuntimeError('Service stopped while waiting for the current check')
            print('Waiting for current bag input check:', route, episode, flush=True)
        tick += 1
        time.sleep(5)


def stop_service_gracefully(cancelled):
    state = service_state()
    pid = int(state.get('MainPID', '0'))
    if state['ActiveState'] != 'active' or not pid:
        raise RuntimeError('Expected active receiver before one-shot handoff')
    subprocess.run(['sudo', 'systemctl', 'kill', '--kill-who=main', '--signal=SIGTERM', UNIT], check=True)
    print('Waiting for receiver to exit normally; no forced termination.', flush=True)
    while service_state().get('MainPID') == str(pid):
        time.sleep(2)
    state = service_state()
    if state['ActiveState'] != 'inactive' or state.get('Result') != 'success':
        raise RuntimeError('Receiver did not exit successfully; do not take over queue')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', type=Path, default=Path('/data/data-fruits/real/harvest-tomato/code/auto-clean-convert'))
    p.add_argument('--nvme-root', type=Path, default=Path('/data1/auto-clean-convert-trials/user'))
    p.add_argument('--handoff', type=Path, required=False, help='Staged ownership information for the existing trial')
    p.add_argument('--child', type=Path, help=argparse.SUPPRESS)
    p.add_argument('--run', action='store_true')
    p.add_argument('--jobs', type=int, choices=[1, 2], default=1,
                   help='Process one or two queued bags sequentially on NVMe')
    args = p.parse_args(argv)
    if args.child:
        run_next(trial.read(args.child))
        return 0
    project = args.project.resolve()
    if subprocess.run(['findmnt', '-n', '-T', str(project), '-o', 'FSTYPE'], check=True,
                      capture_output=True, text=True).stdout.strip().startswith('fuse'):
        raise ValueError('Run on the server, not SSHFS')
    sys.path.insert(0, str(project / 'src'))
    from convest.ingest.config import load_service_config
    config_path = project / 'configs/ingest.server.yaml'
    cfg = load_service_config(config_path)
    if cfg.get('copy_input') is not False:
        raise ValueError('This one-shot requires the verified direct-input mode')
    state = Path(cfg['state_root'])
    with sqlite3.connect((state / 'queue.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        row, route, source, _ = select_next(cfg, db)
    needed = sum(f.stat().st_size for f in source.rglob('*') if f.is_file()) * 3 + 10 * 10**9
    nvme, mount = trial.choose_nvme([args.nvme_root], needed)
    if service_state().get('ActiveState') != 'active':
        raise ValueError('Expected active receiver; inspect status before proceeding')
    old_script = project.parent / 'operations/nvme-trial/nvme_trial.py'
    old = legacy_coordinators(old_script)
    prior = trial.read(state / 'maintenance.json') if (state / 'maintenance.json').exists() else None
    expected = trial.read(args.handoff)['expected_maintenance'] if args.handoff else None
    if prior and prior.get('drain') and prior != expected:
        raise ValueError('An unrelated maintenance request exists; preserved without modification')
    print(json.dumps({'next_candidate': route['id'] + '/' + row['episode'],
                      'nvme': str(nvme), 'mount': mount, 'old_trial_pids': [x[0] for x in old],
                      'action': f'finish current work; process {args.jobs} queued source(s) sequentially on NVMe and publish normally',
                      'production_config_changed': False, 'queued_jobs': args.jobs, 'parallel_jobs': 1}, ensure_ascii=False, indent=2), flush=True)
    if not args.run:
        return 0
    subprocess.run(['sudo', '-v'], check=True)
    cancelled = [False]
    def stop(signum, frame):
        cancelled[0] = True
        print('Cancellation requested; waiting for any current one-shot bag to finish safely.', flush=True)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    trial_id = 'nvme-next-' + datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    report_root = project.parent / 'experiments' / trial_id
    report_root.mkdir(parents=True, exist_ok=False)
    report = {'status': 'failed', 'trial': trial_id, 'runs': args.jobs, 'variant': 'nvme',
              'requested_jobs': args.jobs, 'results': [],
              'production_config_changed': False, 'official_queue_and_publication': True}
    lease = None
    lock = None
    legacy_done = not old
    stopped = False
    try:
        if not old:
            wait_for_current_processing_boundary(state, cancelled, prior)
        if cancelled[0]:
            raise InterruptedError('Cancelled before requesting maintenance')
        # Replace only the known earlier trial's request with our own lease. The
        # old coordinator will preserve it instead of resuming the receiver.
        trial.cli_command(project, config_path, 'drain')
        lease = trial.read(state / 'maintenance.json')
        for pid, start in old:
            if still_same_process(pid, start):
                os.kill(pid, signal.SIGINT)
            while still_same_process(pid, start):
                print('Waiting for old trial to finish its current run; remaining ABBA runs cancelled.', flush=True)
                time.sleep(10)
        legacy_done = True
        lock = trial.acquire_trial_lock(project)
        if cancelled[0]:
            raise InterruptedError('Cancelled before one-shot execution')
        if trial.read(state / 'maintenance.json') != lease:
            raise RuntimeError('Maintenance changed externally')
        stop_service_gracefully(cancelled)
        stopped = True
        if cancelled[0]:
            raise InterruptedError('Cancelled before one-shot execution')
        signature = trial.code_signature(project)
        target = nvme / 'auto-clean-convert-next' / trial_id / 'project'
        trial.clone_project(project, target, signature)
        shutil.copyfile(Path(__file__).resolve(), target / 'scripts/nvme_next.py')
        trial.write(report_root / 'code_hashes.json', signature)
        env = {**os.environ, 'PYTHONPATH': str(target / 'src'), 'PYTHONNOUSERSITE': '1'}
        for index in range(args.jobs):
            if cancelled[0]:
                report['status'] = 'cancelled_after_current_job'
                break
            run_root = report_root if args.jobs == 1 else report_root / 'runs' / f'{index+1:02d}'
            spec = {'project': str(target), 'config': cfg, 'lease': lease, 'report_root': str(run_root)}
            spec_path = run_root / 'spec.json'
            trial.write(spec_path, spec)
            command = [str(project / '.venv/bin/python'), '-u', str(target / 'scripts/nvme_next.py'), '--child', str(spec_path)]
            print(f'Starting NVMe queued bag {index+1}/{args.jobs}; no original-disk run.', flush=True)
            with (run_root / 'run.log').open('w') as log:
                child = subprocess.Popen(command, cwd=target, env=env, stdout=log,
                                         stderr=subprocess.STDOUT, start_new_session=True)
                while child.poll() is None:
                    print('Queued bag running on NVMe; log:', str(run_root / 'run.log'), flush=True)
                    time.sleep(15)
                if child.returncode:
                    raise RuntimeError('NVMe queued job failed; inspect ' + str(run_root / 'run.log'))
            outcome = trial.read(run_root / 'result.json')
            report['results'].append(outcome)
            report['status'] = 'running'
            report['processed_jobs'] = len(report['results'])
            report['published_jobs'] = sum(r['status'] == 'passed' for r in report['results'])
            trial.write(report_root / 'summary.json', report)
        if args.jobs == 1 and report['results']:
            report.update(report['results'][0])
        elif len(report['results']) == args.jobs:
            report['status'] = 'passed' if all(r['status'] == 'passed' for r in report['results']) else 'passed_with_quality_exclusions'
            report['total_processing_s'] = sum(r['total_s'] for r in report['results'])
            report['mean_processing_s'] = report['total_processing_s'] / len(report['results'])
    except BaseException as exc:
        report.update(status='cancelled' if isinstance(exc, InterruptedError) else 'failed',
                      error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        try:
            current = service_state()
            if legacy_done and lease is not None:
                if current['ActiveState'] == 'inactive' and current.get('Result') == 'success':
                    report['maintenance_restore'] = trial.restore_receiver(project, config_path, state, lease)
                    subprocess.run(['sudo', 'systemctl', 'start', UNIT], check=True)
                    report['service_restarted'] = True
                elif current['ActiveState'] == 'active' and not stopped:
                    report['maintenance_restore'] = trial.restore_receiver(project, config_path, state, lease)
                else:
                    report['recovery_required'] = current
        finally:
            if lock is not None:
                lock.close()
            trial.write(report_root / 'summary.json', report)
            print('REPORT ' + str(report_root / 'summary.json'), flush=True)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        raise SystemExit(2)
