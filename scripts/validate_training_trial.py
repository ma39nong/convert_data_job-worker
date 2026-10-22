#!/usr/bin/env python3
"""Bounded real-data acceptance using copies of already verified single-source jobs.

No recording, original capture queue, upstream service, or 24h clock is changed.
The trial's own batches are sealed with an injected timestamp, not a real 24h wait.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import yaml

from convest.config import WORKSPACE, digest
from convest.capture.config import load as load_capture
from convest.capture.transfer import manifest, verify_tree
from convest.ingest.batches import BatchPublisher, durable_json
from convest.ingest.inputs import snapshot, require_same
from convest.pipeline import load_records
from convest.training.config import load
from convest.training.worker import context, discover, process_once, upload_once, read


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--sharpa-job', required=True, type=Path)
    p.add_argument('--wuji-job', required=True, type=Path)
    p.add_argument('--upload', action='store_true', help='Upload both verified variants to configured training server')
    p.add_argument('--training-config', type=Path, default=WORKSPACE / 'configs/training.example.yaml')
    args = p.parse_args()
    base = WORKSPACE / 'outputs' / ('training-real-' + time.strftime('%Y%m%d-%H%M%S'))
    base.mkdir(parents=True)
    raw = yaml.safe_load((WORKSPACE / 'configs/capture.daily.yaml').read_text())
    raw.update(state_root=str(base / 'capture-state'), work_root=str(base / 'capture-work'),
               local_batch_root=str(base / 'batches'), delete_raw_after_upload=False)
    capture_path = base / 'capture.yaml'
    capture_path.write_text(yaml.safe_dump(raw))
    capture = load_capture(capture_path)
    original = {}
    for robot, job in [('sharpa', args.sharpa_job), ('wuji', args.wuji_job)]:
        dataset = job.resolve() / 'dataset'
        before = snapshot(dataset)
        files = manifest(dataset)
        report = read(dataset / 'conversion/verification.json')
        summary = read(job / 'summary.json')['jobs'][0]
        if (report.get('status') != 'passed' or not report.get('source_comparison')
                or not report.get('independent_alignment') or summary['state'] != 'completed'
                or not summary.get('original_sha256_verified')):
            raise ValueError('Real source job is not fully verified: ' + str(job))
        record = load_records(dataset)[0]
        episode = Path(record['source']).name
        scratch = base / 'copies' / robot
        shutil.copytree(dataset, scratch)
        verify_tree(scratch, files)
        audit = base / 'audits' / robot
        audit.mkdir(parents=True)
        shutil.copyfile(job / 'summary.json', audit / 'summary.json')
        shutil.copyfile(job / 'quality_report.json', audit / 'quality_report.json')
        proof = {'input': str(dataset), 'snapshot': before, 'files': files,
                 'recording_id': 'verified-real-' + robot + '-' + episode,
                 'content_identity': digest(files), 'acceptance_basis': 'copy of completed, source-verified real job'}
        route = next(r for r in capture['routes'] if r['robot'] == robot and r['group'] == 'A')
        pub = BatchPublisher(route, capture['fingerprint'])
        pub.publish(scratch, proof, episode, audit)
        deadline = pub.status()[0]['deadline_ns']
        pub.published(now_ns=deadline + 1)  # Only the trial's clock argument is advanced.
        assert pub.status()[0]['state'] == 'sealed'
        original[robot] = {'dataset': str(dataset), 'snapshot': before, 'files': files}
    raw_training = yaml.safe_load(args.training_config.read_text())
    raw_training.update(capture_project=str(WORKSPACE), capture_config=str(capture_path),
                        state_root=str(base / 'state'), output_root=str(base / 'datasets'))
    training_path = base / 'training.yaml'
    training_path.write_text(yaml.safe_dump(raw_training))
    cfg = load(training_path)
    # Exercise the actual CLI process, import path, config loader and locks.
    for _ in range(2):
        subprocess.run([sys.executable, '-m', 'convest.training.cli', 'once', '--config', str(training_path)], check=True)
    with context(cfg) as db:
        rows = db.execute('SELECT * FROM tasks').fetchall()
        if len(rows) != 2 or any(r['state'] != 'local_ready' for r in rows):
            raise ValueError('Real processing incomplete: ' + str([dict(r) for r in rows]))
    if args.upload:
        for _ in range(2):
            subprocess.run([sys.executable, '-m', 'convest.training.cli', 'upload-once', '--config', str(training_path)], check=True)
    results = []
    with context(cfg) as db:
        discover(cfg, db)
        assert not process_once(cfg, db), 'Completed source must not be processed twice'
        for row in db.execute('SELECT * FROM tasks'):
            if row['state'] != ('uploaded' if args.upload else 'local_ready'):
                raise ValueError('Real delivery incomplete: ' + str(dict(row)))
            spec, payload = json.loads(row['spec']), json.loads(row['payload'])
            pair = Path(payload['source'])
            normal = read(pair / 'normal/processing/normal.json')
            speed = read(pair / 'speedup/processing/summary.json')
            results.append({'robot': spec['robot'], 'episode': spec['name'], 'state': row['state'],
                            'local': str(pair), 'normal': normal['validation'], 'speedup': speed,
                            'remote_logical': str(Path(cfg['training_target']['dataset_root']) / payload['robot'] / payload['group'] / payload['name'])})
    for item in original.values():
        verify_tree(item['dataset'], item['files'])
        require_same(item['dataset'], item['snapshot'])
    report = {'status': 'passed', 'results': results, 'source_datasets_unchanged': True,
              'natural_24h_wait_tested': False, 'sealed_trial_batches': True,
              'actual_ssh_upload_and_server_sha256': args.upload,
              'training_started': False, 'existing_capture_service_changed': False,
              'config': str(training_path)}
    durable_json(base / 'acceptance.json', report)
    print('REPORT=' + str(base / 'acceptance.json'))


if __name__ == '__main__':
    main()
