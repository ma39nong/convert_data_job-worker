"""Validate all paths without creating them or touching a remote server."""
from pathlib import Path
import yaml
from convest.config import WORKSPACE,load_config,digest
from convest.registry import get_target
from bag_worker.idle import ALGORITHM_VERSION,default_config
from dataclasses import asdict
import re


def history_policy(value):
    if not isinstance(value,dict) or value.get('mode') not in ('none','include','all'):
        raise ValueError('Explicit initial_history required: mode none, include or all')
    if set(value)-{'mode','episodes'}:raise ValueError('Unknown initial_history field')
    names=value.get('episodes',[])
    if not isinstance(names,list) or any(not isinstance(n,str) or not re.fullmatch(r'episode[0-9]+',n) for n in names):
        raise ValueError('initial_history episodes must be exact episode directory names')
    if value['mode']!='include' and names:raise ValueError('episodes only allowed with initial_history mode include')
    return {'mode':value['mode'],'episodes':sorted(set(names))}


def load_service_config(path):
    path=Path(path).resolve();cfg=yaml.safe_load(path.read_text())
    if cfg.get('schema_version')!=1:raise ValueError('Unsupported service config')
    def resolve(value):
        p=Path(value).expanduser()
        # abspath preserves a publication symlink's name, unlike resolve().
        import os
        return Path(os.path.abspath(p if p.is_absolute() else WORKSPACE/p))
    for key in ('state_root','work_root','data_root'):cfg[key]=str(resolve(cfg[key]))
    work=Path(cfg['work_root'])
    if not work.is_relative_to(WORKSPACE/'outputs') or work==WORKSPACE/'outputs':
        raise ValueError('Work root must remain below this project outputs/')
    for k,default in [('poll_seconds',10),('max_attempts',3),('retry_seconds',60),('stable_scans',3),('stable_seconds',60),('max_jobs_per_scan',1),('max_checks_per_scan',4)]:
        v=cfg.setdefault(k,default)
        if type(v) is not int or v<1:raise ValueError(f'Invalid {k}')
    if cfg['stable_scans']<2:raise ValueError('At least two independent scans are required')
    if cfg['stable_seconds']<(cfg['stable_scans']-1)*cfg['poll_seconds']:
        raise ValueError('Stable duration must span the required independent scans')
    if cfg['max_checks_per_scan']<cfg['max_jobs_per_scan']:raise ValueError('Check budget must cover processing budget')
    hours=cfg.setdefault('batch_hours',72)
    if type(hours) is not int or hours<1:raise ValueError('batch_hours must be a positive integer')
    roots=[Path(cfg['state_root']),work];pairs=set();routes=[];history={}
    for route in cfg['routes']:
        robot,group=route['robot'],route['group']
        if robot not in ('sharpa','wuji') or group not in ('A','B') or (robot,group) in pairs:
            raise ValueError('Invalid or duplicate route')
        pairs.add((robot,group));route=dict(route);route['id']=f'{robot}-{group}'
        history[route['id']]=history_policy(route.pop('initial_history',cfg.get('initial_history')))
        root=Path(cfg['data_root'])
        route['source']=str(root/'origin-data'/robot/group)
        route['output']=str(root/'data-lerobot'/robot/group)
        route['batch_hours']=hours
        base=load_config(resolve(route.get('conversion_config',f'configs/{robot}.yaml')))
        if base['robot']!=robot:raise ValueError('Conversion robot mismatch')
        base.update(source_root=route['source'],output_root=route['output'],repo_id=route.get('repo_id',f'fr3_{robot}/auto_{group.lower()}'))
        route['conversion']=base;routes.append(route)
        roots.extend((Path(route['source']),Path(route['output'])))
    if not routes:raise ValueError('No routes')
    for i,a in enumerate(roots):
        # Resolve parents to catch existing symlink aliases while preserving the
        # A/B leaf names (these become dataset publication pointers).
        ar=a.parent.resolve()/a.name
        for b in roots[i+1:]:
            br=b.parent.resolve()/b.name
            if ar.is_relative_to(br) or br.is_relative_to(ar):raise ValueError('Input/state/work/output paths overlap')
    cfg['routes']=routes
    cfg['initial_history_by_route']=history
    cfg['fingerprint']=digest({'routes':routes,'idle':asdict(default_config()),'idle_version':ALGORITHM_VERSION,
        'converter_version':get_target('lerobot_v21_multimodal').version,'publisher':'timed-batches-v1','readiness':'stable-structural-v1'})
    return cfg
