import copy
import json
import base64
from types import SimpleNamespace
import pytest
from convest.subtasks.boundaries import parse_observations, boundary_prompt, refine_all
from convest.subtasks.template import rules_for
from convest.subtasks.core import segment


def observations(event):
    task={'id':0,'event':event,'frames':[100,101,102]}
    rows=[dict(frame=f,side='before' if f==100 else 'after',left_motion='still',
               right_motion='still',left_location='above_left_tray',
               left_delivery='complete',right_retention='held',held_object='bare_stem')
          for f in task['frames']]
    if event=='in_hand_adjust_to_harvest':
        rows[1]['left_motion']='return_onset';rows[2]['left_motion']='returning'
    else:
        action='adjust' if event=='in_hand_harvest_to_adjust' else 'return'
        rows[1]['right_motion']=action+'_onset';rows[2]['right_motion']=action+'ing'
        # English returning vs adjusting.
        rows[2]['right_motion']='adjusting' if action=='adjust' else 'returning'
    return task,rows


def check(task,rows):
    return parse_observations({'output_text':json.dumps({'boundaries':[dict(
        id=0,first_after_frame=101,confidence=.99,states=rows)]})},[task])[0]


@pytest.mark.parametrize('event',['in_hand_harvest_to_adjust','in_hand_adjust_to_harvest','in_hand_harvest_to_return'])
def test_onsets_require_both_hands_stationary_gate_not_confidence(event):
    task,rows=observations(event)
    assert check(task,rows)['stationary_handoff_proven']
    for field,value,index in [('right_retention','occluded',0),('right_motion','adjusting',0),
                              ('left_location','elsewhere',0)]:
        bad=copy.deepcopy(rows);bad[index][field]=value
        assert check(task,bad)['needs_review']
    bad=copy.deepcopy(rows);del bad[0]['left_motion']
    assert check(task,bad)['frame'] is None


@pytest.mark.parametrize('event',['in_hand_harvest_to_adjust','in_hand_harvest_to_return'])
def test_delivery_release_and_stillness_must_precede_right_motion(event):
    task,rows=observations(event)
    for field,value in [('left_delivery','in_progress'),('left_delivery','not_observed'),
                        ('left_motion','delivering'),('right_motion','adjusting' if 'adjust' in event else 'returning')]:
        bad=copy.deepcopy(rows);bad[1][field]=value
        assert check(task,bad)['needs_review']
    # A previously active motion must not be called BEFORE a later fake onset.
    early=copy.deepcopy(rows[0]);early.update(frame=99,right_motion='adjusting' if 'adjust' in event else 'returning')
    assert check({**task,'frames':[99,100,101,102]},[early]+rows)['needs_review']


def test_second_harvest_starts_leaving_tray_while_right_is_still():
    task,rows=observations('in_hand_adjust_to_harvest')
    bad=copy.deepcopy(rows);bad[1]['right_motion']='adjusting'
    assert check(task,bad)['needs_review']
    bad=copy.deepcopy(rows);bad[1]['left_motion']='returning'
    assert check(task,bad)['needs_review']
    # No delivery requirement is invented for this transition; right settling
    # and left leaving the tray are the defining physical gate.
    for r in rows:r['left_delivery']='not_observed'
    assert check(task,rows)['stationary_handoff_proven']


def test_final_return_does_not_require_bare_stem_but_keeps_handoff_gate():
    task,rows=observations('in_hand_harvest_to_return')
    for held in ['fruit_truss','occluded',None]:
        bad=copy.deepcopy(rows);bad[0]['held_object']=held
        assert check(task,bad)['stationary_handoff_proven']
    for row in rows:row.pop('held_object')
    assert check(task,rows)['stationary_handoff_proven']
    rows[0]['left_delivery']='in_progress'
    assert check(task,rows)['needs_review']


def test_new_rules_are_scoped_and_all_three_searches_use_whole_neighbor_context(tmp_path,monkeypatch):
    from convest.subtasks import template
    assert 'STATIONARY HANDOFF' not in rules_for(False)
    assert 'STATIONARY HANDOFF' in rules_for(True)
    assert 'left_delivery: complete' in boundary_prompt(True)
    assert 'held_object:' not in boundary_prompt(True)
    assert 'carrying the held object' in rules_for(True)
    monkeypatch.setattr(template,'images_at',lambda ep,frames,**kw:[{
        'frame':f,'sha256':str(f),'data_url':'data:image/jpeg;base64,'+base64.b64encode(b'x').decode()} for f in frames])
    seen=[]
    class Client:
        def call_images(self,groups,prompt):
            seen.extend(g['task'] for g in groups)
            return {'output_text':json.dumps({'boundaries':[dict(id=g['task']['id'],first_after_frame=None,confidence=.5,states=[]) for g in groups]})},'key',False
    labels=['grasp_fruit','rotate_stem','grasp_fruit','return_stem']
    rows=[segment(i*100,(i+1)*100,k) for i,k in enumerate(labels)]
    records=[]
    def record(*args):records.append(args)
    _,evidence,_=refine_all(SimpleNamespace(length=400,fps=30),rows,tmp_path,Client(),record,records,30,workflow_in_hand=True)
    assert {t['event'] for t in seen}=={'in_hand_harvest_to_adjust','in_hand_adjust_to_harvest','in_hand_harvest_to_return'}
    assert all('handoff_context_frames' in t for t in seen)
    assert any(t['frames'][0]==1 for t in seen if t['event']=='in_hand_harvest_to_adjust')
    assert all(e['source_frame']['needs_review'] for e in evidence.values())


def test_dense_handoff_proof_recovers_early_onset_from_late_overview(tmp_path,monkeypatch):
    from convest.subtasks import template
    monkeypatch.setattr(template,'images_at',lambda ep,frames,**kw:[{
        'frame':f,'sha256':str(f),'data_url':'data:image/jpeg;base64,'+base64.b64encode(b'x').decode()} for f in frames])
    onsets={'in_hand_harvest_to_adjust':100,'in_hand_adjust_to_harvest':200,'in_hand_harvest_to_return':300}
    class Client:
        def call_images(self,groups,prompt):
            boundaries=[]
            for g in groups:
                t=g['task'];onset=onsets[t['event']];chosen=next((f for f in t['frames'] if f>=onset),None);states=[]
                for f in t['frames']:
                    s=dict(frame=f,side='before' if f<onset else 'after',left_motion='still',right_motion='still',
                           left_location='above_left_tray',left_delivery='complete',right_retention='held',held_object='bare_stem')
                    if f>=onset:
                        field='left_motion' if t['event']=='in_hand_adjust_to_harvest' else 'right_motion'
                        s[field]=('return_onset' if t['event']!='in_hand_harvest_to_adjust' else 'adjust_onset') if f==onset else ('returning' if t['event']!='in_hand_harvest_to_adjust' else 'adjusting')
                    states.append(s)
                boundaries.append(dict(id=t['id'],first_after_frame=chosen,confidence=.99,states=states))
            return {'output_text':json.dumps({'boundaries':boundaries})},'key',False
    rows=[segment(0,120,'grasp_fruit'),segment(120,220,'rotate_stem'),segment(220,320,'grasp_fruit'),segment(320,400,'return_stem')]
    records=[]
    result,evidence,_=refine_all(SimpleNamespace(length=400,fps=30),rows,tmp_path,Client(),lambda *a:records.append(a),records,30,workflow_in_hand=True)
    assert [r['end_frame'] for r in result]==[100,200,300,400]
    assert all(e['source_frame']['stationary_handoff_proven'] and e['source_frame']['consecutive_source_frames'] for e in evidence.values())


def test_first_harvest_needs_right_stationary_then_left_approach_not_contact():
    task={'id':0,'event':'in_hand_pickup_to_harvest','frames':[100,101,102]}
    rows=[dict(frame=f,side='before' if f==100 else 'after',left_motion='still' if f==100 else 'approach_onset' if f==101 else 'approaching',
               right_motion='still',right_location='center',right_retention='held',
               left_location='elsewhere',left_delivery='not_observed') for f in task['frames']]
    assert check(task,rows)['stationary_handoff_proven']
    for field,value,index in [('right_motion','lifting',0),('right_motion','lifting',1),
                              ('right_location','elsewhere',0),('right_location','occluded',1),
                              ('left_motion','approaching',1),('left_motion','other',1),
                              ('left_motion','approaching',0),('right_retention','occluded',0)]:
        bad=copy.deepcopy(rows);bad[index][field]=value
        assert check(task,bad)['needs_review']
    bad=copy.deepcopy(rows);del bad[0]['right_location']
    assert check(task,bad)['frame'] is None
    # A late candidate cannot hide earlier approach evidence.
    early=copy.deepcopy(rows[0]);early.update(frame=99,left_motion='approaching')
    assert check({**task,'frames':[99,100,101,102]},[early]+rows)['needs_review']


def test_first_harvest_routes_to_stationary_gate_without_old_motion_cue(tmp_path,monkeypatch):
    from convest.subtasks import template
    from convest.subtasks.boundaries import handoff_event
    assert handoff_event('lift_truss','grasp_fruit')=='in_hand_pickup_to_harvest'
    rules=rules_for(True)
    assert '1 -> 2 pickup to first harvest' in rules
    assert 'even before contact. Not the first touch or grip.' not in rules
    assert 'FIRST frame the LEFT hand/arm starts preparing' in rules_for(False)
    monkeypatch.setattr(template,'images_at',lambda ep,frames,**kw:[{
        'frame':f,'sha256':str(f),'data_url':'data:image/jpeg;base64,'+base64.b64encode(b'x').decode()} for f in frames])
    def old_cue(*args,**kwargs):raise AssertionError('Old left-only cue cannot set first harvest boundary')
    monkeypatch.setattr(template,'left_motion_candidate',old_cue)
    seen=[]
    class Client:
        def call_images(self,groups,prompt):
            result=[]
            for g in groups:
                t=g['task'];seen.append(t);assert t['event']=='in_hand_pickup_to_harvest'
                chosen=next((f for f in t['frames'] if f>=100),None)
                states=[dict(frame=f,side='before' if f<100 else 'after',left_motion='still' if f<100 else 'approach_onset' if f==100 else 'approaching',
                             right_motion='still',right_location='center',right_retention='held',left_location='elsewhere',left_delivery='not_observed') for f in t['frames']]
                result.append(dict(id=t['id'],first_after_frame=chosen,confidence=.9,states=states))
            return {'output_text':json.dumps({'boundaries':result})},'key',False
    records=[]
    rows=[segment(0,120,'lift_truss'),segment(120,250,'grasp_fruit')]
    result,evidence,motion=refine_all(SimpleNamespace(length=250,fps=30),rows,tmp_path,Client(),lambda *a:records.append(a),records,20,workflow_in_hand=True)
    assert result[0]['end_frame']==100
    assert evidence['0']['source_frame']['stationary_handoff_proven']
    assert not motion and all('handoff_context_frames' in t for t in seen)
