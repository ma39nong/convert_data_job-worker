"""Local motion evidence for fruit release; never semantic success or approval."""
import hashlib
import json
import time
import numpy as np
from .core import atomic_json, values
from .fine_cycles import binding

POLICY='fine-left-arm-finger-evidence-v1'

def fingerprint(fine):
    return hashlib.sha256(json.dumps({'segments':fine['segments'],'revision':fine['revision'],
        'proposal_key':fine.get('proposal_key'),'coarse_binding':fine.get('coarse_binding')},sort_keys=True).encode()).hexdigest()

def analyze(ep,coarse,fine):
    names=ep.info['features'].get('observation.state',{}).get('names',[])
    arm=[i for i,n in enumerate(names) if n.startswith('left_fr3_joint') and n.endswith('.position')]
    fingers=[i for i,n in enumerate(names) if n.startswith('left_') and '_fr3_' not in n and n.endswith('.position')]
    report={'policy':POLICY,'network_requests':0,'boundaries':[],'extra_motion_events':[],
        'automatic_changes':0,'notes':[],'thresholds':{'arm_max_delta_rad':.01,'arm_pose_distance_rad':.15,'event_group_gap_seconds':.3,'finger_noise_floor_rad':1e-4,'search_seconds':2},
        'semantic_release_proven':False}
    if len(arm)!=7 or len(fingers)<2:
        report['notes']=['缺少左臂七关节或手指位置，未执行细边界运动校验。'];return report
    q=values(ep.table,'observation.state')
    if len(q)!=ep.length or not np.isfinite(q[:,arm+fingers]).all():
        report['notes']=['关节状态无效，保留全部细边界。'];return report
    a=q[:,arm];f=q[:,fingers]
    ad=np.r_[0,np.max(np.abs(np.diff(a,axis=0)),axis=1)]
    fd=np.r_[0,np.max(np.abs(np.diff(f,axis=0)),axis=1)]
    # Absolute joint deltas reveal a finger event, not its opening direction.
    threshold=max(1e-4,float(np.median(fd)+4*np.median(np.abs(fd-np.median(fd)))))
    report['thresholds']['finger_event_delta_rad']=threshold
    radius=max(1,round(2*ep.fps));gap=max(1,round(.3*ep.fps))
    for phase,parent in enumerate(coarse['segments'],1):
        if parent['label']!='harvest':continue
        children=[r for r in fine['segments'] if parent['start_frame']<=r['start_frame']<parent['end_frame']]
        cuts=[r['end_frame'] for r in children[:-1]]
        anchors=cuts[:]
        model=next((h for h in (fine.get('model_analysis') or {}).get('harvests',[]) if h.get('coarse_phase_index')==phase),{})
        anchors += [round(c['delivery_seconds']*ep.fps) for c in model.get('cycles',[]) if isinstance(c.get('delivery_seconds'),(float,int)) and not isinstance(c.get('delivery_seconds'),bool) and np.isfinite(c['delivery_seconds']) and parent['start_frame']<c['delivery_seconds']*ep.fps<parent['end_frame']]
        anchors=[x for x in anchors if parent['start_frame']<x<parent['end_frame']]
        if not anchors:continue
        # Each delivery can occur over a different tray compartment. Do not average
        # unlike arm configurations into an unreachable 'tray pose'.
        # Similarity is a search hint, not FK or proof the hand is above the tray.
        near=np.zeros(ep.length,dtype=bool)
        for anchor in anchors:
            near |= np.max(np.abs(a-a[anchor]),axis=1)<=.15
        events=np.flatnonzero((fd>threshold)&near&(ad<.01))
        events=[int(x) for x in events if parent['start_frame']<x<parent['end_frame']]
        groups=[]
        for x in events:
            if not groups or x-groups[-1][-1]>gap:groups.append([x])
            else:groups[-1].append(x)
        candidates=[{'start_frame':g[0],'end_frame':g[-1]+1,'frame':max(g,key=lambda x:fd[x])} for g in groups]
        for index,cut in enumerate(cuts,1):
            options=[c for c in candidates if c['end_frame']>=cut-radius and c['start_frame']<=cut+radius]
            state='no_candidate' if not options else 'multiple_candidates' if len(options)>1 else 'candidate_only'
            report['boundaries'].append({'coarse_phase_index':phase,'fruit_boundary':index,'original_frame':cut,
                'candidate_windows':options,'state':state,'needs_review':True})
            times='、'.join(f"{c['start_frame']/ep.fps:.2f}–{c['end_frame']/ep.fps:.2f}秒" for c in options)
            report['notes'].append(f'第{phase}粗阶段第{index}颗后边界 {cut/ep.fps:.2f}秒：'+
                ('附近手指运动候选 '+times+'；需用视频确认释放入盘。' if options else '附近未找到可靠手指运动候选，需看片复核。'))
        for c in candidates:
            if all(abs(c['frame']-x)>radius for x in anchors):
                report['extra_motion_events'].append({'coarse_phase_index':phase,**c})
        if any(e['coarse_phase_index']==phase for e in report['extra_motion_events']):
            report['notes'].append(f'第{phase}粗阶段还有未对应模型投放的手指运动，可能是漏分或空手/重复动作，请看片；不自动增加颗数。')
    report['notes'].append('本地关节/手指校验未证明释放或成功投放，未自动改边界；末颗仍保持粗阶段终点。')
    return report

def audit(ws,id_):
    from .fine_cycles import reviewed_coarse
    from .quarantine import assert_included
    with ws.lock,ws.store.lock:
        assert_included(ws,id_);ep=ws.store.episode(id_);coarse=reviewed_coarse(ws,ep);fine=ws.stages(ep)['fine']
        if not fine.get('fine_profile') or fine.get('needs_fine_generation') or fine.get('coarse_binding_stale'):
            raise ValueError('请先生成与当前已审核粗阶段一致的单颗细标注')
        source=ep.identity();key=fingerprint(fine)
    report=analyze(ep,coarse,fine)
    report.update(source=source,fine_sha256=key,coarse_binding=binding(coarse),created_at=time.time())
    with ws.lock,ws.store.lock:
        if ep.identity()!=source or fingerprint(ws.stages(ep)['fine'])!=key or binding(ws.stages(ep)['coarse'])!=binding(coarse):
            raise ValueError('校验期间来源或标注变化，未保存过期候选')
        atomic_json(ws.root/'fine-local-evidence'/(id_+'.json'),report)
    return {'id':id_,'state':'ready','fine_local':True,'network_requests':0,'automatic_changes':0,'boundaries':len(report['boundaries'])}
