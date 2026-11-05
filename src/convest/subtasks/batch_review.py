"""Review the complete loaded set, preserving confirmed work and revision guards.

Run with the local review server. Results/checkpoints survive interruptions; Ark
request caches are source/prompt/image specific. This is model review, not truth.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import threading
import hashlib
import json
from pathlib import Path
import time
import urllib.request
from .core import Episode,atomic_json,project
from .doubao import Client,credential_from_source
from .template import POLICY,annotate_template
from .behavior import scan, merge_candidates
from .boundaries import refine_all


def audit_reference(episode,doc,folder,client,progress=None):
    """Audit every existing confirmed boundary without modifying its annotation."""
    folder=Path(folder);path=folder/'reference-boundaries.json'
    if path.exists():
        result=json.loads(path.read_text())
        if result['source']!=episode.identity() or result['reference_revision']!=doc['revision']:raise ValueError('人工边界复查来源或版本变化')
        return result
    records=[]
    def record(response,key,cached,stage):records.append({'key':key,'cached':cached,'stage':stage,'usage':response.get('usage',{})})
    proposed,evidence,cues=refine_all(episode,[{**s,'reviewed':False} for s in doc['segments']],folder/'reference-frames',client,record,records,128,progress)
    unresolved=[int(i) for i,p in evidence.items() if p['source_frame']['needs_review']]
    result={'source':episode.identity(),'reference_revision':doc['revision'],'reference_segments':doc['segments'],
        'proposed_segments':proposed,'boundary_evidence':evidence,'unresolved_boundaries':unresolved,'requests':records,
        'differences':[{'index':i,'original_frame':a['end_frame'],'proposed_frame':b['end_frame']} for i,(a,b) in enumerate(zip(doc['segments'][:-1],proposed[:-1])) if a['end_frame']!=b['end_frame']]}
    atomic_json(path,result);return result


def enrich_report(output,report):
    """Summarize evidence and compare independent predictions to confirmed truth."""
    import numpy as np
    from .core import LABELS
    output=Path(output);run=output/'quality-review'/report['run_id']
    snapshot=json.loads((run/'annotations-before.json').read_text())
    for row in report['episodes']:
        folder=run/row['id'];behavior=folder/'behavior.json';proposal=folder/'proposal.json'
        if behavior.exists():
            scanned=json.loads(behavior.read_text())['events'];published=folder/'published.json'
            # Report the retained human decisions and mapped events, not a fresh
            # model candidate marked pending again after a human dismissed it.
            row['behavior_scanned_candidates']=len(scanned)
            row['behavior_events']=(json.loads(published.read_text()).get('review_events',[]) if published.exists()
                else merge_candidates(snapshot[row['id']].get('review_events',[]),scanned,snapshot[row['id']]['source']['length']))
            row['behavior_candidates']=sum(e['status']=='pending' for e in row['behavior_events'])
        if proposal.exists():
            result=json.loads(proposal.read_text());unresolved={int(i) for i in result['unresolved_boundaries']}
            for boundary in row.get('boundaries_detail',[]):boundary['needs_review']=boundary['index'] in unresolved or boundary['source_frame'].get('needs_review',True)
        reference_audit=folder/'reference-boundaries.json'
        if row['human_preserved'] and reference_audit.exists():
            audit=json.loads(reference_audit.read_text());unresolved=set(audit['unresolved_boundaries'])
            row.update(boundaries=len(audit['reference_segments'])-1,segments=len(audit['reference_segments']),unresolved=len(unresolved),reference_visual_differences=audit['differences'])
            row['boundaries_detail']=[{'index':i,'before':a['label'],'after':b['label'],'frame':a['end_frame'],
                'proposed_frame':audit['proposed_segments'][i]['end_frame'],'needs_review':i in unresolved,
                'source_frame':audit['boundary_evidence'][str(i)]['source_frame']} for i,(a,b) in enumerate(zip(audit['reference_segments'],audit['reference_segments'][1:]))]
        if row['human_preserved'] and proposal.exists():
            reference=snapshot[row['id']]['segments'];predicted=json.loads(proposal.read_text())['segments']
            length=snapshot[row['id']]['source']['length']
            a=np.empty(length,dtype=object);b=np.empty(length,dtype=object)
            for spans,array in ((reference,a),(predicted,b)):
                for span in spans:array[span['start_frame']:span['end_frame']]=span['label']
            coarse_a=np.array([LABELS[x][0] for x in a]);coarse_b=np.array([LABELS[x][0] for x in b])
            row['human_reference_comparison']={'reference_segments':len(reference),'predicted_segments':len(predicted),
                'fine_frame_agreement':float(np.mean(a==b)),'coarse_frame_agreement':float(np.mean(coarse_a==coarse_b)),
                'scope':'仅此条完整人工参考的逐帧标签一致率，不能代表其他片段或模型置信度'}
    report['summary']={'total':len(report['episodes']),
        'completed':sum(r['state'] in {'saved','audited_reference'} for r in report['episodes']),
        'boundaries':sum(r.get('boundaries') or 0 for r in report['episodes']),
        'unresolved_boundaries':sum(r.get('unresolved') or 0 for r in report['episodes']),
        'behavior_candidates':sum(r.get('behavior_candidates') or 0 for r in report['episodes']),
        'failed':[r['id'] for r in report['episodes'] if r['state'] not in {'saved','audited_reference'}]}
    atomic_json(run/'report.json',report);atomic_json(output/'quality-review'/'report.json',report)
    return report


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='outputs/subtasks/review')
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--credential-source',required=True)
    parser.add_argument('--workers',type=int,choices=(1,2,3),default=2)
    parser.add_argument('--resume',action='store_true',help='继续最近一轮，只重做未成功片段；已保存结果及人工修改不覆盖')
    args=parser.parse_args(argv);output=Path(args.output).resolve();base=f'http://127.0.0.1:{args.port}'
    def get(path):return json.load(urllib.request.urlopen(base+path,timeout=120))
    bootstrap=get('/api/bootstrap');token=bootstrap['token']
    if output!=Path(bootstrap['output']).resolve():raise ValueError('输出目录须与当前审核服务一致，避免报告和标注分离')
    entries=bootstrap['episodes'];snapshot={e['id']:get('/api/episode?id='+e['id'])['annotation'] for e in entries}
    previous=None
    if args.resume:
        previous=json.loads((output/'quality-review'/'report.json').read_text())
        if previous['policy']!=POLICY:raise ValueError('策略已变化，不能接续旧策略报告')
        snapshot=json.loads((output/'quality-review'/previous['run_id']/'annotations-before.json').read_text())
        if set(snapshot)!=set(e['id'] for e in entries):raise ValueError('片段集合已变化，不能接续旧批次')
    run_id=hashlib.sha256(json.dumps({'policy':POLICY,'snapshot':snapshot},sort_keys=True).encode()).hexdigest()[:16]
    root=output/'quality-review';run=root/run_id;run.mkdir(parents=True,exist_ok=True)
    atomic_json(run/'annotations-before.json',snapshot)
    key=credential_from_source(args.credential_source);client=Client(output/'doubao',key)
    report={'policy':POLICY,'run_id':run_id,'started_at':previous['started_at'] if previous else time.time(),'state':'running','total':len(entries),'episodes':[],
            'warning':'模型全量复查，不是人工真值或准确率；已人工确认的动作保留，异常及训练取舍由人工决定。'}
    if previous:report['resumed_at']=previous.get('resumed_at',[])+[time.time()]
    rows={}
    for entry in entries:
        id_=entry['id'];doc=snapshot[id_]
        row={'id':id_,'dataset':entry['dataset'],'episode_index':entry['episode_index'],'variant':entry['variant'],
             'revision_before':doc['revision'],'state':'queued','human_preserved':any(s['reviewed'] for s in doc['segments']),
             'requests_completed':0,'boundaries':None,'unresolved':None,'segments':None,
             'behavior_candidates':None,'requests':0,'dense_stride':1,'source_fps':entry['fps'],
             'boundaries_detail':[],'behavior_events':[],'revision_after':None,'changed_boundaries':None,'error':None}
        if previous:
            old_row=next(r for r in previous['episodes'] if r['id']==id_)
            row.update(old_row)
            if row['state'] not in {'saved','audited_reference'}:row.update(state='queued',error=None)
        report['episodes'].append(row);rows[id_]=row
    report_lock=threading.RLock()
    def checkpoint():
        with report_lock:
            _checkpoint()
    def _checkpoint():
        atomic_json(run/'report.json',report);atomic_json(root/'report.json',report)
    checkpoint();new_docs={}
    for id_,row in rows.items():
        if row['state'] in {'saved','audited_reference'}:
            current=get('/api/episode?id='+id_)['annotation']
            if current['revision']!=row['revision_after']:
                row.update(state='conflict',error='上次复查后人工已更新，此轮不覆盖；请单独复查新版本')
            else:new_docs[id_]=current
    ordered=sorted(entries,key=lambda e:(e['variant']=='speedup',not any(s['reviewed'] for s in snapshot[e['id']]['segments']),e['dataset'],e['episode_index']))
    def process(entry):
        id_=entry['id'];row=rows[id_];doc=snapshot[id_];row['state']='running';checkpoint()
        ep=Episode(entry['dataset'],entry['episode_index']);folder=run/id_;folder.mkdir(exist_ok=True)
        from .style import transition_examples
        model_client=Client(output/'doubao',key,style_examples=transition_examples(snapshot,ep))
        print(json.dumps({'episode':id_,'variant':entry['variant'],'state':'running'}),flush=True)
        try:
            result_path=folder/'proposal.json'
            if result_path.exists():
                result=json.loads(result_path.read_text());assert result['source']==ep.identity()
            elif entry['variant']=='normal':
                result=annotate_template(ep,output,api_key=key,client=model_client,max_requests=128,
                    progress=lambda done,total:(row.update(requests_completed=done),checkpoint()))
                result={'source':ep.identity(),**result}
                atomic_json(result_path,result)
            else:
                matches=[e for e in entries if e['variant']=='normal' and snapshot[e['id']]['source'].get('source_recording_id')==doc['source'].get('source_recording_id')]
                if len(matches)!=1:raise ValueError('speedup 不是唯一同源normal，未猜测配对')
                normal_entry=matches[0];normal_id=normal_entry['id'];normal=Episode(normal_entry['dataset'],normal_entry['episode_index'])
                if normal_id not in new_docs:raise ValueError('同源normal尚未成功复查/保存，未投影旧草稿')
                projected=project(normal,ep,new_docs[normal_id]);records=[]
                def record(response,key,cached,stage):records.append({'key':key,'cached':cached,'stage':stage,'usage':response.get('usage',{})})
                checked,evidence,cues=refine_all(ep,projected['segments'],folder/'visual-audit',model_client,record,records,128,
                    lambda done,total:(row.update(requests_completed=done),checkpoint()))
                differences=[i for i,(a,b) in enumerate(zip(projected['segments'][:-1],checked[:-1])) if a['end_frame']!=b['end_frame']]
                unresolved=sorted(set(differences)|{int(i) for i,p in evidence.items() if p['source_frame']['needs_review']})
                result={'source':ep.identity(),'method':'doubao_template_source_frames_v1','policy':POLICY,
                    'segments':projected['segments'],'boundary_evidence':evidence,'unresolved_boundaries':unresolved,
                    'requests':records,'request_count':len(records),'projection':{k:v for k,v in projected.items() if k not in {'segments','source'}},
                    'independent_visual_segments':checked,'mapping_visual_disagreements':differences,
                    'boundary_audit':{'scope':'every_boundary','source_fps':ep.fps,'dense_stride':1,'all_boundaries_checked':len(evidence)==len(projected['segments'])-1},
                    'warning':'normal按观测来源帧映射，speedup每一分界另看连续原帧；视觉与映射不一致保留疑点，不破坏来源对应。'}
                atomic_json(result_path,result)
            behavior_path=folder/'behavior.json'
            if behavior_path.exists():behavior=json.loads(behavior_path.read_text())
            else:
                behavior=scan(ep,output,client=client);atomic_json(behavior_path,behavior)
            row.update(boundaries=len(result['segments'])-1,unresolved=len(result['unresolved_boundaries']),
                segments=len(result['segments']),behavior_candidates=len(behavior['events']),behavior_events=behavior['events'],
                requests=len(result.get('requests',[])),dense_stride=1,source_fps=ep.fps)
            row['boundaries_detail']=[{'index':i,'before':a['label'],'after':b['label'],
                 'previous_frame':doc['segments'][i]['end_frame'] if i<len(doc['segments'])-1 and len(doc['segments'])==len(result['segments']) else None,
                 'frame':a['end_frame'],'needs_review':i in {int(x) for x in result['unresolved_boundaries']},
                 'source_frame':result['boundary_evidence'][str(i)]['source_frame']} for i,(a,b) in enumerate(zip(result['segments'],result['segments'][1:]))]
            if row['human_preserved']:
                audit_reference(ep,doc,folder,model_client,lambda done,total:(row.update(requests_completed=done),checkpoint()))
                assert get('/api/episode?id='+id_)['annotation']==doc,'人工参考在复查中变化，未覆盖'
                new_docs[id_]=doc;row.update(state='audited_reference',revision_after=doc['revision'])
            else:
                payload={'id':id_,'revision':doc['revision'],'proposal':result,'behavior_review':behavior,'run_id':run_id}
                req=urllib.request.Request(base+'/api/apply-quality-review',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Subtasks-Token':token})
                published=json.load(urllib.request.urlopen(req,timeout=120))['annotation'];new_docs[id_]=published
                atomic_json(folder/'published.json',published);row.update(state='saved',revision_after=published['revision'])
            row['changed_boundaries']=sum(s['end_frame']!=t['end_frame'] or s['label']!=t['label'] for s,t in zip(doc['segments'],result['segments']))
            print(json.dumps({'episode':id_,'state':row['state'],'boundaries':row['boundaries'],'unresolved':row['unresolved']}),flush=True)
        except Exception as exc:
            row.update(state='failed',error=str(exc));print(json.dumps({'episode':id_,'state':'failed','error':str(exc)}),flush=True)
        checkpoint()
    for variant in ('normal','speedup'):
        selected=[e for e in ordered if e['variant']==variant and rows[e['id']]['state'] not in {'saved','audited_reference','conflict'}]
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(process,selected))
    report.update(state='completed' if all(r['state'] in {'saved','audited_reference'} for r in report['episodes']) else 'needs_attention',finished_at=time.time())
    enrich_report(output,report)
    checkpoint();print(json.dumps({'state':report['state'],'episodes':len(entries),'report':str(root/'report.json')}),flush=True)

if __name__=='__main__':main()
