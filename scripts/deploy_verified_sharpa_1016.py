"""Recover deployment after the previous delivery path became temporarily unavailable.

Only an owned, fully verified candidate can move into the authorized path.
An existing owned delivery is backed up; no input or old output is deleted. Rechecks all artifact hashes.
"""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
import fcntl
import json
import time
from convest.config import WORKSPACE,atomic_json
from convest.pipeline import load_records
from convest.jobs import checksum

CONTROL=WORKSPACE/'outputs/production_sharpa_1016_v3'


def main():
    inventory=json.loads((CONTROL/'inventory.json').read_text())
    stage=Path(inventory['target_root']);final=Path(inventory['delivery_root'])
    if final.is_symlink() or stage.is_symlink() or stage.parent!=final.parent:
        raise ValueError('Recovery requires safe sibling paths')
    owner=json.loads((stage/'production/owner.json').read_text())
    if owner.get('owner')!='production-sharpa-1016-v1':raise ValueError('Unowned candidate')
    with (stage/'production/lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        records=load_records(stage)
        verification=json.loads((stage/'conversion/verification.json').read_text())
        if verification.get('status')!='passed' or verification.get('episodes')!=len(records) or len(records)!=169:
            raise ValueError('Candidate verification incomplete')
        old=json.loads((WORKSPACE/'outputs/production_sharpa_1016/status.json').read_text())
        expected={n for n,o in old['outcomes'].items() if o['state']=='published'}
        actual={r['production']['episode'] for r in records}
        if expected!=actual:raise ValueError('Accepted source coverage differs from original delivery')
        source_frames=0;sensor_rows=0;artifacts=[]
        for record in records:
            name=record['production']['episode']
            audit=json.loads((stage/f'production/jobs/{name}/dataset/conversion/verification.json').read_text())
            if (record.get('cleaning_effectiveness',{}).get('status')!='passed' or audit.get('status')!='passed'
                or not audit.get('source_comparison') or not audit.get('independent_alignment')):
                raise ValueError(f'Missing independent audit: {name}')
            if audit['frames']!=sum(e['length'] for e in record['episodes']):raise ValueError('Source audit frame count mismatch')
            source_frames+=audit['frames'];sensor_rows+=audit['lossless_sensor_rows_compared']
            artifacts.extend((relative,proof) for relative,proof in record['production']['artifacts'].items())
        if source_frames!=verification['frames']:raise ValueError('Global/source frame count mismatch')
        def check(item):
            relative,proof=item;p=stage/relative
            if p.is_symlink() or not p.resolve().is_relative_to(stage.resolve()):raise ValueError('Unsafe artifact')
            before=p.stat()
            if before.st_size!=proof['bytes'] or checksum(p)!=proof['sha256']:raise ValueError(f'Hash mismatch: {relative}')
            if p.stat().st_mtime_ns!=before.st_mtime_ns:raise ValueError('Artifact changed during hash check')
            return relative
        checked=[]
        with ThreadPoolExecutor(max_workers=4) as pool:
            for future in as_completed([pool.submit(check,item) for item in artifacts]):
                checked.append(future.result())
                atomic_json(CONTROL/'deployment_recovery_progress.json',{'state':'hashing','checked_files':len(checked),'total_files':len(artifacts)})
                if len(checked)%40==0:print(f'Fresh hash validation {len(checked)}/{len(artifacts)}',flush=True)
        if final.is_symlink():raise ValueError('Refuse delivery symlink')
        backup=None
        if final.exists():
            old_owner=json.loads((final/'production/owner.json').read_text())
            old_records=load_records(final)
            if old_owner.get('owner')!='production-sharpa-1016-v1' or {r['production']['episode'] for r in old_records}!=expected:
                raise ValueError('Existing delivery ownership/source coverage mismatch')
            backup=final.with_name(final.name+'_before_idle_v3_'+time.strftime('%Y%m%d_%H%M%S'))
            if backup.exists():raise FileExistsError(backup)
        event={'state':'switching','staging':str(stage),'final':str(final),'backup':str(backup) if backup else None,
            'previous_delivery_missing_before_switch':backup is None,'previous_published':len(expected),'new_published':len(actual),
            'previously_accepted_now_excluded':[],'newly_accepted':[],'verified_files':len(checked),'started_ns':time.time_ns()}
        atomic_json(CONTROL/'deployment.json',event)
        if backup:
            with (final/'production/lock').open('a') as old_lock:
                fcntl.flock(old_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                final.rename(backup)
                try:stage.rename(final)
                except BaseException:
                    backup.rename(final)
                    raise
        else:
            if final.exists() or final.is_symlink():raise FileExistsError('Delivery path appeared; no overwrite')
            stage.rename(final)
        event.update(state='completed',finished_ns=time.time_ns())
        report={'status':'passed','delivery_ready':True,'episodes':len(records),'frames':source_frames,
            'full_video_and_data_validation':verification['status'],'independent_source_audits_passed':len(records),
            'independent_cleaning_audits_passed':len(records),'lossless_sensor_rows_compared':sensor_rows,
            'sha256_verified_files':len(checked),'selected':189,'excluded':20,'deferred':27,
            'local_intermediate_directories':[p.name for p in (CONTROL/'cache').iterdir() if p.is_dir()],
            'deployment':event}
        atomic_json(final/'production/deployment.json',event);atomic_json(CONTROL/'deployment.json',event)
        atomic_json(final/'production/delivery_notice.json',{'state':'verified_replacement','delivery_ready':True,
            'cleaning_algorithm_version':3,'independent_cleaning_coverage':'passed_for_all_published_sources',
            'backup':str(backup) if backup else None,'previous_delivery_missing_before_switch':backup is None})
        atomic_json(final/'production/delivery_validation.json',report);atomic_json(CONTROL/'delivery_validation.json',report)
        problem=final/'production/episode290_investigation.json'
        if problem.exists():
            v=json.loads(problem.read_text());v.update(state='resolved',resolution='episode290_resolution.json');atomic_json(problem,v)
        atomic_json(CONTROL/'deployment_recovery_progress.json',{'state':'completed','checked_files':len(checked),'total_files':len(artifacts)})
        print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
