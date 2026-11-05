"""Full boundary audit: contextual search, consecutive source frames, abstention."""
import base64
import json
from pathlib import Path
from .core import atomic_json, validate_segments


def parse_observations(response,tasks):
    from .harvest import review_boundaries
    from .template import text_json
    result=review_boundaries(response,tasks)
    try:
        raw=text_json(response)['boundaries']
        ids=[r['id'] for r in raw]
        if any(type(i) is not int for i in ids) or sorted(ids)!=sorted(t['id'] for t in tasks):return result
        rows={row['id']:row for row in raw}
    except (ValueError,TypeError,KeyError,AttributeError):return result
    for task in tasks:
        item=result[task['id']];states=rows[task['id']].get('states',[])
        observed={};valid=True
        for s in states:
            f=s.get('frame')
            if type(f) is not int or f not in task['frames'] or f in observed or s.get('side') not in {'before','after','occluded'}:
                valid=False;break
            observed[f]=s
        frame=item.get('frame');frames=task['frames']
        if frame not in frames or frames.index(frame)==0:valid=False
        if valid:
            j=frames.index(frame)
            valid=(len(observed)==len(frames) and observed[frames[j-1]]['side']=='before' and observed[frame]['side']=='after'
                   and all(observed[f]['side']!='after' for f in frames[:j]))
        if task.get('outcome_frames'):
            outcomes=rows[task['id']].get('outcome_observations',[])
            seen=set();supported=False;outcome_valid=True
            for observation in outcomes:
                f=observation.get('frame')
                if type(f) is not int or f not in task['outcome_frames'] or f in seen:
                    outcome_valid=False;break
                seen.add(f)
                supported |= observation.get('attachment')=='detached' and observation.get('retention')=='held' and observation.get('single_fruit') is True
            supported=outcome_valid and len(seen)==len(task['outcome_frames']) and supported
            item['outcome_observations']=outcomes;item['single_fruit_detachment_supported']=supported
            if not supported:
                valid=False;item['reason']='未看见同一目标果实独立脱离并被握持，不能把准备/失败拧动当成正常摘下'
        item['before_after_witness']=valid
        item['states']=states
        if not valid:item.update(frame=None,needs_review=True)
    return result


def refine_all(episode,segments,run,client,record,records,max_requests=128,progress=None):
    """Audit every boundary. No confidence-only acceptance, no subframe interpolation.

    Broad sampled context locates a candidate; acceptance requires consecutive
    original frames. A failed window gets one wider search and another source
    pass. Failed transitions keep their original candidate and explicit flags.
    """
    from .template import RULES,event_name,images_at,left_motion_candidate
    result=validate_segments(segments,episode.length,episode.fps)
    ends=[s['end_frame'] for s in result[:-1]];evidence={};motion={}
    run=Path(run);run.mkdir(parents=True,exist_ok=True)
    prompt=RULES+'''\nAudit these SEPARATE action transitions using every supplied source-frame image.
For EACH image output {frame, side: before|after|occluded}. BEFORE means the named
before action is still occurring; AFTER means the named next action has begun.
Do not repeat a suggested timestamp. Choose FIRST AFTER, with previous image BEFORE.
If images begin already AFTER, or the transition is obscured, return null.
For grasp_fruit -> pick_and_place also classify EVERY image's phase
approach|grip_secured|extract|detached_transport|occluded, attachment
attached|detached|occluded, retention held|empty|falling|occluded.
The transition is SECURED GRIP BEFORE purposefully pulling -> EXTRACTION BEGINS.
Do not wait for detached transport, but require the FINAL extraction that actually detaches the target. Earlier loosening/failed pulls stay BEFORE. Do not confuse finger closing with extraction.
Use cam0 for arm movement, both wrist views for grip, stem connection and retention.
Moving an empty hand to the tray is an anomaly, not proof of a fruit delivery.
For extraction tasks, additional OUTCOME context images show the proposed pick interval after the onset. Track the SAME target and require ONE independent fruit still retained by the left hand. Output outcome_observations for ALL task.outcome_frames: {frame,attachment,retention,single_fruit:true|false}. If all remain attached, the hand is empty, a whole truss is taken, or retention is obscured, do not certify a normal pick: return null. Context frames are NOT candidate boundary frames.
All results are model hypotheses requiring human review. Confidence is not accuracy.
Return only {"boundaries":[{"id":0,"first_after_frame":123|null,"confidence":0.8,
"states":[{"frame":122,"side":"before","phase":"grip_secured","attachment":"attached","retention":"held"},...]}]}.
'''
    def check(ids,stage,*,wide=False,dense=False):
        batch_size=1 if wide or dense else 2
        for offset in range(0,len(ids),batch_size):
            if len(records)>=max_requests:raise ValueError('完整逐帧复核超过请求预算，未发布半条结果')
            batch=ids[offset:offset+batch_size];groups=[]
            for local,i in enumerate(batch):
                center=ends[i];lower=ends[i-1]+1 if i else 1;upper=ends[i+1]-1 if i+1<len(ends) else episode.length-1
                radius=12 if dense else round(2*episode.fps)
                if wide:radius=round(6*episode.fps)
                lo=max(lower,center-radius);hi=min(upper,center+radius)
                stride=1 if dense else max(1,round(episode.fps/5))
                frames=sorted(set(range(lo,hi+1,stride))|{center,hi})
                task={'id':local,'event':event_name(result[i]['label'],result[i+1]['label']),
                      'before':result[i]['label'],'after':result[i+1]['label'],'candidate_frame':center,'frames':frames}
                if i in motion:
                    task['left_motion_cue']=motion[i]
                    task['cue_meaning']='FIRST sustained LEFT arm movement after a stationary prefix. Preparation/approach starts here, not at first contact or later hand arrival. If visual interpretation conflicts, abstain.'
                context=[]
                if task['event']=='first_extraction_motion':
                    stop=ends[i+1] if i+1<len(ends) else episode.length
                    width=stop-center
                    context=sorted({min(stop-1,center+max(1,width//3)),min(stop-1,center+max(1,2*width//3)),stop-1})
                    task['outcome_frames']=context
                assets=images_at(episode,sorted(set(frames)|set(context)),panel_size=720)
                for asset in assets:asset['role']='candidate' if asset['frame'] in frames else 'outcome'
                folder=run/stage/str(i);folder.mkdir(parents=True,exist_ok=True)
                for asset in assets:(folder/f"{asset['frame']}.jpg").write_bytes(base64.b64decode(asset['data_url'].split(',',1)[1]))
                groups.append({'task':task,'assets':assets})
            response,key,cached=client.call_images(groups,prompt+'\n'+json.dumps([g['task'] for g in groups]))
            record(response,key,cached,stage)
            updates=parse_observations(response,[g['task'] for g in groups])
            for local,item in updates.items():
                i=batch[local]
                item['input_frames']=groups[local]['task']['frames']
                if i in motion and item.get('frame') is not None and abs(item['frame']-motion[i])>max(1,round(.1*episode.fps)):
                    item.update(frame=None,needs_review=True,motion_visual_conflict=True,reason='视觉建议偏离首次左臂准备运动起点，不能把后续接触当作开始')
                if dense:item['consecutive_source_frames']=all(b==a+1 for a,b in zip(item['input_frames'],item['input_frames'][1:]))
                evidence.setdefault(str(i),{})[stage]=item
                if not item['needs_review']:ends[i]=item['frame']
            atomic_json(run/'progress.json',{'boundaries':ends,'evidence':evidence,'requests':records})
            if progress:progress(len(records),max(len(records),len(ids)))
    ids=list(range(len(ends)))
    check(ids,'event_window')
    for i in ids:
        if event_name(result[i]['label'],result[i+1]['label'])=='first_left_approach':
            cue=left_motion_candidate(episode,ends[i],lower_bound=ends[i-1]+1 if i else 1,upper_bound=ends[i+1] if i+1<len(ends) else episode.length-1)
            if cue is not None and (ends[i-1] if i else 0)<cue<(ends[i+1] if i+1<len(ends) else episode.length):
                motion[i]=cue;ends[i]=cue
    check(ids,'source_frame',dense=True)
    unresolved=[i for i in ids if evidence[str(i)]['source_frame']['needs_review']]
    if unresolved:
        check(unresolved,'expanded_window',wide=True)
        check(unresolved,'source_retry',dense=True)
        for i in unresolved:evidence[str(i)]['source_frame']=evidence[str(i)]['source_retry']
    for i,s in enumerate(result):s.update(start_frame=ends[i-1] if i else 0,end_frame=ends[i] if i<len(ends) else episode.length)
    result=validate_segments(result,episode.length,episode.fps)
    return result,evidence,motion
