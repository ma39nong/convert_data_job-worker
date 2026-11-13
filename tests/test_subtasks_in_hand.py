import json
import pytest
from convest.subtasks.core import segment,sha
from convest.subtasks.template import validate_template,rules_for
from convest.subtasks.workflow import in_hand_source,IN_HAND_PROFILE,IN_HAND_ADJUST
from convest.subtasks.vocabulary import english_document,CATALOG

FIVE=['grasp_truss','lift_truss','grasp_fruit','pick_and_place','rotate_stem','grasp_fruit','pick_and_place','return_stem']
SEVEN=FIVE[:4]+['return_truss','rotate_stem','grasp_truss','lift_truss']+FIVE[5:]
def rows(labels):return [segment(i,i+1,k) for i,k in enumerate(labels)]

def test_workflow_grammar_is_scoped_and_rejects_old_forcing():
    assert len(validate_template(rows(FIVE),in_hand=True))==5
    assert len(validate_template(rows(SEVEN)))==7
    with pytest.raises(ValueError):validate_template(rows(SEVEN),in_hand=True)
    with pytest.raises(ValueError):validate_template(rows(FIVE))
    assert in_hand_source({'remote_source':{'dataset':'/normal/full-block-more/A/1112'}})
    assert in_hand_source({'remote_source':{'dataset':'/speedup/full-block-more/B/1113'}})
    assert not in_hand_source({'dataset':'/normal/no-block-more/A/1112'})
    assert not in_hand_source({'dataset':'/normal/full-block-more-old/A'})

def test_new_export_uses_air_prompt_without_changing_legacy_or_review():
    doc={'stage':'coarse','source':{'remote_source':{'dataset':'/normal/full-block-more/A/1112'}},'segments':[dict(start_frame=i,end_frame=i+1,label=k,reviewed=False) for i,k in enumerate(['pickup','harvest','adjust','harvest','return'])]}
    result=english_document(doc)
    assert result['prompt_profile']==IN_HAND_PROFILE
    assert result['segments'][2]['text']==IN_HAND_ADJUST
    assert not any(r['reviewed'] for r in result['segments'])
    doc['stage']='fine';doc['segments'][2]['label']='rotate_stem'
    assert english_document(doc)['segments'][2]['text']==CATALOG['fine']['rotate_stem']
    doc['stage']='coarse';doc['segments'][2]['label']='adjust';doc['source']={'dataset':'/normal/no-block-more/A/old'}
    assert english_document(doc)['segments'][2]['text']==CATALOG['coarse']['adjust']

def test_every_model_pass_uses_new_workflow(tmp_path,monkeypatch):
    from test_subtasks import dataset
    from convest.subtasks import template,doubao,harvest,boundaries
    ep=dataset(tmp_path/'full-block-more'/'source')
    seen=[]
    def montage(ep,path,start,end,**kwargs):
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'video')
        return {'sample_fps':kwargs.get('sample_fps',1),'sha256':sha(path)}
    monkeypatch.setattr(doubao,'make_montage',montage)
    class Client:
        def call(self,path,prompt,fps):
            assert 'FIVE coarse phases' in prompt and 'There is NO intermediate plate return' in prompt
            assert 'Exactly these\nseven' not in prompt
            seen.append('local' if 'LOCAL FINE PASS' in prompt else 'global')
            duration=ep.length/ep.fps
            if 'LOCAL FINE PASS' in prompt:
                lo,hi=map(int,path.stem.split('_')[-2:]);duration=(hi-lo)/ep.fps
                labels=['grasp_fruit','pick_and_place']
            else:labels=['grasp_truss','lift_truss','HARVEST','rotate_stem','HARVEST','return_stem']
            return {'output_text':json.dumps({'segments':[{'end_seconds':duration*(i+1)/len(labels),'label':k,'confidence':.9,'attachment':'attached' if i==0 else 'detached','retention':'held','event':'approach_attached' if i==0 else 'extract_start'} for i,k in enumerate(labels)]})},str(len(seen)),False
    def refine(ep,segments,run,client,record,records,max_requests,progress,**kw):
        assert kw['workflow_in_hand'] is True
        assert 'FIVE coarse phases' in boundaries.boundary_prompt(True)
        seen.append('boundaries')
        evidence={str(i):{'source_frame':{'needs_review':False}} for i in range(len(segments)-1)}
        return segments,evidence,{}
    monkeypatch.setattr(boundaries,'refine_all',refine)
    result=template.annotate_template(ep,tmp_path/'output',client=Client())
    assert result['workflow_profile']==IN_HAND_PROFILE
    assert result['coarse_template']==['拿起番茄','摘取番茄','调整番茄','摘取番茄','放回番茄']
    assert seen==['global','local','local','boundaries']
    assert [r['label'] for r in result['segments']]==FIVE
    assert result['segments'][-1]['end_frame']==ep.length
    assert all(not r['reviewed'] for r in result['segments'])
