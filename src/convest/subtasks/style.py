"""Human-reviewed transition examples teach granularity, never target timings."""
from .template import images_at


def transition_examples(documents,target):
    result=[];seen=set()
    for id_,doc in documents.items():
        source=doc['source']
        if id_==target.id or doc['status']!='reviewed' or not all(s['reviewed'] for s in doc['segments']):continue
        if source.get('source_recording_id') and source.get('source_recording_id')==target.entry.get('source_recording_id'):continue
        from .core import Episode
        reference=Episode(source['dataset'],source['episode_index'])
        if reference.identity()!=source:raise ValueError('人工参考的来源指纹已变化')
        rows=doc['segments'];space=doc.get('stage','fine')
        if space not in {'fine','coarse'}:continue
        for a,b in zip(rows,rows[1:]):
            pair=(a['label'],b['label'])
            allowed=({('pickup','harvest'),('harvest','adjust'),('adjust','harvest'),('harvest','return'),('return','adjust'),('adjust','pickup')} if space=='coarse' else {('grasp_fruit','pick_and_place'),('rotate_stem','grasp_truss')})
            if pair not in allowed or (space,pair) in seen:continue
            frame=a['end_frame'];fps=reference.fps
            indices=sorted({max(a['start_frame'],frame-round(fps)),frame-1,frame,
                            min(b['end_frame']-1,frame+round(fps)),
                            min(b['end_frame']-1,frame+round(10*fps) if pair[0]=='rotate_stem' else frame+round(2*fps))})
            assets=images_at(reference,indices,panel_size=480)
            for asset in assets:
                result.append({**asset,'source_id':id_,'source_revision':doc['revision'],'transition':list(pair),
                    'label_space':space,'source_stage':space,'relative_seconds':(asset['frame']-frame)/fps,'label':a['label'] if asset['frame']<frame else b['label']})
            seen.add((space,pair))
    return result


def content(examples):
    result=[]
    for e in examples:
        result.extend([{'type':'input_text','text':f"HUMAN STYLE EXAMPLE, separate recording. Label space {e.get('label_space','fine')} with coarse labels describing whole phases only and never asserting a fine grasp or extraction state. Transition {e['transition']}, relative {e['relative_seconds']:.3f}s, HUMAN label {e['label']}. Copy ONLY action meaning, never timestamps, fruit count, hand pose or background. Hand hardware may differ."},
                       {'type':'input_image','image_url':e['data_url']}])
    if examples:result.append({'type':'input_text','text':'END OF HUMAN EXAMPLES. All following frames/video are the target. Target timestamps must come ONLY from target evidence.'})
    return result


def for_video(examples,prompt):
    # Local fruit evidence must not receive whole-phase labels as fine truth.
    return [e for e in examples if not ('LOCAL FINE PASS:' in prompt and e.get('label_space')=='coarse')]

def for_boundaries(examples,pairs):
    from .workspace import COARSE_FROM_FINE
    coarse_pairs={(COARSE_FROM_FINE.get(a,a),COARSE_FROM_FINE.get(b,b)) for a,b in pairs}
    return [e for e in examples if tuple(e['transition']) in (coarse_pairs if e.get('label_space')=='coarse' else pairs)]
