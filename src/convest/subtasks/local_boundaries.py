"""Local state-motion onset cues; never proof of fruit release or human approval."""
import copy
import json
import time
import base64
import numpy as np
from .core import atomic_json
from .workspace import validate_coarse

POLICY='local_arm_onset_candidates_v1'

def onset_candidates(delta,other,center,radius,*,threshold=1e-4,quiet=3,sustained=3):
    """First moving sample after quiet. Other arm may have one isolated state spike.

    This spike tolerance is a cue, not a claim of semantic stillness; every
    candidate retains manual review and the exact state samples."""
    n=min(len(delta),len(other));result=[]
    for f in range(max(quiet,center-radius),min(n-sustained+1,center+radius+1)):
        if (np.all(delta[f-quiet:f]<=threshold) and np.all(delta[f:f+sustained]>threshold)
            and np.count_nonzero(other[f-quiet:f+sustained]>threshold)<=1):
            result.append(f)
    return result

def refine(ws,id_):
    from .template import images_at
    from .quarantine import assert_included
    from .core import values
    with ws.lock,ws.store.lock:
        assert_included(ws,id_);ep=ws.store.episode(id_)
        if ws.store.entries[id_]['variant']!='normal':raise ValueError('仅细化normal，再映射speedup')
        if ws.stage_path(id_,'coarse').exists():raise ValueError('粗阶段已经人工保存，未覆盖')
        proposal_path=ws.root/'coarse-proposals'/(id_+'.json')
        original=json.loads(proposal_path.read_text())
        source=ep.identity()
        if original['source']!=source:raise ValueError('来源变化，不能使用旧候选')
        if original.get('provenance',{}).get('local_boundary_refinement') or original.get('provenance',{}).get('visual_boundary_resolution'):
            return {'id':id_,'state':'preserved','reason':'本草稿已本地校准或经视频核对，保留现有结果','network_requests':0}
        rows=copy.deepcopy(original['segments'])
    if [r['label'] for r in rows]!=['pickup','harvest','adjust','harvest','return']:raise ValueError('本地细化仅支持当前五阶段流程')
    names=ep.info['features']['observation.state'].get('names',[]);q=values(ep.table,'observation.state');motion={}
    for hand in ('left','right'):
        cols=[i for i,n in enumerate(names) if n.startswith(hand+'_fr3_joint') and n.endswith('.position')]
        if len(cols)!=7:raise ValueError('缺少双臂七关节位置，未盲目移动边界')
        arm=q[:,cols]
        if not np.isfinite(arm).all():raise ValueError('关节状态含无效值')
        motion[hand]=np.r_[0,np.max(np.abs(np.diff(arm,axis=0)),axis=1)]
    evidence=[];radius=round(3*ep.fps);run=ws.root/'local-boundary-evidence'/id_/str(time.time_ns())
    for i,hand in enumerate(('left','right','left','right')):
        old=rows[i]['end_frame'];other='right' if hand=='left' else 'left'
        candidates=onset_candidates(motion[hand],motion[other],old,radius)
        used_radius=radius
        if not candidates:
            used_radius=round(6*ep.fps)
            candidates=onset_candidates(motion[hand],motion[other],old,used_radius)
        # Never choose the nearest arbitrary movement if several are plausible.
        if len(candidates)!=1:
            evidence.append({'boundary_index':i,'old_frame':old,'candidate_frames':candidates,'selected_frame':None,'needs_review':True,'reason':'候选不唯一或缺少静止交接，保留原边界'});continue
        frame=candidates[0]
        if not rows[i]['start_frame']<frame<rows[i+1]['end_frame']:
            evidence.append({'boundary_index':i,'old_frame':old,'selected_frame':None,'needs_review':True,'reason':'运动候选越过相邻阶段，保留原边界'});continue
        assets=images_at(ep,range(frame-3,frame+4),panel_size=480)
        files=[];run.mkdir(parents=True,exist_ok=True)
        for a in assets:
            p=run/f'boundary-{i+1}-frame-{a["frame"]}.jpg';p.write_bytes(base64.b64decode(a['data_url'].split(',')[1]));files.append({'frame':a['frame'],'path':str(p),'sha256':a['sha256']})
        evidence.append({'boundary_index':i,'old_frame':old,'candidate_frames':candidates,'selected_frame':frame,
            'seconds':frame/ep.fps,'search_radius_seconds':used_radius/ep.fps,'moving_arm':hand,'previous_frame':frame-1,'threshold_rad_per_sample':1e-4,
            'quiet_samples':3,'sustained_samples':3,'motion_samples':motion[hand][frame-3:frame+4].tolist(),
            'other_arm_samples':motion[other][frame-3:frame+4].tolist(),'other_arm_isolated_spikes':int(np.count_nonzero(motion[other][frame-3:frame+3]>1e-4)),'consecutive_four_camera_frames':files,
            'needs_review':True,'reason':'关节运动起点候选；释放、持串和任务语义仍需结合视频人工确认；不等于清洗阈值或绝对视觉首帧证明'})
        rows[i]['end_frame']=frame;rows[i+1]['start_frame']=frame
    rows=validate_coarse([{**r,'reviewed':False} for r in rows],ep.length,ep.fps)
    result=copy.deepcopy(original);prov=result['provenance'];prov['before_local_refinement']={'segments':original['segments'],'model_analysis':copy.deepcopy(prov.get('model_analysis'))}
    prov['local_boundary_refinement']={'policy':POLICY,'network_requests':0,'search_radius_seconds':3,'expanded_radius_seconds_if_empty':6,'evidence':evidence,'semantic_gate_automatically_proven':False}
    prov['coarse_segments']=rows;prov['unresolved_boundaries']=[0,1,2,3];prov['boundary_audit']={'scope':POLICY,'all_boundaries_checked':False}
    analysis=prov.get('model_analysis')
    if analysis:
        for i,r in enumerate(rows):analysis['segments'][i].update(start_seconds=r['start_frame']/ep.fps,end_seconds=r['end_frame']/ep.fps)
        for i,h in enumerate(analysis['handoffs']):
            h.update(seconds=rows[i]['end_frame']/ep.fps,needs_review=True,observation='本地双臂关节运动起点候选，原始Qwen描述另存于修改前记录',reason='请结合四路连续帧确认释放、停稳和下一动作意图')
        analysis['review_notes'].append('粗边界已使用本地关节状态细化到原始帧；投放时间和数量保留，所有交接仍需人工复核。')
    result.update(segments=rows,created_at=time.time(),warning='本地关节运动＋四路连续帧候选细化，未调用API；边界已按帧更新，投放/释放语义仍待人工审核。')
    prov['warning']=result['warning']
    audit_notes(result,ep.fps)
    with ws.lock,ws.store.lock:
        if ws.stage_path(id_,'coarse').exists():raise ValueError('细化期间已人工保存，未覆盖')
        if json.loads(proposal_path.read_text())!=original or ep.identity()!=source:raise ValueError('处理期间草稿或来源变化，未覆盖')
        atomic_json(ws.root/'coarse-proposal-history'/id_/(str(time.time_ns())+'.json'),original)
        atomic_json(proposal_path,result)
    atomic_json(run/'report.json',prov['local_boundary_refinement'])
    return {'id':id_,'state':'ready','network_requests':0,'boundaries':[{'before':e['old_frame'],'after':e.get('selected_frame'),'seconds':e.get('seconds')} for e in evidence]}


def audit_notes(proposal,fps):
    """Expose unresolved motion and delivery-time conflicts; do not move cuts."""
    evidence=proposal['provenance']['local_boundary_refinement']['evidence']
    missing=[e['boundary_index']+1 for e in evidence if e.get('selected_frame') is None]
    notes=[]
    visual=sum(bool(e.get('visual_context_checked')) for e in evidence)
    if missing:notes.append('第'+','.join(map(str,missing))+'处交接缺少唯一运动候选，已保留原边界')
    analysis=proposal['provenance'].get('model_analysis');rows=proposal['segments']
    if analysis:
        for key,index,name in [('first_harvest',1,'第一轮'),('second_harvest',3,'第二轮')]:
            times=analysis.get('harvest_summary',{}).get(key,{}).get('delivery_seconds',[])
            outside=[t for t in times if not rows[index]['start_frame']/fps<=t<=rows[index]['end_frame']/fps]
            if outside:notes.append(name+'模型投放时间'+','.join(map(str,outside))+'秒位于细化后的采摘阶段之外，需核对计时及交接；未改投放时间')
        analysis['review_notes']=[n for n in analysis.get('review_notes',[]) if not n.startswith('本地校准检查：')]+['本地校准检查：'+n for n in notes]
    proposal['warning']=(f'边界候选已核对：{4-len(missing)}/4处，其中{visual}处结合视频上下文选择。' if visual else f'本地校准完成：{4-len(missing)}/4处找到唯一运动候选。')+('；'.join(notes)+'。' if notes else '')+'运动候选及释放/持串语义仍待人工审核；未调用API。'
    proposal['provenance']['warning']=proposal['warning']
    proposal['provenance']['local_boundary_review_notes']=notes
    return notes


def audit_existing(ws,ids):
    results=[]
    for id_ in ids:
        try:
            with ws.lock,ws.store.lock:
                ep=ws.store.episode(id_)
                if ws.stage_path(id_,'coarse').exists():
                    results.append({'id':id_,'state':'preserved'});continue
                path=ws.root/'coarse-proposals'/(id_+'.json');old=json.loads(path.read_text())
                if old['source']!=ep.identity():raise ValueError('来源变化，未更新提示')
                new=copy.deepcopy(old);notes=audit_notes(new,ep.fps)
                if new!=old:
                    atomic_json(ws.root/'coarse-proposal-history'/id_/(str(time.time_ns())+'.json'),old)
                    atomic_json(path,new)
                results.append({'id':id_,'state':'ready','notes':notes})
        except Exception as exc:results.append({'id':id_,'state':'failed','error':str(exc)})
    return {'episodes':results,'network_requests':0}


def resolve_delivery_timing(ws,id_,correction):
    """Apply evidence-backed timing corrections, never approve/save a stage.

    Explicit release brackets keep visual sampling uncertainty separate from
    the point timestamps used by the existing summary UI. Cuts require explicit
    visual-context records and still retain unreviewed status.
    """
    import hashlib,math
    from pathlib import Path
    from .quarantine import assert_included
    with ws.lock,ws.store.lock:
        assert_included(ws,id_)
        if ws.stage_path(id_,'coarse').exists():raise ValueError('粗阶段已人工保存，未覆盖')
        ep=ws.store.episode(id_)
        if ws.store.entries[id_]['variant']!='normal':raise ValueError('仅校正normal草稿')
        path=ws.root/'coarse-proposals'/(id_+'.json');raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=correction.get('expected_sha256'):raise ValueError('草稿已变化，请重新核对')
        original=json.loads(raw)
        if original['source']!=ep.identity():raise ValueError('来源变化，未校正')
        evidence=correction.get('evidence',[])
        if not evidence or not correction.get('reason'):raise ValueError('缺少视频证据或校正说明')
        allowed=(Path.cwd()/'outputs'/'subtasks').resolve()
        for asset in evidence:
            p=Path(asset['path']).resolve()
            if not p.is_relative_to(allowed) or hashlib.sha256(p.read_bytes()).hexdigest()!=asset['sha256']:raise ValueError('证据路径或SHA无效')
        new=copy.deepcopy(original);analysis=new['provenance']['model_analysis'];records={}
        boundary_records=[]
        for record in correction.get('boundaries',[]):
            index=record['boundary_index'];frame=record['frame']
            if isinstance(index,bool) or not isinstance(index,int) or index not in range(4) or isinstance(frame,bool) or not isinstance(frame,int) or not record.get('observation'):raise ValueError('无效边界核对记录')
            new['segments'][index]['end_frame']=frame;new['segments'][index+1]['start_frame']=frame
            item=new['provenance']['local_boundary_refinement']['evidence'][index]
            boundary_records.append({**record,'previous_frame':original['segments'][index]['end_frame'],'human_reviewed':False})
            item['before_visual_context_check']=copy.deepcopy(item)
            item.update(selected_frame=frame,seconds=frame/ep.fps,visual_context_checked=True,needs_review=True,reason=record['observation'])
        if boundary_records:
            new['segments']=validate_coarse([{**r,'reviewed':False} for r in new['segments']],ep.length,ep.fps)
            new['provenance']['coarse_segments']=new['segments']
            for index,row in enumerate(new['segments']):analysis['segments'][index].update(start_seconds=row['start_frame']/ep.fps,end_seconds=row['end_frame']/ep.fps)
            for record in boundary_records:
                handoff=analysis['handoffs'][record['boundary_index']]
                handoff.update(seconds=record['frame']/ep.fps,observation=record['observation'],needs_review=True,reason='结合视频上下文及关节状态选择动作起点；仍待人工确认')
            new['provenance']['visual_boundary_resolution']={'at':time.time(),'boundaries':boundary_records,'evidence':evidence,'human_reviewed':False}
            analysis['visual_boundary_resolution']=new['provenance']['visual_boundary_resolution']

        for key,record in correction.get('phases',{}).items():
            if key not in {'first_harvest','second_harvest'}:raise ValueError('未知采摘轮次')
            row=new['segments'][1 if key=='first_harvest' else 3];lo=row['start_frame']/ep.fps;hi=row['end_frame']/ep.fps
            intervals=record['release_intervals_seconds'];times=[]
            if not intervals:raise ValueError('没有已观察的投放区间')
            for a,b in intervals:
                if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (a,b)) or not lo<=a<=b<hi:raise ValueError('释放区间超出采摘阶段')
                if times and a<=intervals[len(times)-1][1]:raise ValueError('投放区间重叠或乱序')
                times.append((a+b)/2)
            complete=record.get('complete') is True
            analysis['harvest_summary'][key]={'successful_count':len(times) if complete else None,'delivery_seconds':times}
            records[key]={'release_intervals_seconds':intervals,'complete':complete,'time_semantics':'midpoint_of_observed_release_bracket_not_exact_frame'}
        if not records and not boundary_records:raise ValueError('没有校正项')
        resolution={'policy':'visual_delivery_brackets_v1','at':time.time(),'reason':correction['reason'],'evidence':evidence,'phases':{**original['provenance'].get('visual_timing_resolution',{}).get('phases',{}),**records},'human_reviewed':False,'network_requests':0}
        new['provenance']['visual_timing_resolution']=resolution
        analysis['visual_timing_resolution']=resolution
        analysis['review_notes'].append('视频计时校正：'+correction['reason']+'；投放时间为可见释放区间中点，非逐帧精确释放时刻；'+('相关边界已结合视频上下文校正' if boundary_records else '阶段边界未改')+'，仍待人工审核。')
        audit_notes(new,ep.fps)
        new['warning']='投放计时已依据主视频和左腕视频校正，待人工复核。'+new['warning']
        new['provenance']['warning']=new['warning'];new['created_at']=time.time()
        atomic_json(ws.root/'coarse-proposal-history'/id_/(str(time.time_ns())+'.json'),original)
        atomic_json(path,new)
        return {'id':id_,'state':'ready','resolution':resolution,'notes':new['provenance']['local_boundary_review_notes']}


def refine_batch(ws,ids,progress=None):
    """Sequential local-only draft calibration; failures never stop later rows."""
    import uuid
    batch={'id':uuid.uuid4().hex,'state':'running','total':len(ids),'episodes':[],'network_requests':0,'started_at':time.time()}
    path=ws.root/'local-boundary-batches'/(batch['id']+'.json')
    atomic_json(path,batch)
    entries={e['id']:e for e in ws.catalog()['episodes']}
    for id_ in ids:
        try:
            with ws.lock,ws.store.lock:
                entry=entries.get(id_)
                if not entry or not entry.get('local_calibration_eligible') or ws.stage_path(id_,'coarse').exists():
                    result={'id':id_,'state':'preserved','reason':'已保存、已校准、无有效五阶段草稿或不在适用范围'}
                else:result=None
            if result is None:result=refine(ws,id_)
        except Exception as exc:result={'id':id_,'state':'failed','error':str(exc)}
        batch['episodes'].append(result);atomic_json(path,batch)
        if progress:progress(len(batch['episodes']),len(ids))
    batch.update(state='completed',finished_at=time.time())
    batch['calibrated']=sum(e['state']=='ready' for e in batch['episodes'])
    batch['preserved']=sum(e['state']=='preserved' for e in batch['episodes'])
    batch['failed']=sum(e['state']=='failed' for e in batch['episodes'])
    atomic_json(path,batch)
    return batch
