"""Full boundary audit: contextual search, consecutive source frames, abstention."""
import base64
import json
from pathlib import Path
from .core import atomic_json, validate_segments


def hand_settling_cue(episode,start,lift_frame):
    """Read-only posture cue, not grip/force ground truth or a cleaning rule.

    Compare sustained finger motion with this attempt's own closing activity
    and subsequent holding noise. Ignore isolated position spikes. A disagreement
    with RGB must remain unverified, never become an automatic confirmation.
    """
    import numpy as np
    from convest.training.adatempo.lerobot_io import values
    names=getattr(episode,'info',{}).get('features',{}).get('observation.state',{}).get('names',[])
    cols=[i for i,n in enumerate(names) if n.startswith('right_') and not n.startswith('right_fr3') and n.endswith('.position')]
    if len(cols)<4:return None
    q=values(episode.table,'observation.state')[:,cols]
    delta=np.linalg.norm(np.diff(q,axis=0),axis=1)
    lo=max(0,start);hi=min(lift_frame,len(delta)-3)
    if hi-lo<5:return None
    noise=float(np.median(delta[hi:min(len(delta),hi+max(3,round(.3*episode.fps)))]))
    threshold=max(1e-4,2*noise,.1*float(np.percentile(delta[lo:hi],95)))
    active=delta[lo:hi]>threshold
    sustained=active & (np.r_[False,active[:-1]] | np.r_[active[1:],False])
    indices=np.flatnonzero(sustained)
    if not len(indices):return None
    frame=lo+int(indices[-1])+2
    if frame+max(2,round(.1*episode.fps))>hi+1:return None
    return {'frame':frame,'joint_count':len(cols),'relative_motion_threshold':threshold,
            'holding_noise':noise,'lift_context_frame':lift_frame,'not_grip_truth':True}


def parse_observations(response,tasks):
    from .harvest import review_boundaries
    from .template import text_json, parse_boundaries
    contexts={}
    try:
        payload=text_json(response);by_id={t['id']:t for t in tasks}
        for row in payload.get('boundaries',[]):
            task=by_id.get(row.get('id'))
            if not task:continue
            extras=[s for s in row.get('states',[]) if type(s.get('frame')) is int and s['frame'] in task.get('outcome_frames',[]) and s['frame'] not in task['frames']]
            if extras:
                contexts[task['id']]=extras
                row['states']=[s for s in row['states'] if s not in extras]
        response={**response,'output_text':json.dumps(payload)}
    except (ValueError,TypeError,AttributeError):pass
    try:localizations=parse_boundaries(response,tasks)
    except (ValueError,TypeError,AttributeError):localizations={}
    result=review_boundaries(response,tasks)
    try:
        raw=text_json(response)['boundaries']
        ids=[r['id'] for r in raw]
        if any(type(i) is not int for i in ids) or sorted(ids)!=sorted(t['id'] for t in tasks):return result
        rows={row['id']:row for row in raw}
    except (ValueError,TypeError,KeyError,AttributeError):return result
    for task in tasks:
        item=result[task['id']];states=rows[task['id']].get('states',[])
        location=localizations.get(task['id'],{})
        if not location.get('needs_review',True) and not item.get('schema_error'):
            item['localization_frame']=location['frame']
            item['localization_only']=True
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
        if valid and task['event']=='first_extraction_motion':
            # A model may label an already-started extraction BEFORE a later
            # timestamp. Do not accept that internally contradictory witness.
            earlier=any(observed[f].get('phase') in {'extract','detached_transport'} for f in frames[:j])
            reversal=any(observed[f].get('phase') in {'approach','grip_secured'} for f in frames[j:])
            if earlier or reversal:
                valid=False;item['reason']='物理状态与分界矛盾：较早帧已开始拽脱，或分界后仍回到抓握；不能证实这是最终拽脱起点'
        if valid and task['event']=='grasp_truss -> lift_truss':
            previous=observed[frames[j-1]];current=observed[frame]
            secured=previous.get('right_grip')=='secured' and current.get('right_grip')=='secured'
            onset=previous.get('right_motion')=='still' and current.get('right_motion')=='lift_onset'
            earlier=any(observed[f].get('right_motion') in {'lift_onset','lifting'} for f in frames[:j])
            reversal=any(observed[f].get('right_grip') in {'approaching','closing'} for f in frames[j:])
            valid=secured and onset and not earlier and not reversal
            item['right_lift_onset_proven']=valid
            if not valid:item['reason']='未证实握稳后开始提起的第一帧；静止握持或已经持续提起均不能代替起点'
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
            extra=contexts.get(task['id'],[])
            if extra:
                item['context_states']=extra
                keyed={s['frame']:s for s in outcomes}
                if len({s['frame'] for s in extra})!=len(extra) or any(
                    s['frame'] not in keyed or any(s.get(k)!=keyed[s['frame']].get(k) for k in ('attachment','retention')) for s in extra):
                    supported=False
                    item['reason']='同一后续画面的连接或持果状态相互矛盾，不能证实正常摘下'
            item['outcome_observations']=outcomes;item['single_fruit_detachment_supported']=supported
            if not supported:
                valid=False;item.setdefault('reason','未看见同一目标果实独立脱离并被握持，不能把准备/失败拧动当成正常摘下')
        item['before_after_witness']=valid
        item['states']=states
        if not valid:item.update(frame=None,needs_review=True)
    return result


def boundary_prompt(in_hand=False):
    from .template import rules_for
    return rules_for(in_hand)+'''\nAudit these SEPARATE action transitions using every supplied source-frame image.
For EACH image output {frame, side: before|after|occluded}. BEFORE means the named
before action is still occurring; AFTER means the named next action has begun.
Do not repeat a suggested timestamp. Choose FIRST AFTER, with previous image BEFORE.
If images begin already AFTER, or the transition is obscured, return null.
No previous candidate frame is supplied. Locate the EARLIEST supported physical
onset, not the middle image. Do not rename already-active motion as BEFORE.
For grasp_truss -> lift_truss classify EVERY image's right_grip as
approaching|closing|secured|occluded and right_motion as
still|lift_onset|lifting|occluded. Secured grip and lift onset are DIFFERENT events.
The boundary is SECURED grip with right_motion STILL -> SECURED grip with the
FIRST lift_onset. Holding securely while stationary remains grasp_truss, even if
it lasts several seconds. Do not cut at finger closure or merely preparing to
lift. Do not wait for obvious lifting or tray clearance either: choose the first
actual upward/central-transfer motion of the held truss. Later frames already
lifting cannot substitute for its onset. A grip-settling cue is auxiliary finger
posture evidence ONLY; it is neither a lifting timestamp nor a boundary target.
For grasp_fruit -> pick_and_place also classify EVERY image's phase
approach|grip_secured|extract|detached_transport|occluded, attachment
attached|detached|occluded, retention held|empty|falling|occluded.
The transition is SECURED GRIP BEFORE purposefully pulling -> EXTRACTION BEGINS.
Track the LAST EFFECTIVE GRASP ATTEMPT backward from retained detachment. Choose its FIRST purposeful extraction effort, including the first sustained twist/pull even if the uninterrupted grip continues twisting/pulling for many seconds. Do not pick only the final strong jerk just before the stem breaks. Earlier missed reaches or regrips before this secured attempt stay BEFORE; sustained extraction with unchanged secured grip is AFTER. Do not confuse finger closing with extraction.
Use cam0 for arm movement, both wrist views for grip, stem connection and retention.
Moving an empty hand to the tray is an anomaly, not proof of a fruit delivery.
For extraction tasks, additional OUTCOME context images show the proposed pick interval after the onset. Track the SAME target and require ONE independent fruit still retained by the left hand. Output outcome_observations for ALL task.outcome_frames: {frame,attachment,retention,single_fruit:true|false}. If all remain attached, the hand is empty, a whole truss is taken, or retention is obscured, do not certify a normal pick: return null. Context frames are NOT candidate boundary frames.
All results are model hypotheses requiring human review. Confidence is not accuracy.
Return only {"boundaries":[{"id":0,"first_after_frame":123|null,"confidence":0.8,
"states":[{"frame":122,"side":"before","phase":"grip_secured","attachment":"attached","retention":"held"},...]}]}.
'''


def refine_all(episode,segments,run,client,record,records,max_requests=128,progress=None,*,coarse_labels=None,workflow_in_hand=None):
    """Audit every boundary. No confidence-only acceptance, no subframe interpolation.

    Broad sampled context locates a candidate; acceptance requires consecutive
    original frames. A failed window gets one wider search and another source
    pass. Failed transitions keep their original candidate and explicit flags.
    """
    from .template import event_name,images_at,left_motion_candidate
    result=validate_segments(segments,episode.length,episode.fps)
    ends=[s['end_frame'] for s in result[:-1]];evidence={};motion={}
    run=Path(run);run.mkdir(parents=True,exist_ok=True)
    if workflow_in_hand is None:
        from .workflow import in_hand_episode
        workflow_in_hand=in_hand_episode(episode)
    prompt=boundary_prompt(workflow_in_hand)
    if coarse_labels is not None:
        if list(coarse_labels)!=['pickup','harvest','return','adjust','pickup','harvest','return'] or len(result)!=7:
            raise ValueError('粗阶段复核必须是七阶段六个边界')
        prompt+='\nCOARSE PHASE AUDIT: before/after denote entire large phases, NOT individual fine actions.\nfirst_left_approach (pickup -> harvest): the first sustained LEFT arm preparation/approach toward fruit after the right hand brings the held truss to the center. Do not wait for left contact or successful grip. Inspect cam0 and both wrist views.\ncoarse_harvest_to_return: the RIGHT hand starts deliberately returning the entire truss or bare stem toward the right plate after the harvesting block; left-hand basket trips, missed picks or empty-hand returns are still harvest, not this boundary.\ncoarse_return_to_adjust: right-hand return/release has finished and purposeful finger repositioning of the truss on the right plate BEGINS. Do not use the middle of the adjustment.\ncoarse_adjust_to_pickup: adjustment has ended and the RIGHT hand begins the new grasp/pickup attempt. This is the start of grasping, not the later lift onset, since grasp and lift are one coarse pickup phase.\nFor every event, choose the first physically supported AFTER frame with an immediately preceding BEFORE witness in the dense pass. A high confidence or a sampled overview is not dense evidence. If unclear, abstain.\n'
    def event_at(i):
        if coarse_labels is None:return event_name(result[i]['label'],result[i+1]['label'])
        return ['first_left_approach','coarse_harvest_to_return','coarse_return_to_adjust',
                'coarse_adjust_to_pickup','first_left_approach','coarse_harvest_to_return'][i]
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
                event=event_at(i)
                if not dense and (coarse_labels is not None or event in {'first_extraction_motion','grasp_truss -> lift_truss'}):
                    # Start localization from the complete neighboring actions,
                    # not from a narrow window around a potentially late guess.
                    lo,hi=lower,upper
                    cap=60 if wide else 40
                    stride=max(1,round(episode.fps/(10 if wide else 5)),(hi-lo+cap-1)//cap)
                frames=sorted(set(range(lo,hi+1,stride))|{hi})
                if dense or event not in {'first_extraction_motion','grasp_truss -> lift_truss'}:frames=sorted(set(frames)|{center})
                task={'id':local,'event':event,
                      'before':('coarse_'+coarse_labels[i]) if coarse_labels is not None else result[i]['label'],'after':('coarse_'+coarse_labels[i+1]) if coarse_labels is not None else result[i+1]['label'],'frames':frames}
                if i in motion:
                    task['left_motion_cue']=motion[i]
                    task['cue_meaning']='FIRST sustained LEFT arm movement after a stationary prefix. Preparation/approach starts here, not at first contact or later hand arrival. If visual interpretation conflicts, abstain.'
                if i in hand_cues:
                    task['right_hand_settling_cue']=hand_cues[i]
                    task['hand_cue_meaning']='Independent finger posture settling after sustained closing, not grip/force truth. An earlier apparent contact while fingers still substantially close cannot alone prove a stable grip. This cue is not lift onset. Do not set the boundary to its timestamp; use actual truss lifting motion.'
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
                if i in hand_cues:
                    item['right_hand_settling_cue']=hand_cues[i]
                if dense:item['consecutive_source_frames']=all(b==a+1 for a,b in zip(item['input_frames'],item['input_frames'][1:]))
                evidence.setdefault(str(i),{})[stage]=item
                if not item['needs_review']:ends[i]=item['frame']
                elif not dense and item.get('localization_frame') is not None:
                    # A broad hypothesis can center the NEXT source-frame
                    # inspection. It cannot satisfy the dense witness itself.
                    ends[i]=item['localization_frame']
            atomic_json(run/'progress.json',{'boundaries':ends,'evidence':evidence,'requests':records})
            if progress:progress(len(records),max(len(records),len(ids)))
    ids=list(range(len(ends)))
    hand_cues={}
    check(ids,'event_window')
    for i in ids:
        if event_at(i)=='grasp_truss -> lift_truss':
            states=evidence[str(i)]['event_window'].get('states',[])
            lifts=[s['frame'] for s in states if s.get('right_motion') in {'lift_onset','lifting'}]
            if lifts:
                cue=hand_settling_cue(episode,ends[i-1] if i else 0,min(lifts))
                if cue and (ends[i-1] if i else 0)<cue['frame']<(ends[i+1] if i+1<len(ends) else episode.length):
                    hand_cues[i]=cue
        if event_at(i)=='first_left_approach':
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
