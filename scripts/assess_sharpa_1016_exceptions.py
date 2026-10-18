"""Diagnostics only: never waive collection-state gates or publish recovered data."""
from pathlib import Path
import json
import sqlite3
import shutil
import time
import yaml
from bag_worker.idle import trim_bag
from convest.config import atomic_json
from convest.sources import robot_rosbag2 as adapter
from convest.sources.gello_rosbag2 import snapshot
from convest.recipes import multimodal as recipe
from convest.preflight import validate_media_endpoints
from convest.quality import RecordingRejected


def assess(inventory, config, control, stage_source, guarded_remove):
    source=Path(inventory['source_root']);target=Path(inventory['target_root'])
    cache=control/'cache'
    contract=yaml.safe_load(Path(config['contract']).read_text())
    reports=[]
    for name in inventory['episodes']:
        state=json.loads((source/name/'collection_state.json').read_text())
        if state.get('state')=='finalized' and state.get('finalized') is True and not state.get('failures'):
            continue
        saved=target/f'production/recovery_assessment/{name}.json'
        if saved.exists():
            reports.append(json.loads(saved.read_text()));continue
        slot=cache/f'assess_{name}';guarded_remove(slot,cache);slot.mkdir()
        outcome={'episode':name,'capture_state':state.get('state'),'capture_failures':state.get('failures'),
                 'published':False,'assessment_scope':'SQLite integrity/counts, unchanged default cleaning, required modality endpoints and full-stream 30Hz causal continuity; no task-success judgment and no gate bypass'}
        print(f'{name}: separate recovery assessment (no publishing)',flush=True)
        try:
            if shutil.disk_usage(cache).free < inventory['original_sizes'][name]*4+50*1024**3:
                raise OSError('Insufficient assessment staging reserve')
            proof=stage_source(source/name,slot/'raw'/name);raw=slot/'raw'/name
            meta=yaml.safe_load((raw/'metadata.yaml').read_text())['rosbag2_bagfile_information']
            count=0
            for relative in meta['relative_file_paths']:
                volume=(raw/relative).resolve()
                if not volume.is_relative_to(raw.resolve()):raise ValueError('Unsafe SQLite path')
                with sqlite3.connect(volume.as_uri()+'?mode=ro',uri=True) as db:
                    db.execute('PRAGMA query_only=ON')
                    if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('SQLite corruption')
                    count+=db.execute('SELECT count(*) FROM messages').fetchone()[0]
            if count!=meta['message_count']:raise ValueError('Metadata message count mismatch')
            cleaned=slot/'cleaned'/name
            report=trim_bag(raw,cleaned)
            outcome['cleaning']={k:report[k] for k in ('source_start_ns','mapping','deleted_seconds','deleted','kept') if k in report}
            item=adapter.discover(cleaned)[0]
            if item['source_start_ns'] is None or item['source_end_ns'] is None:
                cm=yaml.safe_load((cleaned/'metadata.yaml').read_text())['rosbag2_bagfile_information']
                item['source_start_ns']=cm['starting_time']['nanoseconds_since_epoch']
                item['source_end_ns']=item['source_start_ns']+cm['duration']['nanoseconds']
                outcome['window_fallback']='cleaned bag record-time bounds; diagnostic only'
            with adapter.Bag(cleaned,config['schema_dir']) as bag:
                streams,aligned=recipe.prepare(adapter,bag,item,config,contract)
                validate_media_endpoints(bag,streams,'sharpa')
                outcome['alignment']=aligned.report
                segments=recipe.output_segments(item,aligned,config)
                outcome['candidate_frames']=sum(s['bounds'][1]-s['bounds'][0] for s in segments)
            outcome['technical_result']='continuous_multimodal_candidate'
            outcome['recommendation']=('Requires review of why operator discarded it and whether task succeeded before any export.'
                if state.get('state')=='discarded' else 'Header/continuity checks pass after standard cleaning; requires explicit recovery policy before export.')
            if snapshot(source/name)!=proof['snapshot']:raise ValueError('Original changed during assessment')
            outcome['original']=proof
        except RecordingRejected as exc:
            outcome.update(technical_result='fails_current_continuity_rules',reason=exc.reason,details=exc.details)
        except Exception as exc:
            outcome.update(technical_result='assessment_failed',error=f'{type(exc).__name__}: {exc}')
        outcome['finished_ns']=time.time_ns()
        atomic_json(saved,outcome);reports.append(outcome)
        atomic_json(control/'recovery_assessment.json',{'assessed':len(reports),'results':reports})
        guarded_remove(slot,cache)
    result={'assessed':len(reports),'published':0,'results':reports,
            'technical_counts':{k:sum(r['technical_result']==k for r in reports) for k in sorted({r['technical_result'] for r in reports})}}
    atomic_json(target/'production/recovery_assessment.json',result)
    atomic_json(control/'recovery_assessment.json',result)
    return result
