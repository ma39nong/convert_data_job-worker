"""Delete only a validated, remotely acknowledged original via durable quarantine."""
from pathlib import Path
import json
import os
import shutil
import time
from convest.ingest.inputs import snapshot,require_same,InputChanged
from . import ledger
from .transfer import ensure_real_path


def check_remaining(root,expected):
    """Allow an interrupted deletion's remaining subset, never extra/replaced files."""
    current=snapshot(root);known={r['path']:r for r in expected}
    for row in current:
        old=known.get(row['path'])
        if not old or row['directory']!=old['directory']:
            raise InputChanged('Unexpected quarantined entry; preserved')
        keys=('inode','device') if row['directory'] else ('inode','device','size','mtime_ns','ctime_ns')
        if any(row[k]!=old[k] for k in keys):
            raise InputChanged('Quarantined original was replaced or changed; preserved')


def delete_original(cfg,queue,payload,task_id):
    row=queue.db.execute('SELECT * FROM recordings WHERE route=? AND episode=?',(payload['route'],payload['episode'])).fetchone()
    row=dict(row)
    if row['raw_deleted_ns']:
        return
    upload=queue.db.execute('SELECT raw_done FROM uploads WHERE id=?',(task_id,)).fetchone()
    if not upload or not upload[0] or not row['raw_uploaded_ns'] or not row['raw_receipt']:
        raise ValueError('Deletion requires durable verified remote upload and ledger receipt')
    route=next(r for r in cfg['routes'] if r['id']==payload['route'])
    source=Path(payload['source'])
    if source!=Path(route['source'])/payload['episode'] or not payload.get('quality_verified'):
        raise ValueError('Deletion source is outside configured validated recording')
    ensure_real_path(source.parent)
    quarantine=source.parent/'.capture-cleanup'/task_id
    if row['quarantine'] and row['quarantine']!=str(quarantine):
        raise ValueError('Deletion intent quarantine differs')
    ensure_real_path(quarantine)
    if row['deletion_state']=='retained':
        require_same(source,payload['source_snapshot'])
        if quarantine.exists():
            raise ValueError('Unowned quarantine exists; preserved')
        ledger.change(queue,payload,{'deletion_state':'deleting','quarantine':str(quarantine)},'raw_deletion_started')
        ledger.export(cfg,queue)
    if source.exists() and not quarantine.exists():
        require_same(source,payload['source_snapshot'])
        quarantine.parent.mkdir(exist_ok=True)
        os.rename(source,quarantine)
        try:
            check_remaining(quarantine,payload['source_snapshot'])
        except BaseException:
            if not source.exists():os.rename(quarantine,source)
            raise
    if quarantine.exists():
        check_remaining(quarantine,payload['source_snapshot'])
        # shutil.rmtree uses descriptor-relative, symlink-resistant traversal on
        # Linux. Never remove the source path after the atomic rename.
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ValueError('Safe directory deletion is unavailable on this platform')
        shutil.rmtree(quarantine)
    ledger.change(queue,payload,{'deletion_state':'deleted','raw_deleted_ns':time.time_ns()},'raw_deleted')
    ledger.export(cfg,queue)
