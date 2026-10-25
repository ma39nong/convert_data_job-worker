"""Capture configuration is separate from the production server receiver."""
from pathlib import Path
import yaml
from convest.config import WORKSPACE, load_config, digest
from convest.ingest.config import history_policy
from bag_worker.idle import ALGORITHM_VERSION, default_config
from dataclasses import asdict


def load(path):
    cfg = yaml.safe_load(Path(path).read_text())
    if cfg.get('schema_version') != 1:
        raise ValueError('Unsupported capture config version')
    def resolve(value):
        p = Path(value).expanduser()
        return str((p if p.is_absolute() else WORKSPACE / p).resolve())
    for key in ('state_root', 'work_root', 'remote_raw_root', 'remote_dataset_root'):
        cfg[key] = resolve(cfg[key])
    for key in ('state_root', 'work_root'):
        if not Path(cfg[key]).is_relative_to(WORKSPACE / 'outputs') or Path(cfg[key]) == WORKSPACE / 'outputs':
            raise ValueError('Local capture state/work must be below project outputs/')
    if cfg.get('require_mount'):
        cfg['require_mount'] = resolve(cfg['require_mount'])
        if any(not Path(cfg[k]).is_relative_to(cfg['require_mount']) for k in ('remote_raw_root', 'remote_dataset_root')):
            raise ValueError('Remote targets must be within require_mount')
    for key, default in [('poll_seconds', 10), ('stable_scans', 3), ('stable_seconds', 60),
                         ('retry_seconds', 60), ('max_attempts', 3), ('max_jobs_per_scan', 1)]:
        value = cfg.setdefault(key, default)
        if type(value) is not int or value < 1:
            raise ValueError('Invalid ' + key)
    if cfg['stable_scans'] < 2 or cfg['stable_seconds'] < (cfg['stable_scans'] - 1) * cfg['poll_seconds']:
        raise ValueError('Stability must span independent observations')
    mode=cfg.get('delivery_mode','single')
    if mode not in ('single','daily'):raise ValueError('Invalid delivery_mode')
    if mode=='daily':
        cfg['batch_hours']=cfg.get('batch_hours',24)
        if type(cfg['batch_hours']) is not int or cfg['batch_hours']<1:raise ValueError('Invalid batch_hours')
        for key in ('delete_raw_after_upload','cleanup_intermediates'):
            cfg.setdefault(key,True)
            if type(cfg[key]) is not bool:raise ValueError('Invalid '+key)
        if cfg.get('batch_name_format', 'range-v1') not in ('range-v1', 'completion-mmdd-v1'):
            raise ValueError('Invalid batch_name_format')
        cfg['local_batch_root']=resolve(cfg.get('local_batch_root','outputs/capture-daily/batches'))
        if not Path(cfg['local_batch_root']).is_relative_to(WORKSPACE/'outputs') or Path(cfg['local_batch_root'])==WORKSPACE/'outputs':
            raise ValueError('Local batches must be below project outputs/')
    routes, ids, roots = [], set(), [Path(cfg[k]) for k in ('state_root','work_root','remote_raw_root','remote_dataset_root')]
    if mode=='daily':roots.append(Path(cfg['local_batch_root']))
    for raw in cfg['routes']:
        robot, group = raw['robot'], raw['group']
        rid = f'{robot}-{group}'
        if robot not in ('sharpa', 'wuji') or group not in ('A', 'B') or rid in ids:
            raise ValueError('Invalid or duplicate capture route')
        ids.add(rid)
        source = resolve(raw['source'])
        conversion = load_config(WORKSPACE / raw.get('conversion_config', f'configs/{robot}.yaml'))
        if conversion['robot'] != robot:
            raise ValueError('Conversion robot mismatch')
        conversion.update(source_root=source, repo_id=raw.get('repo_id', f'fr3_{robot}/capture_{group.lower()}'))
        routes.append({'id': rid, 'robot': robot, 'group': group, 'source': source,
                       'conversion': conversion, 'history': history_policy(raw.get('initial_history', cfg.get('initial_history')))})
        if mode=='daily':
            routes[-1].update(output=str(Path(cfg['local_batch_root'])/robot/group),batch_hours=cfg['batch_hours'])
            if 'batch_name_format' in cfg:routes[-1]['batch_name_format']=cfg['batch_name_format']
        roots.append(Path(source))
    if not routes:
        raise ValueError('No capture routes')
    for i, a in enumerate(roots):
        for b in roots[i+1:]:
            if a.is_relative_to(b) or b.is_relative_to(a):
                raise ValueError('Input/state/work/upload paths overlap')
    cfg['routes'] = routes
    # Pin all roots and history policies: changing destination must never silently
    # reinterpret the existing queue's completed uploads.
    cfg['fingerprint'] = digest({'config': cfg, 'idle': asdict(default_config()), 'idle_version': ALGORITHM_VERSION,
                                 'protocol': 'capture-daily-v2' if mode=='daily' else 'capture-single-recording-v1'})
    return cfg
