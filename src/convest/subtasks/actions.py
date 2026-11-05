"""Small action vocabulary; exceptions belong to independent review events."""
from .core import segment, validate_segments

ACTION_LABELS = ('unknown','grasp_truss','lift_truss','grasp_fruit','pick_and_place','return_truss','rotate_stem','return_stem')
REMOVED = {'fruit_drop','harvest_retry','harvest_uncertain','stop','idle','retry'}


def simplify(segments, length, fps, evidence=()):
    """Collapse historical helper stages without manufacturing successful cycles.

    Uncertain old spans retain the proposed main action when available. The
    original spans are retained in provenance by callers; review is not granted.
    """
    source=validate_segments(segments,length,fps);out=[];changes=[]
    for s in source:
        label=s['label']
        if label in REMOVED:
            rows=[e for e in evidence if e.get('start_frame',length)<s['end_frame'] and e.get('end_frame',0)>s['start_frame']]
            votes={k:0 for k in ('grasp_fruit','pick_and_place')}
            if label=='harvest_uncertain':
                for e in rows:
                    proposed=e.get('claimed_label')
                    if proposed in votes:votes[proposed]+=min(e['end_frame'],s['end_frame'])-max(e['start_frame'],s['start_frame'])
            if max(votes.values()):label=max(votes,key=votes.get)
            elif label=='harvest_retry':label='grasp_fruit'
            elif label in {'fruit_drop','harvest_uncertain'}:
                label=out[-1]['label'] if out and out[-1]['label'] in votes else 'grasp_fruit'
            else:
                label=out[-1]['label'] if out else next((x['label'] for x in source if x['label'] in ACTION_LABELS and x['label']!='unknown'),'unknown')
            changes.append({'start_frame':s['start_frame'],'end_frame':s['end_frame'],'previous_label':s['label'],'label':label})
            s={**s,'label':label,'reviewed':False};s.pop('model_confidence',None)
        if out and out[-1]['label']==label:
            out[-1]['end_frame']=s['end_frame'];out[-1]['reviewed']=out[-1]['reviewed'] and s['reviewed']
        else:out.append(dict(s))
    return validate_segments(out,length,fps),changes
