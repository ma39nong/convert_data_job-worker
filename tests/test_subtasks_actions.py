import json
from convest.subtasks.actions import ACTION_LABELS, REMOVED, simplify
from convest.subtasks.core import segment
from convest.subtasks.harvest import parse_proven_boundaries


def test_helper_stages_collapse_without_confirming_success():
    labels=['grasp_fruit','harvest_retry','harvest_uncertain','pick_and_place','fruit_drop','stop','idle','retry']
    rows=[segment(i*10,(i+1)*10,label,reviewed=False) for i,label in enumerate(labels)]
    result,changes=simplify(rows,80,30,[{'start_frame':20,'end_frame':30,'claimed_label':'grasp_fruit'}])
    assert len(changes)==len(REMOVED)
    assert [s['label'] for s in result]==['grasp_fruit','pick_and_place']
    assert result[0]['start_frame']==0 and result[-1]['end_frame']==80
    assert all(s['label'] in ACTION_LABELS and not s['reviewed'] for s in result)
    assert rows[1]['label']=='harvest_retry'


def test_extraction_onset_precedes_actual_separation():
    task={'id':0,'event':'first_extraction_motion','frames':[491,492,493]}
    def check(phase='extract',retention='held'):
        states=[{'frame':f,'phase':'grip_secured' if f<493 else phase,'attachment':'attached','retention':retention} for f in task['frames']]
        response={'output_text':json.dumps({'boundaries':[{'id':0,'first_after_frame':493,'confidence':.9,'states':states}]})}
        return parse_proven_boundaries(response,[task])[0]
    assert check()['extraction_onset_proven'] and not check()['needs_review']
    assert not check()['attachment_transition_proven']
    assert check('detached_transport')['needs_review']
    assert check(retention='empty')['needs_review']
