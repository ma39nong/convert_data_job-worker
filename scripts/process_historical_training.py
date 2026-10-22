#!/usr/bin/env python3
"""Bounded historical-generation export, independent of the automatic sealed queue.

The user explicitly selects ready, previously verified generations. No upstream
catalogue is sealed or changed. Only retained Parquet chunks and RGB cross the
SFTP mount; the existing full training-server copy verifies the original complete
artifact hashes and the retained compressed chunks. Both transformations run here.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess
import tempfile
import time
import uuid

import pyarrow.parquet as pq

from convest.config import WORKSPACE, digest
from convest.ingest.batches import durable_json
from convest.capture.transfer import manifest, verify_tree, ensure_real_path
from convest.jobs import checksum
from convest.pipeline import load_records
from convest.training.config import load
from convest.training.normal import projection, export, validate_normal
from convest.training.adatempo.lerobot_main import process
from convest.training.adatempo.lerobot_checks import validate_output
from convest.training.ssh_transfer import ssh_args, remote
from convest.training.worker import recipe


def read(path):
    return json.loads(Path(path).read_text())


def inventory(source):
    result = {}
    for area in ('meta', 'data', 'videos'):
        for p in sorted((source / area).rglob('*')):
            if p.is_symlink():
                raise ValueError('Unexpected source symlink')
            if p.is_file():
                stat = p.stat()
                result[p.relative_to(source).as_posix()] = [stat.st_size, stat.st_mtime_ns]
    return result


def chunk_proofs(path, fields):
    parquet = pq.ParquetFile(path)
    offsets = []
    for row in range(parquet.metadata.num_row_groups):
        group = parquet.metadata.row_group(row)
        for col in range(group.num_columns):
            column = group.column(col)
            if not any(column.path_in_schema == k or column.path_in_schema.startswith(k + '.') for k in fields):
                continue
            start = min(x for x in (column.dictionary_page_offset, column.data_page_offset)
                        if x is not None and x >= 4)
            offsets.append((start, column.total_compressed_size))
    with path.open('rb') as stream:
        stream.seek(-8, 2)
        tail = stream.read(8)
        if tail[4:] != b'PAR1':
            raise ValueError('Invalid Parquet footer')
        length = struct.unpack('<I', tail[:4])[0] + 8
        offsets.append((path.stat().st_size - length, length))
        result = []
        for offset, length in sorted(offsets):
            stream.seek(offset)
            data = stream.read(length)
            if len(data) != length:
                raise ValueError('Incomplete retained Parquet chunk')
            result.append({'offset': offset, 'size': length, 'sha256': hashlib.sha256(data).hexdigest()})
    return result


def freeze(group_root, filesystem_root=None):
    catalog = read(group_root / '.ingest/batches.json')
    if len(catalog['batches']) != 1:
        raise ValueError('Select one historical batch explicitly; multiple batches are not implicit')
    batch = catalog['batches'][0]
    if (group_root.name not in ('A', 'B')
            or not re.fullmatch(r'batch-[0-9]{6,}', batch['id'])
            or not re.fullmatch(r'[0-9]+_[0-9]+_data_lerobot', batch['name'])):
        raise ValueError('Invalid historical batch identity')
    native = batch['generation']
    source = filesystem_root / native.lstrip('/') if filesystem_root is not None else Path(native)
    if not source.resolve().is_relative_to((group_root / '.ingest/streams').resolve()):
        raise ValueError('Historical generation escapes its own route')
    marker = read(source / 'ingest/generation.json')
    owner = read(group_root / '.ingest/owner.json')
    if (marker.get('state') != 'ready' or marker.get('route') != owner.get('route')
            or marker.get('fingerprint') != owner.get('fingerprint')):
        raise ValueError('Generation identity does not match its catalogue')
    info = read(source / 'meta/info.json')
    report = read(source / 'conversion/verification.json')
    if (report.get('status') != 'passed' or report.get('full_video_decode') is not True
            or report.get('episodes') != info['total_episodes'] or report.get('frames') != info['total_frames']
            or batch['episodes'] != info['total_episodes'] or batch['frames'] != info['total_frames']):
        raise ValueError('Historical generation has no matching complete verification')
    projected = projection(info, 'sharpa')
    fields = [k for k, v in projected['features'].items() if v['dtype'] != 'video']
    before = inventory(source)
    expected = {}
    for record in load_records(source):
        for name, artifact in record['ingest']['artifacts'].items():
            if Path(name).is_absolute() or '..' in Path(name).parts:
                raise ValueError('Invalid historical artifact path')
            expected[name] = {'path': name, 'size': artifact['bytes'], 'sha256': artifact['sha256']}
    core = {name for name in before if name.startswith(('data/', 'videos/'))}
    if set(expected) != core:
        raise ValueError('Historical artifact inventory differs from provenance')
    for name, signature in before.items():
        p = source / name
        if name in expected:
            if signature[0] != expected[name]['size']:
                raise ValueError('Historical artifact size changed')
            if name.startswith('data/'):
                expected[name]['ranges'] = chunk_proofs(p, fields)
        else:
            expected[name] = {'path': name, 'size': signature[0], 'sha256': checksum(p)}
    if inventory(source) != before:
        raise ValueError('Source changed during snapshot')
    return {'batch_id': batch['id'], 'name': batch['name'], 'native_generation': native,
            'source': str(source), 'route': owner['route'], 'robot': 'sharpa', 'group': group_root.name,
            'upstream_batch_state': batch['state'], 'info': info, 'inventory': before,
            'files': [expected[k] for k in sorted(expected)], 'fields': fields,
            'source_verification': report['status']}


def guard(spec):
    source = Path(spec['source'])
    if inventory(source) != spec['inventory']:
        raise ValueError('Fixed historical source changed')
    for item in spec['files']:
        path = source / item['path']
        if item['path'].startswith('meta/') and checksum(path) != item['sha256']:
            raise ValueError('Source metadata changed')
        if 'ranges' in item and chunk_proofs(path, spec['fields']) != item['ranges']:
            raise ValueError('Retained source chunks changed')


CACHE_CODE = r'''
from pathlib import Path
import hashlib,json,sys
q=json.load(sys.stdin)
root=Path(q['root']).resolve(strict=True)
cache=root/q['robot']/q['group']/q['name']
if not cache.resolve().is_relative_to(root) or cache.is_symlink():raise ValueError('Unsafe cache')
total=0
for row in q['files']:
 rel=Path(row['path'])
 if rel.is_absolute() or '..' in rel.parts:raise ValueError('Unsafe cache artifact')
 p=cache/rel
 if p.is_symlink() or not p.resolve().is_relative_to(cache.resolve()):raise ValueError('Unsafe cached path')
 a=p.stat()
 if a.st_size!=row['size']:raise ValueError('Cache size differs: '+row['path'])
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
 if h.hexdigest()!=row['sha256']:raise ValueError('Cache full SHA256 differs: '+row['path'])
 with p.open('rb') as f:
  for part in row.get('ranges',[]):
   f.seek(part['offset']);b=f.read(part['size'])
   if len(b)!=part['size'] or hashlib.sha256(b).hexdigest()!=part['sha256']:raise ValueError('Source retained chunks differ from verified cache: '+row['path'])
 z=p.stat()
 if (a.st_ino,a.st_size,a.st_mtime_ns,a.st_ctime_ns)!=(z.st_ino,z.st_size,z.st_mtime_ns,z.st_ctime_ns):raise ValueError('Cached input changed')
 total+=a.st_size
print(json.dumps({'status':'passed','complete_source_files_sha256':True,'retained_source_chunks_match_verified_full_input':True,'files':len(q['files']),'bytes':total,'cache':str(cache)}))
'''


SEED_CODE = r'''
from pathlib import Path
import hashlib,json,shutil,sys
q=json.load(sys.stdin);root=Path(q['root']).resolve(strict=True);stage=Path(q['staging'])
if not stage.resolve().is_relative_to(root):raise ValueError('Staging escapes root')
cache=root/q['robot']/q['group']/q['name'];count=total=0
for row in q['files']:
 rel=Path(row['path'])
 if rel.is_absolute() or '..' in rel.parts:raise ValueError('Unsafe manifest path')
 if not row['path'].startswith('normal/videos/'):continue
 source=cache/str(rel)[len('normal/'):];target=stage/rel
 if source.is_symlink() or not source.resolve().is_relative_to(cache.resolve()):raise ValueError('Unsafe cache video')
 if not source.is_file() or source.stat().st_size!=row['size']:continue
 h=hashlib.sha256()
 with source.open('rb') as f:
  for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
 if h.hexdigest()!=row['sha256']:continue
 if not target.resolve().is_relative_to(stage.resolve()):raise ValueError('Unsafe stage video')
 target.parent.mkdir(parents=True,exist_ok=True)
 if not target.exists():shutil.copyfile(source,target)
 count+=1;total+=row['size']
print(json.dumps({'seeded_video_files':count,'seeded_video_bytes':total}))
'''


def call(target, code, request):
    result = subprocess.run([*ssh_args(target), target['host'], shlex.join([target['python'], '-c', code])],
                            input=json.dumps(request), check=True, capture_output=True, text=True, timeout=3600)
    return json.loads(result.stdout)


def deliver(cfg, spec, pair, task, files, workers, status):
    target = cfg['training_target']
    request = {'root': target['dataset_root'], 'robot': 'sharpa', 'group': spec['group'],
               'name': pair.name, 'task': task, 'fingerprint': cfg['fingerprint'],
               'files': files, 'operation': 'prepare'}
    response = remote(target, request)
    if response['status'] != 'existing':
        seed = call(target, SEED_CODE, {'root': target['dataset_root'], 'robot': 'sharpa',
                    'group': spec['group'], 'name': spec['name'], 'staging': response['staging'], 'files': files})
        status('uploading', **seed)
        # Disjoint file lists permit independent rsync streams; final commit waits
        # for every stream and checks the complete tree through the normal helper.
        streams = [[] for _ in range(workers)]
        sizes = [0] * workers
        for item in sorted(files, key=lambda v: v['size'], reverse=True):
            index = sizes.index(min(sizes))
            if '\n' in item['path'] or '\r' in item['path']:
                raise ValueError('Unsupported upload filename')
            streams[index].append(item['path'])
            sizes[index] += item['size']
        with tempfile.TemporaryDirectory(prefix='historical-rsync-') as tmp:
            def send(index):
                if not streams[index]:
                    return
                listing = Path(tmp) / str(index)
                listing.write_text('\n'.join(streams[index]) + '\n')
                subprocess.run(['rsync', '-rlt', '--partial', '--checksum', '--protect-args',
                    '--timeout=120', '--no-perms', '--no-owner', '--no-group',
                    '--files-from=' + str(listing), '--rsync-path=' + target['rsync'],
                    '-e', shlex.join(ssh_args(target)), str(pair) + '/',
                    target['host'] + ':' + response['staging'] + '/'], check=True)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                list(pool.map(send, range(workers)))
    status('remote_sha256')
    result = remote(target, {**request, 'operation': 'commit'})
    status('uploaded', **result)
    return result


def run_batch(cfg, spec, root, upload_workers, status):
    identity = {k: spec[k] for k in ('route', 'name', 'native_generation')}
    identity.update(recipe=recipe(cfg), source_hashes=[(f['path'], f['sha256']) for f in spec['files']],
                    operation='explicit-historical-generation-v1')
    task = digest(identity)
    pair = root / spec['group'] / (spec['name'] + '--' + spec['batch_id'] + '--' + task[:16])
    pair.parent.mkdir(parents=True, exist_ok=True)
    pair.mkdir(exist_ok=True)
    ensure_real_path(pair)
    if any(pair.iterdir()) and not (pair / 'source.json').exists():
        raise ValueError('Refuse a nonempty unowned historical output')
    status('checking_full_input_on_verified_cache', task=task, output=str(pair))
    cache_report = call(cfg['training_target'], CACHE_CODE,
        {'root': cfg['training_target']['dataset_root'], 'robot': 'sharpa', 'group': spec['group'],
         'name': spec['name'], 'files': spec['files']})
    guard(spec)
    source_marker = pair / 'source.json'
    if source_marker.exists():
        previous = read(source_marker)
        for key in ('native_generation', 'route', 'name', 'group', 'files', 'inventory'):
            if previous[key] != spec[key]:
                raise ValueError('Historical output belongs to a changed source')
        spec = previous  # Preserve the first snapshot's state/proof on upload recovery.
    else:
        durable_json(source_marker, spec)
    cache_marker = pair / 'source-cache-verification.json'
    if cache_marker.exists():
        if read(cache_marker) != cache_report:
            raise ValueError('Historical cache proof changed')
    else:
        durable_json(cache_marker, cache_report)
    for variant in ('normal', 'speedup'):
        destination = pair / variant
        marker = pair / (variant + '.complete.json')
        if marker.exists():
            verify_tree(destination, read(marker)['files'])
            continue
        if destination.exists():
            failed = root / '.failed' / (task + '-' + variant + '-' + uuid.uuid4().hex)
            failed.parent.mkdir(exist_ok=True)
            destination.rename(failed)
        destination.mkdir()
        status(variant, task=task)
        if variant == 'normal':
            result = export(spec['source'], destination, 'sharpa')
            for item in spec['files']:
                if item['path'].startswith('videos/') and checksum(destination / item['path']) != item['sha256']:
                    raise ValueError('Normal video differs from original provenance')
        else:
            result = process(pair / 'normal', destination, workers=cfg['workers'], cmax=cfg['cmax'])
        durable_json(marker, {'validation': result, 'files': manifest(destination)})
    status('final_source_and_output_validation')
    normal = validate_normal(spec['source'], pair / 'normal', 'sharpa')
    speedup = validate_output(pair / 'normal', pair / 'speedup')
    guard(spec)
    pipeline = pair / 'pipeline.json'
    if pipeline.exists():
        previous = read(pipeline)
        if (previous['task'] != task or previous['status'] != 'passed'
                or previous['normal'] != normal or previous['speedup'] != speedup
                or previous['source_cache_verification'] != cache_report):
            raise ValueError('Historical completion proof changed')
    else:
        durable_json(pipeline, {'status': 'passed', 'task': task, 'source': identity,
            'source_batch_state': spec['upstream_batch_state'],
            'scope': 'explicit historical snapshot; upstream active/sealed state not changed',
            'normal': normal, 'speedup': speedup, 'source_cache_verification': cache_report,
            'created_ns': time.time_ns()})
    files = manifest(pair)
    result = deliver(cfg, spec, pair, task, files, upload_workers, status)
    return {'group': spec['group'], 'normal': normal, 'speedup': speedup, 'local': str(pair),
            'upload': result, 'source_unchanged': True}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--filesystem-root', type=Path, required=True)
    p.add_argument('--source', required=True, help='Absolute data-server sharpa root')
    p.add_argument('--config', type=Path, default=WORKSPACE / 'configs/training.local.yaml')
    p.add_argument('--output', type=Path, default=WORKSPACE / 'outputs/historical-sharpa')
    p.add_argument('--groups', nargs='+', choices=['A', 'B'], default=['A', 'B'])
    p.add_argument('--upload-workers', type=int, choices=range(1, 9), default=4)
    args = p.parse_args()
    if not Path(args.source).is_absolute() or '..' in Path(args.source).parts:
        raise ValueError('Historical source must be an absolute server path')
    cfg = load(args.config)
    if cfg['training_target']['transport'] != 'ssh':
        raise ValueError('Historical cache verification currently requires SSH training target')
    root = args.output.resolve()
    if not root.is_relative_to(WORKSPACE / 'outputs') or root == WORKSPACE / 'outputs':
        raise ValueError('Historical outputs must be below project outputs')
    ensure_real_path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        results = []
        for group in args.groups:
            def status(phase, **detail):
                value = {'group': group, 'phase': phase, 'updated_ns': time.time_ns(), **detail}
                durable_json(root / 'progress.json', value)
                print(json.dumps(value), flush=True)
            status('snapshot')
            spec = freeze(args.filesystem_root / args.source.lstrip('/') / group, args.filesystem_root)
            durable_json(root / ('snapshot-' + group + '.json'), spec)
            try:
                result = run_batch(cfg, spec, root, args.upload_workers, status)
            except Exception as exc:
                status('failed', error=f'{type(exc).__name__}: {exc}')
                raise
            results.append(result)
            durable_json(root / 'report.json', {'status': 'running', 'results': results})
        durable_json(root / 'report.json', {'status': 'passed', 'results': results,
                    'original_data_preserved': True, 'training_started': False})
        print('REPORT=' + str(root / 'report.json'), flush=True)


if __name__ == '__main__':
    main()
