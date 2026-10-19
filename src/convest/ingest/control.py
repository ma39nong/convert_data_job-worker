"""Durable, queue-preserving maintenance requests, independent of job identity."""
from pathlib import Path
import time

from convest.config import atomic_json
from .publish import OWNER,read


def maintenance_requested(config):
    path=Path(config['state_root'])/'maintenance.json'
    if not path.exists():return False
    value=read(path)
    if value.get('owner')!=OWNER or value.get('fingerprint')!=config['fingerprint'] or type(value.get('drain')) is not bool:
        raise ValueError('Invalid maintenance control; refusing to start another job')
    return value['drain']


def request_maintenance(config,drain):
    state=Path(config['state_root'])
    if state.is_symlink() or read(state/'owner.json')!={'owner':OWNER,'kind':'state','fingerprint':config['fingerprint']}:
        raise ValueError('Maintenance requires an initialized matching service state')
    atomic_json(state/'maintenance.json',{'owner':OWNER,'fingerprint':config['fingerprint'],
                                         'drain':drain,'requested_ns':time.time_ns()})


def progress_reporter(config,route,episode,prefix=''):
    path=Path(config['state_root'])/'progress.json'
    def report(phase,**detail):
        now=time.time_ns();old=read(path) if path.exists() else {}
        phase=prefix+phase
        same=old.get('route')==route['id'] and old.get('episode')==episode and old.get('phase')==phase
        record={'route':route['id'],'episode':episode,'phase':phase,
                'phase_started_ns':old['phase_started_ns'] if same else now,'updated_ns':now,**detail}
        atomic_json(path,record)
        print(__import__('json').dumps({'progress':record},ensure_ascii=False),flush=True)
    return report
