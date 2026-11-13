"""Dataset scoped workflows; never infer chronology from the phase number."""
from pathlib import PurePosixPath
import json

IN_HAND_PROFILE = 'tomato-in-hand-five-phases-v2'
LEGACY_PROFILE = 'tomato-plate-seven-phases-v1'
IN_HAND_SEQUENCE = ['拿起番茄','摘取番茄','调整番茄','摘取番茄','放回番茄']
IN_HAND_ADJUST = 'Right hand adjusts the tomato truss'

def in_hand_source(source):
    remote = source.get('remote_source') or {}
    path = remote.get('dataset') or source.get('dataset', '')
    return 'full-block-more' in PurePosixPath(path).parts

def in_hand_episode(episode):
    if not hasattr(episode,'root'):return False
    source = {'dataset': str(episode.root)}
    receipt = episode.root / 'cache-source.json'
    if receipt.exists():
        source['remote_source'] = json.loads(receipt.read_text()).get('manifest', {})
    return in_hand_source(source)


def source_path(source):
    return (source.get('remote_source') or {}).get('dataset') or source.get('remote_directory') or source.get('dataset', '')

def workflow_group(path):
    return 'without_return' if 'full-block-more' in PurePosixPath(path).parts else 'with_return'

def media_profile(path):
    return 'roi_four' if 'subtask-roi' in PurePosixPath(path).parts else 'original_three'

def entry_source_path(entry):
    if entry.get('remote_directory'):return entry['remote_directory']
    from pathlib import Path
    receipt=Path(entry['dataset'])/'cache-source.json'
    return json.loads(receipt.read_text()).get('manifest',{}).get('dataset',entry['dataset']) if receipt.exists() else entry['dataset']

def same_media(a,b):
    return media_profile(entry_source_path(a))==media_profile(entry_source_path(b))

def recording_key(row):
    keys=('source_recording_id','source_start_ns','source_end_ns')
    return tuple(str(row[k]) for k in keys) if all(row.get(k) is not None for k in keys) else None
