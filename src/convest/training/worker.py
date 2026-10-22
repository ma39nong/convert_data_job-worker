"""Read sealed local batches; process and upload on independent persistent queues."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid

from convest.config import digest
from convest.ingest.batches import durable_json, flush_directory
from convest.ingest.inputs import snapshot, require_same, InputChanged
from convest.capture.transfer import (manifest, verify_tree, copy_tree, check_mount,
                                       ensure_real_path, TransferConflict)


def read(path):
    return json.loads(Path(path).read_text())


def recipe(cfg):
    root = Path(__file__).parent
    code = {str(p.relative_to(root)): digest(p.read_text()) for p in root.rglob('*.py')}
    return digest({'code': code, 'cmax': cfg['cmax'], 'workers': cfg['workers'],
                   'protocol': 'native-vectors-recorded-flags-v1'})


@contextmanager
def context(cfg, role='processing'):
    state = Path(cfg['state_root'])
    ensure_real_path(state)
    state.mkdir(parents=True, exist_ok=True)
    owner = {'owner': 'capture-native-training-v1', 'fingerprint': cfg['fingerprint']}
    # One immutable owner, initialized by either process under a shared init lock.
    with (state / 'init.lock').open('a') as init:
        fcntl.flock(init, fcntl.LOCK_EX)
        if (state / 'owner.json').exists():
            if read(state / 'owner.json') != owner:
                raise ValueError('Training queue configuration mismatch; use a new state/output directory')
        else:
            if any(p.name not in ('init.lock',) for p in state.iterdir()):
                raise ValueError('Refuse nonempty unowned training state')
            durable_json(state / 'owner.json', owner)
    with (state / (role + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = sqlite3.connect(state / 'queue.sqlite3', timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('''CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, spec TEXT NOT NULL, state TEXT NOT NULL,
                payload TEXT, attempts INTEGER DEFAULT 0, next_ns INTEGER DEFAULT 0,
                error TEXT, updated_ns INTEGER NOT NULL)''')
            db.commit()
            yield db
        finally:
            db.close()


def update(db, task, **values):
    values['updated_ns'] = time.time_ns()
    with db:
        db.execute('UPDATE tasks SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                   [*values.values(), task])


def sealed(cfg, spec):
    """Validate only upstream files; never invoke its publisher or mutate its state."""
    route = next(r for r in cfg['capture']['routes'] if r['id'] == spec['route'])
    root = Path(route['output'])
    ensure_real_path(root)
    owner = read(root / '.ingest/owner.json')
    if (owner.get('fingerprint') != cfg['capture']['fingerprint']
            or owner.get('route') != route['id'] or owner.get('layout') != 'timed-batches-v1'
            or owner.get('batch_hours') != 24):
        raise ValueError('Upstream batch ownership mismatch')
    catalogue = read(root / '.ingest/batches.json')
    matches = [b for b in catalogue['batches'] if b['id'] == spec['batch_id']]
    if len(matches) != 1:
        raise ValueError('Upstream batch missing/ambiguous')
    batch = matches[0]
    if batch.get('state') != 'sealed' or not batch.get('sealed_ns') or batch.get('seal_error'):
        raise ValueError('waiting_seal: upstream batch is not sealed')
    if not re.fullmatch(r'[0-9]+_[0-9]+_data_lerobot', batch.get('name', '')):
        raise ValueError('Invalid batch range name')
    generation = Path(batch['generation'])
    if not generation.is_absolute() or not generation.resolve().is_relative_to((root / '.ingest/streams').resolve()):
        raise ValueError('Generation escapes local batch store')
    if batch['generation'] != spec['source'] or batch['name'] != spec['name']:
        raise InputChanged('Sealed batch version changed')
    if (root / batch['name']).resolve(strict=True) != generation.resolve(strict=True):
        raise InputChanged('Sealed batch alias changed')
    ensure_real_path(generation)
    if read(generation / 'ingest/generation.json').get('state') != 'ready':
        raise ValueError('Sealed generation is not ready')
    info = read(generation / 'meta/info.json')
    seal = read(root / '.ingest/seals' / (batch['id'] + '.json'))
    for report in (batch.get('verification', {}), seal):
        if (report.get('status') != 'passed' or report.get('artifact_sha256') != 'passed'
                or report.get('full_video_decode') is not True or not report.get('checked_ns')
                or report.get('generation') != str(generation)
                or report.get('episodes') != info['total_episodes']
                or report.get('frames') != info['total_frames']):
            raise ValueError('Missing or mismatched upstream seal proof')
    if 'seal' in spec and seal != spec['seal']:
        raise InputChanged('Upstream seal changed')
    return seal


def discover(cfg, db):
    report = {}
    code = recipe(cfg)
    for route in cfg['capture']['routes']:
        catalog = Path(route['output']) / '.ingest/batches.json'
        if not catalog.exists():
            report[route['id']] = {'state': 'waiting_batch'}
            continue
        try:
            batches = read(catalog)['batches']
            route_report = {}
            for batch in batches:
                bid = batch['id']
                if not re.fullmatch(r'batch-[0-9]{6,}', bid):
                    raise ValueError('Invalid batch id')
                if batch.get('state') != 'sealed':
                    route_report[bid] = {'state': 'waiting_seal'}
                    continue
                spec = {'route': route['id'], 'robot': route['robot'], 'group': route['group'],
                        'batch_id': bid, 'name': batch['name'], 'source': batch['generation'],
                        'recipe': code}
                spec['seal'] = sealed(cfg, spec)
                task = digest([cfg['fingerprint'], spec])
                with db:
                    db.execute('INSERT OR IGNORE INTO tasks(id,spec,state,updated_ns) VALUES(?,?,?,?)',
                               (task, json.dumps(spec), 'pending', time.time_ns()))
                state = db.execute('SELECT state,error FROM tasks WHERE id=?', (task,)).fetchone()
                route_report[bid] = {'id': task, **dict(state)}
            report[route['id']] = route_report
        except Exception as exc:
            report[route['id']] = {'state': 'blocked', 'error': f'{type(exc).__name__}: {exc}'}
    durable_json(Path(cfg['state_root']) / 'discovery.json', {'updated_ns': time.time_ns(), 'routes': report})
    return report


def ensure_output(cfg):
    output = Path(cfg['output_root'])
    ensure_real_path(output)
    owner = {'owner': 'capture-native-training-output-v1', 'fingerprint': cfg['fingerprint']}
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'owner.json').exists():
        if read(output / 'owner.json') != owner:
            raise ValueError('Training output ownership mismatch')
    else:
        if any(output.iterdir()):
            raise ValueError('Refuse nonempty unowned training output')
        durable_json(output / 'owner.json', owner)
    return output


def pair_name(spec, task):
    return spec['name'] + '--' + spec['batch_id'] + '--' + task[:16]


def process_once(cfg, db):
    row = db.execute("SELECT * FROM tasks WHERE state IN ('pending','retry','processing','waiting_space') AND next_ns<=? ORDER BY updated_ns,id LIMIT 1",
                     (time.time_ns(),)).fetchone()
    if row is None:
        return False
    spec, task = json.loads(row['spec']), row['id']
    def progress(phase, **kw):
        durable_json(Path(cfg['state_root']) / 'progress.json',
                     {'task': task, 'route': spec['route'], 'phase': phase, 'updated_ns': time.time_ns(), **kw})
    try:
        if spec['recipe'] != recipe(cfg):
            raise TransferConflict('Recipe changed; old pending task is obsolete')
        sealed(cfg, spec)
        output = ensure_output(cfg)
        destination = output / spec['robot'] / spec['group'] / pair_name(spec, task)
        stage = output / '.staging' / task
        ensure_real_path(destination)
        ensure_real_path(stage)
        update(db, task, state='processing', error=None, attempts=row['attempts'] + 1)
        progress('checking_source')
        if destination.exists():
            complete = read(destination / 'pipeline.json')
            if complete['task'] != task:
                raise TransferConflict('Unrelated existing local output')
            verify_tree(destination, read(destination.parent / (destination.name + '.manifest.json')))
        else:
            stage.mkdir(parents=True, exist_ok=True)
            source = Path(spec['source'])
            source_snapshot = snapshot(source)
            proof = stage / 'source.json'
            if proof.exists():
                previous = read(proof)
                if previous['spec'] != spec:
                    raise TransferConflict('Staging belongs to a different sealed source')
                source_files = previous['files']
                verify_tree(source, source_files)
            else:
                source_files = manifest(source)
                durable_json(proof, {'spec': spec, 'files': source_files})
            # Full immutable artifact provenance is part of the original seal.
            from convest.pipeline import load_records
            from convest.jobs import checksum
            records = load_records(source)
            if not records:
                raise ValueError('Sealed dataset is missing provenance records')
            for record in records:
                for rel, expected in record['ingest']['artifacts'].items():
                    p = source / rel
                    if Path(rel).is_absolute() or '..' in Path(rel).parts:
                        raise ValueError('Invalid sealed artifact path')
                    if p.stat().st_size != expected['bytes'] or checksum(p) != expected['sha256']:
                        raise InputChanged('Sealed artifact hash mismatch: ' + rel)
            reserve = int(cfg['min_free_gib'] * 1024**3)
            videos = sum(p.stat().st_size for p in (source / 'videos').rglob('*.mp4'))
            if shutil.disk_usage(output).free < reserve + 2 * videos:
                raise OSError(28, 'Insufficient local reserve for normal/speedup')
            from .normal import export, validate_normal
            from .adatempo.lerobot_main import process
            from .adatempo.lerobot_checks import validate_output
            for name in ('normal', 'speedup'):
                dataset = stage / name
                marker = stage / (name + '.complete.json')
                ensure_real_path(dataset)
                ensure_real_path(marker)
                progress(name)
                if marker.exists():
                    verify_tree(dataset, read(marker)['files'])
                    continue
                if dataset.exists():
                    failed = output / '.failed' / (task + '-' + name + '-' + uuid.uuid4().hex)
                    failed.parent.mkdir(exist_ok=True)
                    dataset.rename(failed)
                dataset.mkdir()
                if name == 'normal':
                    report = export(source, dataset, spec['robot'])
                else:
                    report = process(stage / 'normal', dataset, workers=cfg['workers'], cmax=cfg['cmax'])
                durable_json(marker, {'validation': report, 'files': manifest(dataset)})
            progress('final_verification')
            validate_normal(source, stage / 'normal', spec['robot'])
            validate_output(stage / 'normal', stage / 'speedup')
            verify_tree(source, source_files)
            require_same(source, source_snapshot)
            sealed(cfg, spec)
            durable_json(stage / 'pipeline.json', {'status': 'passed', 'task': task, 'source': spec,
                         'created_ns': time.time_ns(), 'normal': 'native dimensions; retained values and RGB bytes unchanged',
                         'speedup': 'AdaTempo temporal resampling; original fps and resolution'})
            files = manifest(stage)
            for file in stage.rglob('*'):
                if file.is_file():
                    with file.open('rb') as stream:
                        os.fsync(stream.fileno())
            for folder in [p for p in stage.rglob('*') if p.is_dir()] + [stage]:
                flush_directory(folder)
            destination.parent.mkdir(parents=True, exist_ok=True)
            durable_json(destination.parent / (destination.name + '.manifest.json'), files)
            stage.rename(destination)
            flush_directory(destination.parent)
        files = read(destination.parent / (destination.name + '.manifest.json'))
        payload = {'source': str(destination), 'files': files, 'snapshot': snapshot(destination),
                   'robot': spec['robot'], 'group': spec['group'], 'name': destination.name}
        update(db, task, state='local_ready', payload=json.dumps(payload), next_ns=0, error=None)
        progress('local_ready', output=str(destination))
    except Exception as exc:
        conflict = isinstance(exc, (TransferConflict, InputChanged))
        state = 'conflict' if conflict else 'waiting_space' if getattr(exc, 'errno', None) == 28 else 'retry'
        update(db, task, state=state, error=f'{type(exc).__name__}: {exc}',
               next_ns=time.time_ns() + cfg['retry_seconds'] * 10**9)
        progress(state, error=str(exc))
    return True


def upload_once(cfg, db):
    row = db.execute("SELECT * FROM tasks WHERE state IN ('local_ready','upload_retry','uploading') AND next_ns<=? ORDER BY updated_ns,id LIMIT 1",
                     (time.time_ns(),)).fetchone()
    if row is None:
        return False
    payload, task = json.loads(row['payload']), row['id']
    target = cfg['training_target']
    remote = ({'require_mount': target['require_mount'],
               'remote_raw_root': target['dataset_root'], 'remote_dataset_root': target['dataset_root']}
              if target['transport'] == 'mount' else None)
    def guard():
        if remote is not None:
            check_mount(remote)
        require_same(payload['source'], payload['snapshot'])
    def progress(**kw):
        durable_json(Path(cfg['state_root']) / 'upload_progress.json',
                     {'task': task, 'route': payload['robot'] + '-' + payload['group'],
                      'updated_ns': time.time_ns(), **kw})
    try:
        guard()
        update(db, task, state='uploading', error=None)
        if target['transport'] == 'ssh':
            from .ssh_transfer import upload
            upload(target, payload, task, cfg['fingerprint'], guard, progress)
            update(db, task, state='uploaded', next_ns=0, error=None)
            return True
        destination = Path(target['dataset_root']) / payload['robot'] / payload['group'] / payload['name']
        copy_tree(payload['source'], destination, payload['files'], task, guard, progress)
        receipt = Path(target['dataset_root']) / '.training-transfer' / cfg['fingerprint'] / (task + '.json')
        ensure_real_path(receipt)
        content = {'status': 'completed', 'task': task, 'dataset': str(destination),
                   'normal': str(destination / 'normal'), 'speedup': str(destination / 'speedup'),
                   'files': payload['files'], 'completed_ns': time.time_ns()}
        durable_json(receipt, content)
        if read(receipt) != content:
            raise OSError('Training receipt read-back failed')
        update(db, task, state='uploaded', next_ns=0, error=None)
        progress(phase='uploaded', receipt=str(receipt))
    except Exception as exc:
        state = 'conflict' if isinstance(exc, (TransferConflict, InputChanged)) else 'upload_retry'
        update(db, task, state=state, error=f'{type(exc).__name__}: {exc}',
               next_ns=time.time_ns() + cfg['retry_seconds'] * 10**9)
        progress(phase=state, error=str(exc))
    return True
