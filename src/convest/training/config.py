"""Independent configuration; never changes the upstream capture fingerprint."""
from pathlib import Path
import math
import os
import re
import subprocess
import sys
import yaml

from convest.config import WORKSPACE, digest
from convest.capture.transfer import ensure_real_path


def load(path):
    cfg = yaml.safe_load(Path(path).read_text())
    if cfg.get('schema_version') != 1:
        raise ValueError('Unsupported training configuration version')
    def resolve(value):
        p = Path(value).expanduser()
        p = p if p.is_absolute() else WORKSPACE / p
        ensure_real_path(p)
        return str(p.resolve())
    cfg['capture_project'] = resolve(cfg.get('capture_project', str(WORKSPACE)))
    capture_path = Path(cfg['capture_config']).expanduser()
    if not capture_path.is_absolute():
        capture_path = Path(cfg['capture_project']) / capture_path
    cfg['capture_config'] = resolve(capture_path)
    if not (Path(cfg['capture_project']) / 'src/convest/capture/config.py').is_file():
        raise ValueError('capture_project must be the upstream capture checkout')
    # Relative output/schema paths and the fingerprint belong to THAT checkout.
    # Import its loader in isolation; do not reinterpret a live config in ours.
    result = subprocess.run([sys.executable, '-c',
        'import json,sys; from convest.capture.config import load; print(json.dumps(load(sys.argv[1])))',
        cfg['capture_config']], cwd=cfg['capture_project'], check=True, capture_output=True, text=True,
        env={**os.environ, 'PYTHONPATH': str(Path(cfg['capture_project']) / 'src')})
    import json
    capture = json.loads(result.stdout)
    if capture.get('delivery_mode') != 'daily' or capture['batch_hours'] != 24:
        raise ValueError('Training derivatives require local 24h capture batches')
    for key in ('state_root', 'output_root'):
        cfg[key] = resolve(cfg[key])
        if not Path(cfg[key]).is_relative_to(WORKSPACE / 'outputs') or Path(cfg[key]) == WORKSPACE / 'outputs':
            raise ValueError('Training state/output must be below project outputs/')
    for key, default in [('poll_seconds', 60), ('retry_seconds', 300), ('workers', 1)]:
        cfg.setdefault(key, default)
        if type(cfg[key]) is not int or cfg[key] < 1 or (key == 'workers' and cfg[key] > 4):
            raise ValueError('Invalid ' + key)
    cfg.setdefault('cmax', 3.0)
    if type(cfg['cmax']) not in (float, int) or not math.isfinite(cfg['cmax']) or not 1 <= cfg['cmax'] <= 3:
        raise ValueError('cmax must be in [1, 3]')
    cfg.setdefault('min_free_gib', 8)
    if type(cfg['min_free_gib']) not in (float, int) or not math.isfinite(cfg['min_free_gib']) or cfg['min_free_gib'] < 0:
        raise ValueError('Invalid min_free_gib')
    target = cfg['training_target']
    if target.get('transport') not in ('mount', 'ssh'):
        raise ValueError('training_target.transport must be mount or ssh')
    if target['transport'] == 'mount':
        for key in ('require_mount', 'dataset_root'):
            target[key] = resolve(target[key])
        if not Path(target['dataset_root']).is_relative_to(target['require_mount']):
            raise ValueError('Training destination must be within required mount')
    else:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', target.get('host', '')):
            raise ValueError('Invalid SSH host or alias')
        if 'user' in target and not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', target['user']):
            raise ValueError('Invalid SSH user')
        if 'port' in target and (type(target['port']) is not int or not 1 <= target['port'] <= 65535):
            raise ValueError('Invalid SSH port')
        for key in ('python', 'rsync'):
            target.setdefault(key, 'python3' if key == 'python' else 'rsync')
            if not re.fullmatch(r'[A-Za-z0-9_./-]+', target[key]) or target[key].startswith('-'):
                raise ValueError('Invalid remote executable')
        root = Path(target['dataset_root'])
        if not root.is_absolute() or '..' in root.parts:
            raise ValueError('SSH dataset_root must be an absolute server path')
    if Path(target['dataset_root']).parts[-2:] != ('workspace', 'data'):
        raise ValueError('Training dataset_root must end with workspace/data')
    # Keep processing and upload roots separate from every upstream owned tree.
    own = [Path(cfg[k]) for k in ('state_root', 'output_root')]
    remotes = [Path(target['require_mount'])] if target['transport'] == 'mount' else []
    upstream = [Path(capture[k]) for k in ('state_root', 'work_root', 'local_batch_root',
                                          'remote_raw_root', 'remote_dataset_root')]
    upstream += [Path(r['source']) for r in capture['routes']]
    pairs = [(own[0], own[1])] + [(p, q) for p in own for q in upstream + remotes]
    if target['transport'] == 'mount':
        pairs += [(Path(target['dataset_root']), p) for p in upstream]
    if any(a.is_relative_to(b) or b.is_relative_to(a) for a, b in pairs):
        raise ValueError('Training and upstream paths overlap')
    cfg['fingerprint'] = digest({'config': cfg, 'capture_fingerprint': capture['fingerprint'],
                                 'protocol': 'native-normal-speedup-v1'})
    cfg['capture'] = capture
    return cfg
