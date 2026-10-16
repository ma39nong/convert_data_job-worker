"""Serializable quality metrics with explicit denominators; no inferred packet loss."""
from pathlib import Path
import json
import numpy as np
from .config import atomic_json


def stream_profile(series):
    times=np.asarray(series.times);delta=np.diff(times)
    return {'samples_after_timestamp_deduplication':len(times),
            'first_timestamp_ns':int(times[0]),'last_timestamp_ns':int(times[-1]),
            'median_hz':float(1e9/np.median(delta)) if len(delta) else None,
            'interval_ms':{'median':float(np.median(delta)/1e6),'p99':float(np.percentile(delta,99)/1e6),
                           'max':float(delta.max()/1e6)} if len(delta) else None,
            'source_loss_rate':None,'source_loss_rate_reason':'No expected-rate/sequence-number loss contract'}


def quality_report(root,verification):
    """Read only provenance columns, never expand dense images for quality summaries."""
    import pyarrow.parquet as pq
    root=Path(root);info_path=root/'meta/info.json'
    if not info_path.exists():
        result={'status':'unavailable','verification':verification}
        atomic_json(root/'conversion/quality_report.json',result);return result
    info=json.loads(info_path.read_text());by_ep={r['episode_index']:r for r in verification.get('results',[])}
    sources=[]
    for path in sorted((root/'conversion/records').glob('*.json')):
        record=json.loads(path.read_text());a=record['alignment'];grid=a['grid_frames']
        item={'source':record['source'],'grid_frames':grid,'invalid_grid_points':a['invalid_frames'],
              'invalid_grid_ratio':a['invalid_frames']/grid if grid else None,
              'exported_frames':sum(e['length'] for e in record['episodes']),
              'unmatched_points_by_stream':a['unmatched'], 'max_accepted_age_ms':a['max_age_ms'],
              'rgb_tolerated_points':a['camera_short_gap_tolerance']['accepted_frames'],
              'depth_splice_tolerated_points':a.get('depth_splice_tolerance',{}).get('accepted_frames'),
              'source_loss_rate':None,'cleaning':record.get('cleaning'),'episodes':[]}
        for ep in record['episodes']:
            index=ep['episode_index'];outcome=by_ep.get(index,{'status':'not_checked'})
            entry={'episode_index':index,'frames':ep['length'],'verification':outcome['status']}
            if 'depth_splice_tolerance' in outcome and outcome['depth_splice_tolerance'] is not None:
                entry['depth_splice_tolerated_points']=outcome['depth_splice_tolerance']['accepted_frames']
            try:
                relative=info['data_path'].format(episode_index=index,episode_chunk=index//1000)
                f=(root/relative).resolve()
                if not f.is_relative_to(root.resolve()):raise ValueError('Unsafe quality report artifact path')
                names=info['features']['audit.sample_timestamp_ns']['names']
                counts=np.zeros(len(names),np.int64);pairs=np.zeros(len(names),np.int64);previous=None;ages=[]
                for batch in pq.ParquetFile(f).iter_batches(batch_size=256,columns=['audit.sample_timestamp_ns','source_timestamp_ns']):
                    values=np.asarray(batch.column(0).to_pylist(),np.int64)
                    grid=batch.column(1).to_numpy()
                    ages.append(np.where(values>0,(grid[:,None]-values)/1e6,np.nan))
                    if previous is not None:values=np.vstack([previous,values])
                    good=(values[1:]>0)&(values[:-1]>0)
                    counts+=((values[1:]==values[:-1])&good).sum(axis=0);pairs+=good.sum(axis=0)
                    previous=values[-1:]
                all_ages=np.concatenate(ages)
                entry['sample_age_ms']={name:({'p50':float(np.nanpercentile(all_ages[:,i],50)),
                                              'p99':float(np.nanpercentile(all_ages[:,i],99)),
                                              'max':float(np.nanmax(all_ages[:,i]))}
                                             if np.isfinite(all_ages[:,i]).any() else None) for i,name in enumerate(names)}
                entry['repeated_sample_timestamps']={name:{'equal_adjacent_pairs':int(c),'comparable_adjacent_pairs':int(n),
                                                          'ratio':float(c/n) if n else None}
                                                     for name,c,n in zip(names,counts,pairs)}
            except Exception as exc:entry['metrics_error']=f'{type(exc).__name__}: {exc}'
            item['episodes'].append(entry)
        sources.append(item)
    def optional(name):
        p=root/'conversion'/name
        return json.loads(p.read_text()) if p.exists() else []
    report={'status':verification['status'],'verification_finished_ns':verification.get('finished_ns'),'source_loss_rate_policy':'not inferred from repetition or alignment invalidity',
            'sources':sources,'quality_exclusions':optional('rejections.json'),'conversion_errors':optional('errors.json'),
            'verification_errors':[r for r in verification.get('results',[]) if r['status']!='passed'],
            'global_verification_error':verification.get('global_error')}
    atomic_json(root/'conversion/quality_report.json',report)
    return report


def job_quality_report(root,jobs):
    """Whole-chain summary: raw input, removed waits, conversion and validation."""
    import yaml
    root=Path(root);dataset_path=root/'dataset/conversion/quality_report.json'
    dataset=json.loads(dataset_path.read_text()) if dataset_path.exists() else None
    converted={Path(s['source']).name:s for s in (dataset or {}).get('sources',[])}
    rows=[]
    for job in jobs:
        row={'episode':job['episode'],'source':job['source'],'state':job['state'],
             'conversion_committed':job.get('conversion_committed',False),
             'input_profile':job.get('input_profile'),'conversion':converted.get(job['episode']),
             'error':job.get('error'),'quality_rejection':job.get('quality_rejection')}
        try:
            before=yaml.safe_load((Path(job['source'])/'metadata.yaml').read_text())['rosbag2_bagfile_information']
            seconds=before['duration']['nanoseconds']/1e9
            row.update(input_duration_s=seconds,input_messages=before['message_count'])
            cleaned=root/'cleaned'/job['episode']/'trim_report.json'
            if cleaned.exists():
                trim=json.loads(cleaned.read_text())
                row.update(deleted_seconds=trim['deleted_seconds'],deleted_intervals=len(trim['mapping']),
                           deleted_duration_ratio=trim['deleted_seconds']/seconds if seconds>0 else None,
                           deleted_messages=trim['deleted'],kept_messages=trim['kept'],
                           deleted_message_ratio=trim['deleted']/before['message_count'] if before['message_count'] else None)
        except Exception as exc:row['input_metrics_error']=f'{type(exc).__name__}: {exc}'
        rows.append(row)
    failed=any(j['state'] in ('failed','verification_failed') for j in jobs) or (dataset or {}).get('status')=='failed'
    report={'status':'failed' if failed else 'passed_with_exclusions' if any(j['state']=='excluded' for j in jobs) else 'passed',
            'source_loss_rate':None,'source_loss_rate_reason':'Not inferred from alignment, repetition or deliberate trimming',
            'dataset_report':'dataset/conversion/quality_report.json' if dataset else None,
            'sources':rows}
    atomic_json(root/'quality_report.json',report)
    return report
