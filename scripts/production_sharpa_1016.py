"""Bounded, resumable publishing for the explicitly selected 10.16 Sharpa batch.

Server inputs are read-only. Only an empty or explicitly owned destination is used.
Every published episode is independently audited locally, SHA-256 checked after
upload, and decoded/checked again on the destination before its commit journal.
"""
from pathlib import Path
import fcntl
import hashlib
import json
import os
import shutil
import sys
import time
from dataclasses import asdict
from bag_worker.idle import ALGORITHM_VERSION, default_config

from convest.config import WORKSPACE, atomic_json, load_config, digest
from convest.jobs import run, checksum
from convest.pipeline import load_records
from convest.formats import multimodal as writer
from convest.sources.gello_rosbag2 import snapshot
from convest.verify import verify, verify_episode

CONTROL = WORKSPACE / 'outputs/production_sharpa_1016'
OWNER = 'production-sharpa-1016-v1'


def read(path):
    return json.loads(Path(path).read_text())


def guarded_remove(path, cache):
    path = Path(path)
    if path.is_symlink() or not path.resolve().is_relative_to(cache.resolve()) or path == cache:
        raise ValueError(f'Unsafe cache cleanup: {path}')
    if path.exists():
        shutil.rmtree(path)


def upload_file(source, target):
    """Replace only task-owned output, then read back and compare all bytes."""
    source, target = Path(source), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError(f'Refuse output symlink: {target}')
    expected = checksum(source)
    temporary = target.with_name(target.name + '.upload.partial')
    if temporary.is_symlink():
        raise ValueError('Refuse upload symlink')
    shutil.copyfile(source, temporary)
    if temporary.stat().st_size != source.stat().st_size or checksum(temporary) != expected:
        raise ValueError(f'Upload checksum mismatch: {target}')
    temporary.replace(target)
    if checksum(target) != expected:
        raise ValueError(f'Published checksum mismatch: {target}')
    return {'bytes': target.stat().st_size, 'sha256': expected}


def stage_source(source, target):
    before = snapshot(source)
    target.mkdir(parents=True)
    hashes = {}
    for entry in sorted(source.iterdir()):
        if entry.is_symlink() or not entry.is_file():
            raise ValueError(f'Unexpected source entry: {entry.name}')
        h = hashlib.sha256()
        with entry.open('rb') as inp, (target / entry.name).open('xb') as out:
            while chunk := inp.read(8 * 1024 * 1024):
                h.update(chunk)
                out.write(chunk)
        shutil.copystat(entry, target / entry.name)
        hashes[entry.name] = h.hexdigest()
    if snapshot(source) != before:
        raise ValueError('Server input changed during staging')
    return {'source': str(source), 'snapshot': before, 'sha256': hashes}


def archive_audits(job, destination):
    for p in sorted(job.rglob('*.json')):
        relative = p.relative_to(job)
        if 'cleaned' in relative.parts or 'staging' in relative.parts:
            continue
        upload_file(p, destination / relative)
    for p in (job / 'cleaned').glob('*/trim_report.json'):
        upload_file(p, destination / 'trim_report.json')


def execute():
    inventory = read(CONTROL / 'inventory.json')
    source = Path(inventory['source_root']).resolve()
    reference = Path(inventory['reference_root']).resolve()
    target = Path(inventory['target_root']).resolve()
    cache = CONTROL / 'cache'
    if not source.is_dir() or not reference.is_dir() or not target.is_dir():
        raise ValueError('Mounted inputs and destination must exist')
    if not os.path.ismount('/home/user/company-remote-2'):
        raise ValueError('Server SSHFS is not mounted')
    if target.is_relative_to(source) or source.is_relative_to(target) or target == reference:
        raise ValueError('Unsafe input/output overlap')
    selected = inventory['episodes']
    identity = digest({'source': str(source), 'reference': str(reference), 'target': str(target), 'episodes': selected})
    marker = target / 'production/owner.json'
    if marker.exists():
        previous = read(marker)
        if previous.get('owner') != OWNER or previous.get('identity') != identity:
            raise ValueError('Destination ownership/selection mismatch')
    else:
        if any(target.iterdir()):
            raise ValueError('Unowned nonempty destination')
        atomic_json(marker, {'owner': OWNER, 'identity': identity, 'created_ns': time.time_ns()})
    cache.mkdir(exist_ok=True)
    cache_marker = cache / 'owner.json'
    if cache_marker.exists() and read(cache_marker) != {'owner': OWNER, 'identity': identity}:
        raise ValueError('Cache ownership mismatch')
    atomic_json(cache_marker, {'owner': OWNER, 'identity': identity})
    with (target / 'production/lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cfg = load_config(WORKSPACE / 'configs/sharpa.yaml')
        cfg.update(output_root=str(target), source_root=str(source), repo_id='fr3_sharpa/tomato_1016_cleaned', min_free_gb=50)
        manifest = target / 'conversion/manifest.json'
        cleaning_identity = digest({'config': cfg, 'idle': asdict(default_config()), 'algorithm_version': ALGORITHM_VERSION})
        if manifest.exists() and (read(manifest)['config'] != cfg or read(manifest).get('cleaning_identity') != cleaning_identity):
            raise ValueError('Publishing config or cleaning algorithm changed; use a new destination')
        atomic_json(manifest, {'owner': 'convest-data', 'config': cfg, 'publisher': OWNER, 'cleaning_identity': cleaning_identity, 'cleaning_algorithm_version': ALGORITHM_VERSION})
        upload_file(CONTROL / 'inventory.json', target / 'production/inventory.json')
        upload_file(CONTROL / 'episodes.txt', target / 'production/episodes.txt')
        upload_file(Path(__file__), target / 'production/publisher.py')
        upload_file(CONTROL / 'collection_state_scan.json', target / 'production/collection_state_scan.json') if (CONTROL / 'collection_state_scan.json').exists() else None
        records = load_records(target)
        committed = {r['production']['episode']: r for r in records}
        outcomes = read(CONTROL / 'status.json').get('outcomes', {}) if (CONTROL / 'status.json').exists() else {}
        def status(current=None, phase=None):
            report = {'state': 'running', 'selected': len(selected), 'published': len(records),
                      'current': current, 'phase': phase, 'updated_ns': time.time_ns(), 'outcomes': outcomes}
            atomic_json(CONTROL / 'status.json', report)
            atomic_json(target / 'production/status.json', report)
        writer.write_metadata(target, records, cfg)
        for ordinal, name in enumerate(selected, 1):
            slot = cache / name
            if name in committed:
                for relative, proof in committed[name]['production']['artifacts'].items():
                    if checksum(target / relative) != proof['sha256']:
                        raise ValueError(f'Committed server output changed: {relative}')
                outcomes[name] = {'state': 'published', 'episodes': [e['episode_index'] for e in committed[name]['episodes']]}
                guarded_remove(slot, cache)
                continue
            if outcomes.get(name, {}).get('state') in ('excluded', 'not_ready'):
                guarded_remove(slot, cache)
                continue
            guarded_remove(slot, cache)
            capture = read(source / name / 'collection_state.json')
            if capture.get('state') != 'finalized' or capture.get('finalized') is not True or capture.get('failures'):
                outcomes[name] = {'state': 'not_ready', 'error': 'Capture is not finalized and eligible', 'collection_state': capture.get('state')}
                atomic_json(target / f'production/jobs/{name}/collection_state.json', capture)
                status(name, 'not_ready')
                print(f'{name}: not_ready ({capture.get("state")}); original untouched', flush=True)
                continue
            slot.mkdir()
            print(f'[{ordinal}/{len(selected)}] {name}: staging original read-only', flush=True)
            status(name, 'staging')
            # Keep enough room for raw + cleaned + reindexed dataset and reserve.
            needed = inventory['original_sizes'][name] * 5 + 50 * 1024**3
            if shutil.disk_usage(cache).free < needed:
                raise OSError('Insufficient bounded staging reserve')
            proof = stage_source(source / name, slot / 'raw' / name)
            atomic_json(slot / 'source_proof.json', proof)
            local_cfg = {**cfg, 'source_root': str(slot / 'raw'), 'output_root': str(slot / 'unused')}
            job = slot / 'job'
            try:
                status(name, 'clean_convert_audit')
                run(local_cfg, job, episodes=[name])
                summary = read(job / 'summary.json')
                outcome = summary['jobs'][0]
                if outcome['state'] != 'completed':
                    state = 'excluded' if outcome['state'] == 'excluded' else 'not_ready' if not outcome.get('readiness') else 'failed'
                    archive_audits(job, target / 'production/jobs' / name)
                    atomic_json(target / 'production/jobs' / name / 'source_proof.json', proof)
                    outcomes[name] = {'state': state, 'error': outcome.get('error'), 'quality_rejection': outcome.get('quality_rejection')}
                    status(name, state)
                    guarded_remove(slot, cache)
                    continue
                for v in outcome['readiness']['volumes']:
                    if v['sha256'] != proof['sha256'][v['name']]:
                        raise ValueError('Staged input differs from server copy hash')
                if snapshot(source / name) != proof['snapshot']:
                    raise ValueError('Server source changed during processing')
                dataset = job / 'dataset'
                new_records = load_records(dataset)
                if len(new_records) != 1:
                    raise ValueError('Expected one source record per production job')
                record = new_records[0]
                detection = read(job / 'cleaned' / name / 'trim_report.json')['auto_idle_detection']
                effectiveness = detection.get('effectiveness_audit', {})
                if detection.get('version') != ALGORITHM_VERSION or effectiveness.get('status') != 'passed':
                    raise ValueError('Missing current independent cleaning effectiveness audit')
                record['cleaning_effectiveness'] = effectiveness
                if records and record['episodes'][0]['features'] != records[0]['episodes'][0]['features']:
                    raise ValueError('Sensor shape differs from published dataset')
                archive_audits(job, target / 'production/jobs' / name)
                total = sum(e['length'] for r in records for e in r['episodes'])
                index = sum(len(r['episodes']) for r in records)
                artifacts = {}
                status(name, 'upload_and_readback')
                for ep in record['episodes']:
                    writer.reindex_segment(dataset, ep, index, total)
                    ep['source_recording_id'] = str(source / name)
                    for relative in ep['paths']:
                        artifacts[relative] = upload_file(dataset / 'final' / relative, target / relative)
                    total += ep['length']; index += 1
                record['production'] = {'episode': name, 'reference_paths': inventory['reference_paths'][name],
                    'original': proof, 'artifacts': artifacts, 'cleaning_algorithm_version': ALGORITHM_VERSION, 'independent_source_audit': f'production/jobs/{name}/dataset/conversion/verification.json',
                    'temporary_source_note': 'source is the task-owned cleaned staging path; it is removed only after destination verification. Original source and full trim mapping are archived.',
                    'published_ns': time.time_ns()}
                atomic_json(target / 'production/jobs' / name / 'source_proof.json', proof)
                proposed = [*records, record]
                writer.write_metadata(target, proposed, cfg)
                info = read(target / 'meta/info.json')
                offset = sum(e['length'] for r in records for e in r['episodes'])
                for ep in record['episodes']:
                    result = verify_episode(target, info, ep, {'stats': ep['stats']}, record, cfg, None, offset, False, True)
                    atomic_json(target / f'production/jobs/{name}/published_episode_{ep["episode_index"]:06d}.json', result)
                    offset += ep['length']
                atomic_json(target / f'conversion/records/{len(records):06d}.json', record)
                records.append(record)
                committed[name] = record
                outcomes[name] = {'state': 'published', 'episodes': [e['episode_index'] for e in record['episodes']],
                                  'frames': sum(e['length'] for e in record['episodes']), 'deleted_seconds': outcome['deleted_seconds']}
                status(name, 'published')
                guarded_remove(slot, cache)
                print(f'{name}: published, verified; local raw/cleaned/converted cache removed', flush=True)
            except Exception as exc:
                # Abort on publisher/infrastructure errors; never erase unverified cache.
                outcomes[name] = {'state': 'failed', 'error': f'{type(exc).__name__}: {exc}'}
                status(name, 'failed')
                raise
        status(phase='final_dataset_verification')
        writer.write_metadata(target, records, cfg)
        verification = verify(target, full_video=True, compare_source=False, raise_on_error=False) if records else {'status': 'failed', 'error': 'No accepted episodes'}
        recovery = None
        if (CONTROL / 'assess_exceptions_authorized.json').exists():
            status(phase='separate_exception_assessment')
            from assess_sharpa_1016_exceptions import assess
            recovery = assess(inventory, cfg, CONTROL, stage_source, guarded_remove)
        result = {'state': 'completed' if verification['status'] == 'passed' else 'verification_failed',
                  'selected': len(selected), 'published': len(records), 'outcomes': outcomes,
                  'outcome_counts': {state: sum(o['state'] == state for o in outcomes.values()) for state in ('published', 'excluded', 'not_ready', 'failed')},
                  'verification': verification['status'], 'recovery_assessed': recovery['assessed'] if recovery else 0, 'finished_ns': time.time_ns(),
                  'local_cache_remaining': [p.name for p in cache.iterdir() if p.is_dir()]}
        atomic_json(CONTROL / 'status.json', result)
        atomic_json(target / 'production/status.json', result)
        print(json.dumps({k:v for k,v in result.items() if k != 'outcomes'}, ensure_ascii=False), flush=True)
        return 0 if verification['status'] == 'passed' else 2


if __name__ == '__main__':
    sys.exit(execute())
