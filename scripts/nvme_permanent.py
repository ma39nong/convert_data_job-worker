#!/usr/bin/env python3
"""Persistently bind the existing worker temporary directory to NVMe.

Run as the ordinary worker user on the server. sudo is used only for systemd
unit installation and mount/service management. Production Python/config/state
paths are unchanged. --check is read-only; --rollback restores /data work.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import nvme_next as handoff
import nvme_trial as trial

UNIT = handoff.UNIT
TAG = '# Managed by auto-clean-convert nvme-permanent v1\n'


def command(*args):
    return subprocess.run(list(args), check=True, capture_output=True, text=True).stdout.strip()


def units(source, target):
    for path in (source, target):
        if any(c in str(path) for c in '\n\r\t %\\"'):
            raise ValueError('Unsupported systemd path characters')
    name = command('systemd-escape', '--path', '--suffix=mount', str(target))
    mount = TAG + ('[Unit]\nDescription=Auto clean convert NVMe temporary work\n'
                   f'RequiresMountsFor={source}\n\n[Mount]\nWhat={source}\n'
                   f'Where={target}\nType=none\nOptions=bind\n')
    dropin = TAG + (f'[Unit]\nRequires={name}\nAfter={name}\n\n[Service]\n'
                    f'ExecStartPre=/usr/bin/mountpoint -q {target}\n'
                    f'ExecStartPre=/usr/bin/test {source} -ef {target}\n')
    return {Path('/etc/systemd/system') / name: mount,
            Path('/etc/systemd/system') / (UNIT + '.d') / '40-nvme-work.conf': dropin}


def check_units(contents, required=False):
    for path, expected in contents.items():
        if path.is_symlink():
            raise ValueError('Refusing symlink unit file: ' + str(path))
        if path.exists() and path.read_text() != expected:
            raise ValueError('Existing unit differs; preserved: ' + str(path))
        if required and not path.exists():
            raise ValueError('Installed NVMe unit is missing: ' + str(path))


def marker_only(path, expected, allow_empty=False):
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise ValueError('Expected worker-owned real directory: ' + str(path))
    names = {p.name for p in path.iterdir()}
    if allow_empty and not names:
        return
    if names != {'owner.json'} or (path / 'owner.json').is_symlink():
        raise ValueError('Work directory is not empty after cleanup; preserved: ' + str(path))
    if trial.read(path / 'owner.json') != expected:
        raise ValueError('Work directory ownership marker differs: ' + str(path))


def bound(source, target):
    return source.exists() and os.path.ismount(target) and os.path.samefile(source, target)


def switch_idle(source, target, expected, contents, report_dir, rollback=False):
    """Caller owns queue lock and has confirmed successful service exit."""
    check_units(contents, required=rollback)
    if rollback:
        if not bound(source, target):
            raise ValueError('Expected our exact bind mount before rollback')
        marker_only(source, expected)
        command('sudo', 'systemctl', 'stop', next(iter(contents)).name)
        if os.path.ismount(target):
            raise RuntimeError('Mount still present; unit files preserved')
        # Hidden original /data directory must still have its original marker.
        marker_only(target, expected)
        for path in contents:
            command('sudo', 'rm', '--', str(path))
        command('sudo', 'systemctl', 'daemon-reload')
        return
    if os.path.ismount(target):
        raise ValueError('Work directory already mounted; use --check')
    marker_only(target, expected)
    if not source.exists():
        source.mkdir(mode=0o700)
    marker_only(source, expected, allow_empty=True)
    if not (source / 'owner.json').exists():
        trial.write(source / 'owner.json', expected)
    report_dir.mkdir(parents=True, exist_ok=True)
    for path, content in contents.items():
        staged = report_dir / path.name
        staged.write_text(content)
    # Validate rendered units before writing /etc. The drop-in is checked by
    # systemd on daemon-reload/start; systemd-analyze accepts the mount fragment.
    command('systemd-analyze', 'verify', str(report_dir / next(iter(contents)).name))
    for path, content in contents.items():
        command('sudo', 'install', '-d', '-m', '0755', str(path.parent))
        command('sudo', 'install', '-m', '0644', str(report_dir / path.name), str(path))
    command('sudo', 'systemctl', 'daemon-reload')
    command('sudo', 'systemctl', 'start', next(iter(contents)).name)
    if not bound(source, target):
        raise RuntimeError('Bind mount verification failed; service remains drained')
    if target.stat().st_dev != source.stat().st_dev:
        raise RuntimeError('Work device differs from NVMe')
    marker_only(target, expected)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project', type=Path, default=Path('/data/data-fruits/real/harvest-tomato/code/auto-clean-convert'))
    p.add_argument('--nvme-root', type=Path, default=Path('/data1/auto-clean-convert-trials/user'))
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument('--check', action='store_true')
    action.add_argument('--apply', action='store_true')
    action.add_argument('--rollback', action='store_true')
    args = p.parse_args(argv)
    if os.getuid() == 0:
        raise ValueError('Run as the worker user; do not sudo this entire script')
    project = args.project.resolve()
    if command('findmnt', '-n', '-T', str(project), '-o', 'FSTYPE').startswith('fuse'):
        raise ValueError('Run in the server terminal, not through SSHFS')
    sys.path.insert(0, str(project / 'src'))
    from convest.ingest.config import load_service_config
    config_path = project / 'configs/ingest.server.yaml'
    cfg = load_service_config(config_path)
    state, target = Path(cfg['state_root']), Path(cfg['work_root'])
    nvme, mount = trial.choose_nvme([args.nvme_root], 30 * 2**30 if not args.rollback else 0)
    if nvme.stat().st_uid != os.getuid():
        raise ValueError('NVMe root must be owned by this worker user')
    source = nvme / 'production-work'
    contents = units(source, target)
    expected = trial.read(state / 'owner.json')
    expected = {**expected, 'kind': 'work'}
    check_units(contents, required=args.rollback)
    info = {'operation': 'rollback' if args.rollback else 'apply', 'source': str(source),
            'target': str(target), 'nvme_mount': mount, 'bound_to_nvme': bound(source, target),
            'parallel_jobs': 1, 'config_changed': False, 'quality_checks_changed': False,
            'service': handoff.service_state(), 'units': {str(k): v for k, v in contents.items()}}
    print(json.dumps(info, ensure_ascii=False, indent=2), flush=True)
    if args.check:
        return 0
    if not args.rollback and bound(source, target):
        check_units(contents, required=True)
        print('Already configured on NVMe; no changes.', flush=True)
        return 0
    prior = trial.read(state / 'maintenance.json') if (state / 'maintenance.json').exists() else None
    if prior and prior.get('drain'):
        raise ValueError('Existing maintenance preserved; finish the earlier trial first')
    command('sudo', '-v')
    cancelled = [False]
    def stop(signum, frame):
        cancelled[0] = True
        print('Cancellation requested; waiting for a safe boundary.', flush=True)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    # The same lock prevents switching while a previous trial restores service.
    lock = trial.acquire_trial_lock(project)
    report_dir = project.parent / 'operations/nvme-permanent/reports' / str(time.time_ns())
    report = {**info, 'status': 'failed'}
    lease = None
    switched = False
    stopped = False
    try:
        if handoff.service_state().get('ActiveState') != 'active':
            raise ValueError('Expected active receiver; finish the earlier trial terminal first')
        handoff.wait_for_current_processing_boundary(state, cancelled, prior)
        if cancelled[0]:
            raise InterruptedError('Cancelled before drain')
        trial.cli_command(project, config_path, 'drain')
        lease = trial.read(state / 'maintenance.json')
        handoff.stop_service_gracefully(cancelled)
        stopped = True
        if cancelled[0]:
            raise InterruptedError('Cancelled before mount changes')
        if trial.read(state / 'maintenance.json') != lease:
            raise RuntimeError('External maintenance changed; preserved')
        with (state / 'worker.lock').open('a') as worker_lock:
            fcntl.flock(worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with sqlite3.connect((state / 'queue.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
                active = db.execute("SELECT count(*) FROM jobs WHERE state IN ('checking','processing','staging')").fetchone()[0]
            if active:
                raise RuntimeError('Queue still has active work; preserved without mounting')
            switch_idle(source, target, expected, contents, report_dir, args.rollback)
            switched = True
        restored = trial.restore_receiver(project, config_path, state, lease)
        report['maintenance_restore'] = restored
        if restored != 'resume_requested':
            raise RuntimeError('External maintenance changed; service left stopped')
        command('sudo', 'systemctl', 'start', UNIT)
        current = handoff.service_state()
        if current.get('ActiveState') != 'active':
            raise RuntimeError('Service did not stay active; inspect journal')
        report.update(status='passed', bound_to_nvme=bound(source, target), service=current)
        print('Rollback complete: work on /data.' if args.rollback else 'Permanent NVMe workspace enabled.', flush=True)
    except BaseException as exc:
        report['error'] = f'{type(exc).__name__}: {exc}'
        # Before any unit/mount edits, cancellation/preflight failure can safely
        # resume the unchanged service. Partial installation stays drained for
        # inspection rather than risking work on an unverified filesystem.
        changed_units = any(path.exists() for path in contents)
        if lease is not None and not switched and (not stopped or (not changed_units and not os.path.ismount(target))):
            restored = trial.restore_receiver(project, config_path, state, lease)
            report['maintenance_restore'] = restored
            if stopped and restored == 'resume_requested':
                command('sudo', 'systemctl', 'start', UNIT)
        report['recovery_required'] = stopped and handoff.service_state().get('ActiveState') != 'active'
        raise
    finally:
        lock.close()
        trial.write(report_dir / 'summary.json', report)
        print('REPORT ' + str(report_dir / 'summary.json'), flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        raise SystemExit(2)
