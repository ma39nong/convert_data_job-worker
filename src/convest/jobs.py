"""Local, resumable trim -> convert -> verify jobs. Inputs remain read-only."""
from pathlib import Path
import fcntl
import hashlib
import json
import sqlite3
import shutil
import time
import yaml

from bag_worker.idle import trim_bag, default_config
from dataclasses import asdict
from convest.config import WORKSPACE, atomic_json, digest
from convest.sources.gello_rosbag2 import snapshot
from convest.pipeline import convert, load_records
from convest.sources.robot_rosbag2 import Bag, read_streams


def checksum(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda:f.read(8*1024*1024),b''):value.update(part)
    return value.hexdigest()


def check_ready(path,contract,schema_dir,profile=None):
    """No writes; consistency, finalized capture, full hashes and stable snapshots."""
    path=Path(path).resolve();before=snapshot(path)
    if any(p.name.endswith(('.partial','.tmp','-wal','-shm')) for p in path.iterdir()):
        raise ValueError('Input has transfer/SQLite temporary files')
    state=json.loads((path/'collection_state.json').read_text())
    if state.get('state')!='finalized' or state.get('finalized') is not True or state.get('failures'):
        raise ValueError('Capture is not finalized and eligible')
    meta=yaml.safe_load((path/'metadata.yaml').read_text())['rosbag2_bagfile_information']
    if meta['storage_identifier']!='sqlite3' or meta.get('compression_format'):
        raise ValueError('Only uncompressed sqlite3 inputs supported')
    total=0;topic_counts={};files=[]
    for relative in meta['relative_file_paths']:
        f=(path/relative).resolve()
        if not f.is_relative_to(path) or not f.is_file():raise ValueError('Missing or unsafe bag volume')
        with sqlite3.connect(f.as_uri()+'?mode=ro',uri=True) as db:
            db.execute('PRAGMA query_only=ON')
            if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Incomplete or corrupt SQLite file')
            names=dict(db.execute('SELECT id,name FROM topics'))
            for tid,count in db.execute('SELECT topic_id,count(*) FROM messages GROUP BY topic_id'):
                total+=count;name=names[tid];topic_counts[name]=topic_counts.get(name,0)+count
        files.append({'name':relative,'bytes':f.stat().st_size,'sha256':checksum(f)})
    if total!=meta['message_count']:raise ValueError('Bag message count differs from metadata')
    expected={v['topic_metadata']['name']:v['message_count'] for v in meta['topics_with_message_count']}
    if any(topic_counts.get(k,0)!=v for k,v in expected.items()) or set(topic_counts)-set(expected):
        raise ValueError('Topic counts differ from metadata')
    # Decode low-dimensional streams and inspect all required sensor headers before trimming.
    with Bag(path,schema_dir) as bag:
        streams=read_streams(bag,contract)
        hand=22 if 'measured_left_hand' in contract['source_joint_names'] else 20
        for group,n in zip(('left_arm','right_arm','left_hand','right_hand'),(7,7,hand,hand)):
            if len(streams[f'state.{group}'].values[0])!=2*n or len(streams[f'action.{group}'].values[0])!=n:
                raise ValueError(f'Robot group dimensions do not match: {group}')
        from .preflight import validate_media_endpoints
        rgb,sensors=validate_media_endpoints(bag,streams,'sharpa' if 'measured_left_hand' in contract['source_joint_names'] else 'wuji')
        if profile is not None:
            from .reporting import stream_profile
            profile.update(streams={k:stream_profile(v) for k,v in streams.items()},rgb_shapes=rgb,sensor_shapes=sensors)
    if snapshot(path)!=before:raise ValueError('Input changed during readiness check')
    return {'source':str(path),'snapshot':before,'volumes':files,'messages':total,
            'metadata_sha256':checksum(path/'metadata.yaml'),
            'collection_state_sha256':checksum(path/'collection_state.json')}


def run(config,run_root,episodes=None,resume=False,workers=1,episode_list=None):
    from convest.verify import verify
    root=Path(run_root).resolve();source=Path(config['source_root']).resolve()
    if not root.is_relative_to(WORKSPACE/'outputs') or root==WORKSPACE/'outputs':
        raise ValueError('Job root must be under workspace outputs/')
    if root.is_relative_to(source) or source.is_relative_to(root):raise ValueError('Input/output overlap')
    from .preflight import select_names
    selected_paths=select_names(source,episodes,episode_list)
    if any(p.parent!=source for p in selected_paths):raise ValueError('Job source_root must contain episode directories')
    selected=[p.name for p in selected_paths]
    if root.exists() and (not resume or not (root/'manifest.json').is_file()):
        raise FileExistsError('Existing job root requires --resume and owned manifest')
    contract=yaml.safe_load(Path(config['contract']).read_text())
    from .recipes.multimodal import validate_contract
    validate_contract(contract,config)
    root.mkdir(parents=True,exist_ok=True)
    with (root/'lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        fingerprint=digest({'config':config,'idle':asdict(default_config()),'version':'job-2-idle-v3'})
        marker=root/'manifest.json'
        if marker.exists():
            previous=json.loads(marker.read_text())
            if previous.get('owner')!='convert-data-job-worker' or previous['fingerprint']!=fingerprint:
                raise ValueError('Job config changed; use a new job directory')
        else:atomic_json(marker,{'owner':'convert-data-job-worker','fingerprint':fingerprint,'config':config})
        results=[]
        cleaned=root/'cleaned';cleaned.mkdir(exist_ok=True)
        for name in selected:
            journal=root/'jobs'/f'{name}.json';old=json.loads(journal.read_text()) if journal.exists() else {}
            job={**old,'episode':name,'source':str(source/name),'updated_ns':time.time_ns()}
            def transition(state,**extra):
                job.update(state=state,updated_ns=time.time_ns(),**extra);atomic_json(journal,job)
                print(f'{name}: {state}',flush=True)
            try:
                transition('checking')
                profile={}
                ready=check_ready(source/name,contract,config['schema_dir'],profile)
                job['input_profile']=profile
                if old.get('readiness') and ready!=old['readiness']:raise ValueError('Source differs from prior job snapshot')
                transition('ready',readiness=ready,error=None)
                target=cleaned/name
                # A kill can land between export commit and recording its snapshot.
                # Only artifacts owned by a persisted 'trimming' transition may be regenerated.
                if old.get('state')=='trimming' and not old.get('cleaned_snapshot'):
                    for owned in (target,target.with_name(target.name+'.partial')):
                        if owned.is_symlink():raise ValueError('Refuse symlink at owned output')
                        if owned.exists():shutil.rmtree(owned)
                if target.exists():
                    report=json.loads((target/'trim_report.json').read_text())
                    if report['source']!=str(source/name):raise ValueError('Cleaned source mismatch')
                    if not old.get('cleaned_snapshot') or snapshot(target)!=old['cleaned_snapshot']:
                        raise ValueError('Cleaned output has no committed snapshot or has changed')
                else:
                    transition('trimming')
                    report=trim_bag(source/name,target)
                transition('trimmed',cleaned=str(target),cleaned_snapshot=snapshot(target),
                           deleted_seconds=report['deleted_seconds'])
                results.append(job.copy())
            except Exception as exc:
                transition('failed',error=f'{type(exc).__name__}: {exc}');results.append(job.copy())
        good=[j for j in results if j['state']=='trimmed']
        if good:
            # Explicit list avoids including jobs from earlier runs absent from this selection.
            selection=root/'selection.txt';selection.write_text(''.join(j['episode']+'\n' for j in good))
            cfg={**config,'source_root':str(cleaned),'output_root':str(root/'dataset')}
            atomic_json(root/'conversion_config.json',cfg)
            for job in good:
                job.update(state='converting',updated_ns=time.time_ns());atomic_json(root/'jobs'/f'{job["episode"]}.json',job)
            try:
                code=convert(cfg,resume=(root/'dataset/conversion/manifest.json').exists(),workers=workers,episode_list=selection)
                records=load_records(root/'dataset');completed={Path(r['source']).name for r in records}
                summary=json.loads((root/'dataset/conversion/summary.json').read_text())
                rejected={Path(r['source']).name:r for r in summary['quality_rejections']}
                errors={Path(r['source']).name:r for r in summary['errors']}
                checked=verify(root/'dataset',full_video=True,compare_source=True,raise_on_error=False) if records else None
                outcomes={}
                for outcome in (checked or {}).get('results',[]):
                    outcomes.setdefault(Path(outcome['source']).name,[]).append(outcome)
                for job in good:
                    name=job['episode']
                    try:
                        if name in completed:
                            job.update(conversion_committed=True)
                            relevant=outcomes.get(name,[])
                            if not relevant or any(r['status']!='passed' for r in relevant):
                                job.update(state='verification_failed',verification={'status':'failed','results':relevant,'report':'dataset/conversion/verification.json'},
                                           error=(checked or {}).get('global_error') or 'Episode verification failed')
                            else:
                                if snapshot(Path(job['source']))!=job['readiness']['snapshot']:
                                    raise ValueError('Original changed during job')
                                for volume in job['readiness']['volumes']:
                                    if checksum(Path(job['source'])/volume['name'])!=volume['sha256']:
                                        raise ValueError('Original content hash changed during job')
                                job.update(state='completed',verification={'status':'passed','results':relevant,'episodes':len(relevant),'frames':sum(r['frames'] for r in relevant),'report':'dataset/conversion/verification.json'},error=None,original_sha256_verified=True)
                        elif name in rejected:job.update(state='excluded',quality_rejection=rejected[name])
                        else:job.update(state='failed',error=errors.get(name,{'error':'No committed output'}))
                    except Exception as exc:
                        job.update(state='verification_failed' if name in completed else 'failed',error=f'{type(exc).__name__}: {exc}')
                    job['updated_ns']=time.time_ns()
                    atomic_json(root/'jobs'/f'{name}.json',job)
            except Exception as exc:
                # Preserve committed conversions when batch orchestration itself fails.
                committed={Path(r['source']).name for r in load_records(root/'dataset')}
                for job in good:
                    done=job['episode'] in committed
                    job.update(state='verification_failed' if done else 'failed',conversion_committed=done,
                               error=f'{type(exc).__name__}: {exc}')
                    atomic_json(root/'jobs'/f'{job["episode"]}.json',job)
        final=[json.loads((root/'jobs'/f'{n}.json').read_text()) for n in selected]
        summary={'robot':config['robot'],'selected':len(final),'completed':sum(j['state']=='completed' for j in final),
                 'excluded':sum(j['state']=='excluded' for j in final),'failed':sum(j['state'] in ('failed','verification_failed') for j in final),
                 'verification_failed':sum(j['state']=='verification_failed' for j in final),
                 'jobs':final}
        dataset_report=root/'dataset/conversion/verification.json'
        summary['dataset_verification_passed']=(json.loads(dataset_report.read_text()).get('status')=='passed') if dataset_report.exists() else None
        from .reporting import job_quality_report
        job_quality_report(root,final)
        summary['quality_report']='quality_report.json'
        summary['dataset_quality_report']='dataset/conversion/quality_report.json' if (root/'dataset/conversion/quality_report.json').exists() else None
        atomic_json(root/'summary.json',summary)
        print(json.dumps({k:v for k,v in summary.items() if k!='jobs'}),flush=True)
        return 2 if summary['failed'] or summary['dataset_verification_passed'] is False else 0
