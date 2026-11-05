import json
from convest.subtasks.boundaries import parse_observations


def test_every_boundary_requires_complete_before_after_witness():
    task={'id':0,'event':'return_truss -> rotate_stem','frames':[50,51,52]}
    def check(sides):
        rows=[{'frame':f,'side':s} for f,s in zip(task['frames'],sides)]
        return parse_observations({'output_text':json.dumps({'boundaries':[{'id':0,'first_after_frame':51,'confidence':.99,'states':rows}]})},[task])[0]
    assert not check(['before','after','after'])['needs_review']
    assert check(['after','after','after'])['needs_review']
    assert check(['before','occluded','after'])['needs_review']
    assert check(['before','after'])['needs_review']


def test_extraction_needs_semantics_and_before_after_sides():
    task={'id':0,'event':'first_extraction_motion','frames':[80,81]}
    states=[{'frame':80,'side':'before','phase':'grip_secured','attachment':'attached','retention':'held'},
            {'frame':81,'side':'after','phase':'extract','attachment':'attached','retention':'held'}]
    def check():return parse_observations({'output_text':json.dumps({'boundaries':[{'id':0,'first_after_frame':81,'confidence':.9,'states':states}]})},[task])[0]
    assert not check()['needs_review']
    states[1]['phase']='detached_transport'
    assert check()['needs_review']


def test_unresolved_boundary_gets_wider_context_and_new_consecutive_pass(tmp_path,monkeypatch):
    import base64
    from types import SimpleNamespace
    from convest.subtasks import template
    from convest.subtasks.boundaries import refine_all
    from convest.subtasks.core import segment
    monkeypatch.setattr(template,'images_at',lambda ep,frames,**kw:[{'frame':f,'sha256':str(f),'data_url':'data:image/jpeg;base64,'+base64.b64encode(b'x').decode()} for f in frames])
    class Client:
        dense_calls=0
        def call_images(self,groups,prompt):
            rows=[]
            for g in groups:
                t=g['task'];frames=t['frames'];dense=all(b==a+1 for a,b in zip(frames,frames[1:]))
                if dense:self.dense_calls+=1
                assert 'candidate_frame' not in t
                f=next(f for f in frames if f>=80);chosen=None if dense and self.dense_calls==1 else f
                rows.append({'id':t['id'],'first_after_frame':chosen,'confidence':.9,'states':[{'frame':a,'side':'before' if a<f else 'after','right_grip':'secured','right_motion':'still' if a<f else 'lift_onset' if a==f else 'lifting'} for a in frames]})
            return {'output_text':json.dumps({'boundaries':rows})},'key',False
    ep=SimpleNamespace(length=200,fps=30)
    rows=[segment(0,80,'grasp_truss'),segment(80,200,'lift_truss')];records=[]
    def record(*args):records.append(args)
    result,evidence,_=refine_all(ep,rows,tmp_path,Client(),record,records,4)
    assert len(records)==4 and result[0]['end_frame']==80
    assert not evidence['0']['source_frame']['needs_review']
    assert evidence['0']['source_retry']['consecutive_source_frames']
    assert len(evidence['0']['expanded_window']['input_frames'])>len(evidence['0']['event_window']['input_frames'])


def test_extraction_needs_independent_retained_fruit_not_only_a_pull():
    task={'id':0,'event':'first_extraction_motion','frames':[80,81],'outcome_frames':[95,105]}
    states=[{'frame':80,'side':'before','phase':'grip_secured','attachment':'attached','retention':'held'},
            {'frame':81,'side':'after','phase':'extract','attachment':'attached','retention':'held'}]
    outcomes=[{'frame':95,'attachment':'attached','retention':'held','single_fruit':False},
              {'frame':105,'attachment':'attached','retention':'held','single_fruit':False}]
    def check():return parse_observations({'output_text':json.dumps({'boundaries':[{'id':0,'first_after_frame':81,'confidence':.99,'states':states,'outcome_observations':outcomes}]})},[task])[0]
    assert check()['needs_review']
    outcomes[1].update(attachment='detached',single_fruit=True)
    assert not check()['needs_review']
    outcomes[1]['retention']='falling'
    assert check()['needs_review']


def test_late_extraction_cannot_hide_earlier_motion_behind_before_label():
    task={'id':0,'event':'first_extraction_motion','frames':[80,81,82,83]}
    states=[{'frame':f,'side':'before' if f<83 else 'after','phase':'extract' if f in {80,83} else 'grip_secured','attachment':'attached','retention':'held'} for f in task['frames']]
    response={'output_text':json.dumps({'boundaries':[{'id':0,'first_after_frame':83,'confidence':.99,'states':states}]})}
    result=parse_observations(response,[task])[0]
    assert result['frame'] is None and result['needs_review']
    assert '物理状态与分界矛盾' in result['reason']


def test_pickup_requires_first_lift_motion_after_secure_stationary_holding():
    task={'id':0,'event':'grasp_truss -> lift_truss','frames':[74,75,76]}
    states=[{'frame':f,'side':'before' if f<75 else 'after','right_grip':'secured','right_motion':'still' if f<75 else 'lift_onset' if f==75 else 'lifting'} for f in task['frames']]
    def check():return parse_observations({'output_text':json.dumps({'boundaries':[{'id':0,'first_after_frame':75,'confidence':.99,'states':states}]})},[task])[0]
    assert check()['right_lift_onset_proven']
    states[1]['right_motion']='still'
    assert check()['needs_review']  # Secure grip alone is not lifting.
    states[1]['right_motion']='lifting'
    assert check()['needs_review']  # Already lifting does not prove onset.
    states[1]['right_motion']='lift_onset';states[0]['right_motion']='lifting'
    assert check()['needs_review']  # Cannot hide earlier actual movement.
    states[0]['right_motion']='still';states[0]['right_grip']='closing'
    assert check()['needs_review']


def test_quality_report_keeps_human_decisions_and_includes_mapped_events(tmp_path):
    from convest.subtasks.batch_review import enrich_report
    from convest.subtasks.core import atomic_json
    from convest.subtasks.behavior import validate_events
    run=tmp_path/'quality-review'/'run';folder=run/'ep'
    event={'start_frame':10,'end_frame':20,'kind':'empty_hand_place','status':'pending','origin':'model'}
    scanned=validate_events([event],50)
    retained=[{**scanned[0],'status':'dismissed','note':'人工已看片排除'}]
    mapped=validate_events([{**event,'start_frame':30,'end_frame':40}],50)
    atomic_json(run/'annotations-before.json',{'ep':{'source':{'length':50},'review_events':retained}})
    atomic_json(folder/'behavior.json',{'events':scanned})
    atomic_json(folder/'published.json',{'review_events':retained+mapped})
    report={'run_id':'run','episodes':[{'id':'ep','state':'saved','human_preserved':False}]}
    enriched=enrich_report(tmp_path,report)['episodes'][0]
    assert enriched['behavior_events']==retained+mapped
    assert enriched['behavior_scanned_candidates']==1
    assert enriched['behavior_candidates']==1
    # A preserved human reference receives report-only candidates; a previous
    # human dismissal still takes precedence over the repeated model candidate.
    (folder/'published.json').unlink()
    report['episodes'][0]['human_preserved']=True
    enriched=enrich_report(tmp_path,report)['episodes'][0]
    assert enriched['behavior_events']==retained
    assert enriched['behavior_candidates']==0


def test_seeded_boundary_review_does_not_claim_independent_segmentation_accuracy(tmp_path):
    from convest.subtasks.batch_review import enrich_report
    from convest.subtasks.core import atomic_json
    run=tmp_path/'quality-review'/'run'
    atomic_json(run/'annotations-before.json',{'ep':{'source':{'length':50},'segments':[]}})
    atomic_json(run/'ep/proposal.json',{'review_scope':'existing_boundaries','segments':[], 'unresolved_boundaries':[]})
    report={'run_id':'run','episodes':[{'id':'ep','state':'audited_reference','human_preserved':True}]}
    row=enrich_report(tmp_path,report)['episodes'][0]
    assert 'human_reference_comparison' not in row


def test_hand_settling_cue_rejects_early_contact_without_using_user_frame(monkeypatch):
    import numpy as np
    from types import SimpleNamespace
    from convest.subtasks.boundaries import hand_settling_cue
    from convest.training.adatempo import lerobot_io
    delta=np.full(99,.001);delta[5:15]=.1;delta[15:25]=.02
    delta[31]=.07  # An isolated position spike is not a new closing bout.
    q=np.zeros((100,4));q[1:,0]=np.cumsum(delta)
    monkeypatch.setattr(lerobot_io,'values',lambda *args:q)
    ep=SimpleNamespace(fps=30,table=None,info={'features':{'observation.state':{'names':[f'right_finger{i}.position' for i in range(4)]}}})
    cue=hand_settling_cue(ep,0,40)
    assert cue['frame']==26 and cue['not_grip_truth']
    assert cue['joint_count']==4


def test_extra_provided_context_is_not_an_unknown_candidate_but_must_agree():
    task={'id':0,'event':'first_extraction_motion','frames':[80,81],'outcome_frames':[95]}
    states=[{'frame':80,'side':'before','phase':'grip_secured','attachment':'attached','retention':'held'},
            {'frame':81,'side':'after','phase':'extract','attachment':'attached','retention':'held'},
            {'frame':95,'side':'after','phase':'detached_transport','attachment':'detached','retention':'held'}]
    outcomes=[{'frame':95,'attachment':'detached','retention':'held','single_fruit':True}]
    def check():return parse_observations({'output_text':json.dumps({'boundaries':[{'id':0,'first_after_frame':81,'confidence':.9,'states':states,'outcome_observations':outcomes}]})},[task])[0]
    assert not check()['needs_review']
    assert len(check()['states'])==2 and len(check()['context_states'])==1
    states[2]['attachment']='attached'
    assert check()['needs_review']
    states[2]['frame']=96  # Never discard an actually unprovided frame.
    assert check()['needs_review']


def test_incomplete_broad_hypothesis_locates_next_pass_but_is_not_proof():
    task={'id':0,'event':'grasp_truss -> lift_truss','frames':[10,20,30,40]}
    states=[{'frame':20,'side':'before','right_grip':'secured','right_motion':'still'},
            {'frame':30,'side':'after','right_grip':'secured','right_motion':'lift_onset'}]
    r=parse_observations({'output_text':json.dumps({'boundaries':[{'id':0,'first_after_frame':30,'confidence':.8,'states':states}]})},[task])[0]
    assert r['needs_review'] and r['frame'] is None
    assert r['localization_only'] and r['localization_frame']==30
    assert not r['before_after_witness']
