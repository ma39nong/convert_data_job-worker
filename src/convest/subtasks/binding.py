"""Standalone, stdlib-only provenance binding used by local and SSH writers."""
import copy
import hashlib
import json
from pathlib import Path

BINDING_PROFILE = 'derived-dataset-source-v1'


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def bound_file(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Unsafe annotation source path')
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError('Annotation source outside dataset')
    return path


def bind_annotation(root, final_root, episode, document, snapshot, snapshot_bytes=None):
    """Bind only after all dataset artifacts are finalized; retain upstream evidence."""
    root = Path(root)
    info = json.loads((root / 'meta/info.json').read_text())
    entry = next(r for line in (root / 'meta/episodes.jsonl').read_text().splitlines()
                 if line.strip() for r in [json.loads(line)] if r['episode_index'] == episode)
    previous = document['source']
    if (previous['length'] != entry['length'] or previous['fps'] != info['fps']
            or previous.get('source_recording_id') != entry.get('source_recording_id')):
        raise ValueError('Derived annotation identity mismatch')
    paths = ['meta/info.json', 'meta/episodes.jsonl', 'meta/tasks.jsonl',
             info['data_path'].format(episode_chunk=episode // 1000, episode_index=episode)]
    paths += [info['video_path'].format(episode_chunk=episode // 1000, episode_index=episode, video_key=k)
              for k, v in info['features'].items() if v['dtype'] == 'video']
    mapping = f'processing/episode_{episode:06d}.json'
    if (root / mapping).exists():
        paths.append(mapping)
    snapshot_path = root / snapshot
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    if snapshot_path.exists():
        raise ValueError('Upstream annotation snapshot already exists')
    if snapshot_bytes is None:
        snapshot_bytes = (json.dumps(document, ensure_ascii=False, allow_nan=False) + '\n').encode()
    if json.loads(snapshot_bytes) != document:
        raise ValueError('Upstream annotation bytes do not match reviewed document')
    snapshot_path.write_bytes(snapshot_bytes)
    doc = copy.deepcopy(document)
    doc['source'] = dict(dataset=str(final_root), episode_index=episode, length=entry['length'],
                         fps=info['fps'], source_recording_id=entry.get('source_recording_id'),
                         sha256={p: file_sha(bound_file(root, p)) for p in paths})
    doc['source_migration'] = dict(profile=BINDING_PROFILE, upstream_source=copy.deepcopy(previous),
                                   upstream_annotation=snapshot, upstream_annotation_sha256=file_sha(snapshot_path))
    verify_annotation(root, doc)
    return doc


def verify_annotation(root, doc):
    root = Path(root)
    source = doc['source']
    hashes = source.get('sha256', {})
    if not hashes:
        raise ValueError('Annotation has no source hashes')
    info = json.loads((root / 'meta/info.json').read_text())
    episode = source['episode_index']
    entries = [json.loads(line) for line in (root / 'meta/episodes.jsonl').read_text().splitlines() if line.strip()]
    entry = next(r for r in entries if r['episode_index'] == episode)
    required = {'meta/info.json', 'meta/episodes.jsonl', 'meta/tasks.jsonl',
                info['data_path'].format(episode_chunk=episode // 1000, episode_index=episode)}
    required.update(info['video_path'].format(episode_chunk=episode // 1000, episode_index=episode, video_key=k)
                    for k, v in info['features'].items() if v['dtype'] == 'video')
    if (not required.issubset(hashes) or source['length'] != entry['length'] or source['fps'] != info['fps']
            or source.get('source_recording_id') != entry.get('source_recording_id')):
        raise ValueError('Annotation source identity or required hashes mismatch')
    for relative, expected in hashes.items():
        if file_sha(bound_file(root, relative)) != expected:
            raise ValueError('Annotation source hash mismatch: ' + relative)
    migration = doc.get('source_migration')
    if migration and file_sha(bound_file(root, migration['upstream_annotation'])) != migration['upstream_annotation_sha256']:
        raise ValueError('Upstream annotation snapshot changed')
