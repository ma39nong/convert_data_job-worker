"""Policy driven, resumable payload retention for current and historical GUI runs.

State, manifests and provenance are never removed. Network failures fail closed.
Offline scans take the same locks as workers; running upload roles call sweep()
directly while holding their upload lock.
"""
from contextlib import ExitStack
import fcntl
import json
from pathlib import Path
import re
import sqlite3
import time

from convest.config import digest
from convest.ingest.batches import durable_json
from convest.ingest.inputs import snapshot, require_same
from convest.capture.transfer import ensure_real_path, manifest, verify_tree
from convest.training import worker as training
from . import retention

DAY = 86400 * 10**9
DEFAULT = {'enabled': False, 'keep_days': 7}


def read(path):
    return json.loads(Path(path).read_text())


def policy(root):
    path = Path(root) / 'cleanup-policy.json'
    return validate(read(path)) if path.exists() else dict(DEFAULT)


def validate(value):
    if type(value.get('enabled')) is not bool or type(value.get('keep_days')) is not int or not 0 <= value['keep_days'] <= 3650:
        raise ValueError('清理策略需包含自动开关及 0–3650 天保留期')
    return {k: value[k] for k in DEFAULT}


def pair_path(spec, row):
    p = json.loads(row['payload']); source_spec = json.loads(row['spec'])
    if (source_spec['robot'] not in ('sharpa', 'wuji') or source_spec['group'] not in ('A', 'B')
            or (p['robot'], p['group']) != (source_spec['robot'], source_spec['group'])):
        raise ValueError('Invalid pair route')
    expected = Path(spec['training']['output_root']) / source_spec['robot'] / source_spec['group'] / training.pair_name(source_spec, row['id'])
    if Path(p['source']) != expected:
        raise ValueError('Training payload ownership mismatch')
    ensure_real_path(expected)
    owner = read(Path(spec['training']['output_root']) / 'owner.json')
    if owner != {'owner': 'capture-native-training-output-v1', 'fingerprint': spec['training']['fingerprint']}:
        raise ValueError('Training output ownership mismatch')
    return expected


def verify_remotes(spec, row, destination_name):
    """Recheck all remote bytes even when upstream payloads were released before."""
    cfg = spec['training']; p = json.loads(row['payload']); source_spec = json.loads(row['spec'])
    proof = read(pair_path(spec, row) / 'source.json')
    if proof['spec'] != source_spec:
        raise ValueError('Source proof mismatch')
    receipt = read(Path(cfg['state_root']) / 'receipts' / (row['id'] + '.json'))
    if receipt.get('status') != 'completed':
        raise ValueError('Missing training completion receipt')
    for variant in ('normal', 'speedup'):
        files = [{**f, 'path': f['path'][len(variant)+1:]} for f in p['files'] if f['path'].startswith(variant + '/')]
        if not files: raise ValueError('Empty training manifest')
        dest = Path(spec['task'][variant + '_root']) / p['group'] / destination_name(p, variant)
        if receipt['variants'][variant]['destination'] != str(dest): raise ValueError('Destination mismatch')
        retention.verify_backup(spec['training_server'], dest, files, digest([cfg['fingerprint'], row['id'], variant]))
        audit = Path(cfg['state_root']) / 'pair-reports' / row['id']
        dest = Path(spec['task'][variant + '_root']) / p['group'] / '.workflow-pairs' / p['name']
        if receipt['pair_reports'][variant]['destination'] != str(dest): raise ValueError('Pair destination mismatch')
        retention.verify_backup(spec['training_server'], dest, manifest(audit), digest([cfg['fingerprint'], row['id'], 'pair-report']))
    cap = spec['capture']
    with sqlite3.connect(Path(cap['state_root']).joinpath('queue.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        matches = [r for r in db.execute("SELECT * FROM uploads WHERE state='completed' AND dataset_done=1")
                   if json.loads(r['payload']).get('dataset') == source_spec['source']
                   and json.loads(r['payload']).get('route') == source_spec['route']]
    if len(matches) != 1: raise ValueError('Missing completed 202 upload')
    upstream = matches[0]; payload = json.loads(upstream['payload'])
    if payload['dataset_files'] != proof['files'] or payload['batch_id'] != source_spec['batch_id']:
        raise ValueError('202 manifest mismatch')
    dest = Path(spec['task']['lerobot_root']) / p['group'] / source_spec['name']
    receipt = read(Path(cap['state_root']) / 'receipts' / (upstream['id'] + '.json'))
    if receipt.get('status') != 'completed' or receipt.get('destination') != str(dest):
        raise ValueError('Missing 202 receipt')
    retention.verify_backup(spec['data_server'], dest, proof['files'], digest([cap['fingerprint'], upstream['id']]))


def release_pair(spec, db, row, destination_name):
    root = pair_path(spec, row); cfg = spec['training']; tid = row['id']
    state = Path(cfg['state_root']) / 'pair-retention' / (tid + '.json')
    old = read(state) if state.exists() else {}
    if old.get('status') == 'released': return old
    if row['state'] != 'uploaded': raise ValueError('Pair is not uploaded')
    # Full source must finish releasing first; its validation needs the intact pair.
    retention.release_one(spec, db, row, destination_name)
    full = read(Path(cfg['state_root']) / 'retention' / (tid + '.json'))
    if full.get('status') != 'released': raise ValueError('Complete LeRobot release still pending')
    verify_remotes(spec, row, destination_name)
    p = json.loads(row['payload'])
    if not old.get('trees'):
        require_same(root, p['snapshot']); verify_tree(root, p['files'])
        trees = []
        for variant in ('normal', 'speedup'):
            for name in ('data', 'videos'):
                folder = root / variant / name
                if folder.exists() or folder.is_symlink():
                    ensure_real_path(folder)
                    trees.append({'path': str(folder), 'snapshot': snapshot(folder), 'files': manifest(folder),
                                  'quarantine': str(folder.parent / ('.gui-release-' + tid + '-' + name))})
        old = {'task': tid, 'source': str(root), 'status': 'releasing', 'trees': trees,
               'verified_ns': time.time_ns()}
        durable_json(state, old)
    if old.get('task') != tid or old.get('source') != str(root): raise ValueError('Pair intent mismatch')
    if not retention.shutil.rmtree.avoids_symlink_attacks: raise ValueError('Safe deletion unavailable')
    for tree in old['trees']:
        folder = Path(tree['path']); quarantine = Path(tree['quarantine'])
        if (folder.parent not in (root / 'normal', root / 'speedup') or folder.name not in ('data', 'videos')
                or quarantine != folder.parent / ('.gui-release-' + tid + '-' + folder.name)):
            raise ValueError('Unsafe pair release path')
        ensure_real_path(folder); ensure_real_path(quarantine)
        if folder.exists():
            if quarantine.exists(): raise ValueError('Ambiguous quarantine')
            retention.check_tree(folder, tree)
            folder.rename(quarantine)
        if quarantine.exists():
            retention.check_tree(quarantine, tree, partial=True)
            retention.shutil.rmtree(quarantine)
    old.update(status='released', released_ns=time.time_ns())
    durable_json(state, old)
    return old


def sweep(spec, db, keep_days, apply=False):
    from .worker import training_destination_name
    result = []
    for row in db.execute('SELECT * FROM tasks ORDER BY updated_ns,id').fetchall():
        item = {'id': row['id'], 'status': 'retained'}
        try:
            if row['state'] != 'uploaded':
                item['reason'] = '未完成上传'; continue
            # Use completion time rather than directory age. Never refresh this on retry.
            receipt = read(Path(spec['training']['state_root']) / 'receipts' / (row['id'] + '.json'))
            completed = max(receipt['variants'][v]['completed_ns'] for v in ('normal', 'speedup'))
            if max(completed, row['updated_ns']) + keep_days * DAY > time.time_ns():
                item['reason'] = '仍在保留期'; continue
            root = pair_path(spec, row)
            released = Path(spec['training']['state_root']) / 'pair-retention' / (row['id'] + '.json')
            if released.exists() and read(released).get('status') == 'released':
                item['status'] = 'released'; continue
            # Preview deliberately does not contact remote servers or promise deletion.
            item.update(status='candidate', name=json.loads(row['spec'])['name'], source=str(root))
            if apply:
                release_pair(spec, db, row, training_destination_name)
                item['status'] = 'released'
        except Exception as exc:
            item.update(status='retained', reason=str(exc))
        finally:
            result.append(item)
    return result


def scan(root, keep_days, apply=False, progress=lambda value: None):
    """Includes deleted UI tasks and old config versions using their ORIGINAL endpoints."""
    root = Path(root); result = []
    for file in sorted((root / 'runs').glob('task-*/*/spec.json')):
        run = file.parent
        item = {'run': str(run), 'status': 'retained'}
        try:
            ensure_real_path(file)
            if not re.fullmatch(r'task-[a-f0-9]{12}', run.parent.name) or not re.fullmatch(r'[a-f0-9]{20}', run.name):
                raise ValueError('Invalid run identity')
            spec = read(file)
            if Path(spec['root']) != run: raise ValueError('Run path mismatch')
            from .manager import alive
            runtime = read(run / 'runtime.json') if (run / 'runtime.json').exists() else {}
            if alive(runtime, run) or any(alive(child, run) for child in runtime.get('children', {}).values()):
                raise BlockingIOError('Worker is running')
            # Only permit the GUI's exact owned locations, never imported arbitrary paths.
            for cfg, fields in ((spec['capture'], {'state_root': 'capture/state', 'work_root': 'capture/work', 'local_batch_root': 'capture/batches'}),
                                (spec['training'], {'state_root': 'training/state', 'output_root': 'training/datasets'})):
                for key, suffix in fields.items():
                    if Path(cfg[key]) != run / suffix: raise ValueError('Run ownership path mismatch')
            for route in spec['capture'].get('routes', []):
                if (route['robot'] not in ('sharpa', 'wuji') or route['group'] not in ('A', 'B')
                        or Path(route['output']) != run / 'capture/batches' / route['robot'] / route['group']):
                    raise ValueError('Run route ownership mismatch')
            if spec['training'].get('capture', spec['capture']) != spec['capture']:
                raise ValueError('Capture configuration mismatch')
            with ExitStack() as stack:
                for path in [run / 'supervisor.lock', *[run / role / 'state' / (kind + '.lock')
                             for role in ('capture', 'training') for kind in ('processing', 'upload')]]:
                    ensure_real_path(path)
                    lock = stack.enter_context(path.open('a'))
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                db_path = run / 'training/state/queue.sqlite3'
                ensure_real_path(db_path)
                db = stack.enter_context(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True))
                db.row_factory = sqlite3.Row
                item.update(status='checked', tasks=sweep(spec, db, keep_days, apply))
                if not item['tasks']:
                    item['reason'] = '暂无已登记训练批次；未封存或未转换数据保持保留'
        except BlockingIOError:
            item['reason'] = '任务运行或收尾中，跳过；运行任务由上传角色执行自动策略'
        except Exception as exc:
            item['reason'] = str(exc)
        result.append(item); progress(result)
    return result


def automatic(spec, db):
    root = Path(spec['root']).parents[2]
    cfg = policy(root)
    if not cfg['enabled']: return
    report = Path(spec['root']) / 'cleanup-auto.json'
    previous = read(report) if report.exists() else {}
    if previous.get('next_ns', 0) > time.time_ns(): return
    durable_json(report, {'status': 'running', 'next_ns': time.time_ns() + DAY})
    results = sweep(spec, db, cfg['keep_days'], apply=True)
    durable_json(report, {'status': 'completed', 'tasks': results, 'next_ns': time.time_ns() + DAY})
