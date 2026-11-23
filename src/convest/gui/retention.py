"""Release owned sealed batch payloads only after all three remote copies verify.

Keep metadata, provenance, catalogue and seals so capture recovery and training
idempotency remain valid. Quarantine each data/videos tree before deleting it.
"""
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time

from convest.config import digest
from convest.ingest.batches import durable_json
from convest.ingest.inputs import snapshot, require_same
from convest.jobs import checksum
from convest.capture.transfer import ensure_real_path, manifest, verify_tree
from convest.training import worker as training
from .transport import verify_backup


def read(path):
    return json.loads(Path(path).read_text())


def check_tree(root, tree, partial=False):
    current = snapshot(root)
    known = {r['path']: r for r in tree['snapshot']}
    if not partial and {r['path'] for r in current} != set(known):
        raise ValueError('Release tree changed; retained')
    hashes = {f['path']: f['sha256'] for f in tree['files']}
    for row in current:
        previous = known.get(row['path'])
        if not previous or row['directory'] != previous['directory']:
            raise ValueError('Unexpected release entry; retained')
        # Removing another owned hardlink changes ctime, but never the inode,
        # file bytes or mtime. Hash remaining files before each deletion.
        keys = ('inode', 'device') if row['directory'] else ('inode', 'device', 'size', 'mtime_ns')
        if any(row[k] != previous[k] for k in keys):
            raise ValueError('Release entry replaced; retained')
        if not row['directory'] and checksum(Path(root) / row['path']) != hashes[row['path']]:
            raise ValueError('Release file contents changed; retained')


def release_one(spec, db, row, destination_name):
    cfg = spec['training']; task = spec['task']; tid = row['id']
    state = Path(cfg['state_root']) / 'retention' / (tid + '.json')
    old = read(state) if state.exists() else {}
    if old.get('status') == 'released': return
    if old.get('retry_after_ns', 0) > time.time_ns(): return
    p = json.loads(row['payload']); source_spec = json.loads(row['spec'])
    # Validate the retained local pair and its source proof, never arbitrary paths.
    require_same(p['source'], p['snapshot'])
    verify_tree(Path(p['source']), p['files'])
    proof = read(Path(p['source']) / 'source.json')
    if proof['spec'] != source_spec: raise ValueError('LeRobot release source proof mismatch')
    source = Path(source_spec['source'])
    if (p['robot'], p['group']) != (source_spec['robot'], source_spec['group']):
        raise ValueError('Training/source route mismatch')
    for other in db.execute('SELECT id,state,spec FROM tasks'):
        if other['id'] != tid and json.loads(other['spec']).get('source') == str(source) and other['state'] != 'uploaded':
            raise ValueError('Other local consumer still needs complete LeRobot')
    training.sealed(cfg, source_spec)
    route = next(r for r in cfg['capture']['routes'] if r['id'] == source_spec['route'])
    store = Path(route['output']) / '.ingest'
    ensure_real_path(store)
    # Coordinate with capture catalogue recovery/publication; do not modify it.
    with (store / 'publish.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        capture_db = Path(cfg['capture']['state_root']) / 'queue.sqlite3'
        with sqlite3.connect(capture_db.as_uri() + '?mode=ro', uri=True) as capture:
            capture.row_factory = sqlite3.Row
            candidates = [r for r in capture.execute("SELECT * FROM uploads WHERE state='completed' AND dataset_done=1")
                          if json.loads(r['payload']).get('kind') == 'batch'
                          and json.loads(r['payload']).get('dataset') == str(source)
                          and json.loads(r['payload']).get('route') == source_spec['route']]
        if len(candidates) != 1:
            raise ValueError('等待202完整LeRobot备份完成，保留本机数据')
        upstream = candidates[0]; payload = json.loads(upstream['payload'])
        if (payload['batch_id'], payload['batch_name'], payload['dataset_files']) != (source_spec['batch_id'], source_spec['name'], proof['files']):
            raise ValueError('202 batch manifest/source mismatch; retained')
        receipt = read(Path(cfg['state_root']) / 'receipts' / (tid + '.json'))
        if receipt.get('status') != 'completed': raise ValueError('Missing completed training pair receipt')
        for variant in ('normal', 'speedup'):
            files = [{**f, 'path': f['path'][len(variant)+1:]} for f in p['files'] if f['path'].startswith(variant + '/')]
            dest = Path(task[variant + '_root']) / p['group'] / destination_name(p, variant)
            if receipt['variants'][variant]['destination'] != str(dest): raise ValueError('Training destination changed')
            verify_backup(spec['training_server'], dest, files, digest([cfg['fingerprint'], tid, variant]))
            audit = Path(cfg['state_root']) / 'pair-reports' / tid
            pair_dest = Path(task[variant + '_root']) / p['group'] / '.workflow-pairs' / p['name']
            if receipt['pair_reports'][variant]['destination'] != str(pair_dest): raise ValueError('Pair proof destination changed')
            verify_backup(spec['training_server'], pair_dest, manifest(audit), digest([cfg['fingerprint'], tid, 'pair-report']))
        dest = Path(task['lerobot_root']) / p['group'] / source_spec['name']
        upstream_receipt = read(Path(cfg['capture']['state_root']) / 'receipts' / (upstream['id'] + '.json'))
        if upstream_receipt.get('status') != 'completed' or upstream_receipt.get('destination') != str(dest):
            raise ValueError('Missing durable 202 receipt; retained')
        verify_backup(spec['data_server'], dest, proof['files'], digest([cfg['capture']['fingerprint'], upstream['id']]))
        if not old.get('trees'):
            require_same(source, payload['dataset_snapshot'])
            verify_tree(source, proof['files'])
            # Follow only the immutable parent chain in this specific batch store.
            generation_store = source.parent
            expected_store = store / 'streams' / source_spec['batch_id'] / '.worker-generations' / 'current'
            if generation_store != expected_store: raise ValueError('Unexpected generation store; retained')
            trees = []; visited = set(); generation = source
            while generation:
                ensure_real_path(generation)
                if generation.parent != generation_store or generation in visited or not generation.name.startswith('gen-'):
                    raise ValueError('Unsafe generation ancestry; retained')
                visited.add(generation)
                marker = read(generation / 'ingest/generation.json')
                if (marker.get('route'), marker.get('fingerprint'), marker.get('state')) != (route['id'], cfg['capture']['fingerprint'], 'ready'):
                    raise ValueError('Generation ownership mismatch; retained')
                for name in ('data', 'videos'):
                    folder = generation / name
                    if folder.exists() or folder.is_symlink():
                        ensure_real_path(folder)
                        trees.append({'path': str(folder), 'snapshot': snapshot(folder), 'files': manifest(folder),
                                      'quarantine': str(generation / ('.gui-release-' + tid + '-' + name))})
                generation = Path(marker['parent']) if marker.get('parent') else None
            old = {'status': 'releasing', 'task': tid, 'source': str(source), 'trees': trees,
                   'verified_ns': time.time_ns(), 'remote_202': str(dest)}
            durable_json(state, old)
        if old.get('source') != str(source) or old.get('task') != tid: raise ValueError('Release intent mismatch')
        if not shutil.rmtree.avoids_symlink_attacks: raise ValueError('Safe deletion unavailable')
        for tree in old['trees']:
            folder = Path(tree['path']); quarantine = Path(tree['quarantine'])
            # Revalidate persisted paths before any deletion on recovery.
            if (folder.parent.parent != source.parent or folder.name not in ('data', 'videos')
                    or quarantine != folder.parent / ('.gui-release-' + tid + '-' + folder.name)):
                raise ValueError('Unsafe release intent path')
            ensure_real_path(folder); ensure_real_path(quarantine)
            if folder.exists():
                if quarantine.exists(): raise ValueError('Ambiguous quarantine; retained')
                check_tree(folder, tree)
                os.rename(folder, quarantine)
            if quarantine.exists():
                check_tree(quarantine, tree, partial=True)
                shutil.rmtree(quarantine)
        old.update(status='released', released_ns=time.time_ns())
        old.pop('error', None); old.pop('retry_after_ns', None)
        durable_json(state, old)


def release_complete_batches(spec, db, destination_name):
    if not spec['task'].get('delete_lerobot', False): return
    for row in db.execute("SELECT * FROM tasks WHERE state='uploaded' ORDER BY updated_ns,id").fetchall():
        state = Path(spec['training']['state_root']) / 'retention' / (row['id'] + '.json')
        try:
            release_one(spec, db, row, destination_name)
        except Exception as exc:
            report = read(state) if state.exists() else {'task': row['id'], 'status': 'retained'}
            report.update(error=str(exc), retry_after_ns=time.time_ns() + 30 * 10**9)
            durable_json(state, report)
            print('完整LeRobot释放待重试，未完成的数据保持保留：' + str(exc), flush=True)
