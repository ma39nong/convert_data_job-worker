import numpy as np
from convest.subtasks.local_boundaries import onset_candidates

def test_sustained_onset_rejects_spike_and_requires_other_arm_quiet():
    d=np.zeros(60);other=np.zeros(60);d[10]=.02;d[25:40]=.002
    assert onset_candidates(d,other,25,20)==[25]
    other[24]=.0003
    assert onset_candidates(d,other,25,20)==[25]
    other[24:28]=.01
    assert onset_candidates(d,other,25,20)==[]

def test_ambiguous_candidates_remain_explicit_and_search_is_bounded():
    d=np.zeros(100);other=np.zeros(100);d[20:30]=.002;d[45:60]=.002
    assert onset_candidates(d,other,35,20)==[20,45]
    assert onset_candidates(d,other,20,4)==[20]

def test_saved_coarse_is_never_replaced(tmp_path):
    import pytest
    from test_subtasks import dataset
    from convest.subtasks.core import Store
    from convest.subtasks.workspace import Workspace
    from convest.subtasks.local_boundaries import refine
    ep=dataset(tmp_path/'full-block-more'/'normal');store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    try:
        doc=ws.stages(ep)['coarse'];doc['segments']=[{'start_frame':0,'end_frame':ep.length,'label':'pickup','reviewed':True}]
        ws.save_stage(ep,'coarse',doc,confirm_review=True);before=ws.stage_path(ep.id,'coarse').read_bytes()
        with pytest.raises(ValueError,match='已经人工保存'):refine(ws,ep.id)
        assert ws.stage_path(ep.id,'coarse').read_bytes()==before
    finally:store.lockfile.close()

def test_audit_flags_ambiguous_and_delivery_conflict_without_editing_data():
    import copy
    from convest.subtasks.local_boundaries import audit_notes
    rows=[{'start_frame':a,'end_frame':b} for a,b in [(0,10),(10,20),(20,30),(30,40),(40,50)]]
    summary={'first_harvest':{'successful_count':1,'delivery_seconds':[2.5]},'second_harvest':{'successful_count':None,'delivery_seconds':[]}}
    p={'segments':rows,'provenance':{'local_boundary_refinement':{'evidence':[{'boundary_index':i,'selected_frame':None if i==2 else 10} for i in range(4)]},'model_analysis':{'harvest_summary':summary,'review_notes':[]}}}
    before=copy.deepcopy(p);notes=audit_notes(p,10)
    assert len(notes)==2 and '3/4' in p['warning']
    assert p['segments']==before['segments'] and summary==before['provenance']['model_analysis']['harvest_summary']
    audit_notes(p,10);assert len(p['provenance']['model_analysis']['review_notes'])==2

def test_visual_resolution_keeps_cuts_and_rejects_stale_or_saved(tmp_path,monkeypatch):
    import json,hashlib,threading
    from types import SimpleNamespace
    from convest.subtasks.local_boundaries import resolve_delivery_timing
    monkeypatch.chdir(tmp_path)
    root=tmp_path/'workspace';root.mkdir();evidence=tmp_path/'outputs/subtasks/evidence.jpg';evidence.parent.mkdir(parents=True);evidence.write_bytes(b'evidence')
    source={'episode':1};ep=SimpleNamespace(identity=lambda:source,fps=10,length=50)
    stage=root/'saved.json';ws=SimpleNamespace(root=root,lock=threading.RLock(),stage_path=lambda *args:stage)
    ws.store=SimpleNamespace(lock=threading.RLock(),episode=lambda _:ep,entries={'e':{'variant':'normal'}})
    rows=[{'start_frame':a,'end_frame':b,'reviewed':False,'label':label} for (a,b),label in zip([(0,10),(10,20),(20,30),(30,40),(40,50)],['pickup','harvest','adjust','harvest','return'])]
    proposal={'source':source,'segments':rows,'provenance':{'local_boundary_refinement':{'evidence':[{'boundary_index':i,'selected_frame':10} for i in range(4)]},'model_analysis':{'harvest_summary':{},'review_notes':[],'segments':[{} for _ in rows],'handoffs':[{} for _ in range(4)]}}}
    path=root/'coarse-proposals/e.json';path.parent.mkdir();path.write_text(json.dumps(proposal))
    correction={'expected_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'reason':'observed release','evidence':[{'path':str(evidence),'sha256':hashlib.sha256(evidence.read_bytes()).hexdigest()}],'phases':{'first_harvest':{'complete':True,'release_intervals_seconds':[[1.2,1.4]]}}}
    import pytest
    bad={**correction,'phases':{'first_harvest':{'release_intervals_seconds':[[1.9,2.1]]}}}
    with pytest.raises(ValueError,match='超出'):resolve_delivery_timing(ws,'e',bad)
    resolve_delivery_timing(ws,'e',correction);result=json.loads(path.read_text())
    assert result['segments']==rows
    assert result['provenance']['model_analysis']['harvest_summary']['first_harvest']['successful_count']==1
    assert result['provenance']['visual_timing_resolution']['human_reviewed'] is False
    with pytest.raises(ValueError,match='变化'):resolve_delivery_timing(ws,'e',correction)
    updated={**correction,'expected_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'boundaries':[{'boundary_index':1,'frame':19,'observation':'right adjustment after left release'}]}
    resolve_delivery_timing(ws,'e',updated);shifted=json.loads(path.read_text())
    assert shifted['segments'][1]['end_frame']==19 and shifted['segments'][2]['start_frame']==19
    assert all(not r['reviewed'] for r in shifted['segments'])
    assert shifted['provenance']['model_analysis']['handoffs'][1]['seconds']==1.9
    stage.write_text('human review')
    with pytest.raises(ValueError,match='人工保存'):resolve_delivery_timing(ws,'e',correction)
    assert stage.read_text()=='human review'


def test_batch_skips_protected_and_continues_after_failure(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import threading
    import convest.subtasks.local_boundaries as local
    ws=SimpleNamespace(root=tmp_path,lock=threading.RLock(),store=SimpleNamespace(lock=threading.RLock()),stage_path=lambda id_,stage:tmp_path/(id_+'-saved'))
    ws.catalog=lambda:{'episodes':[{'id':id_,'local_calibration_eligible':id_!='already'} for id_ in ['saved','already','bad','good']]}
    (tmp_path/'saved-saved').write_text('human')
    calls=[]
    def refine(_,id_):
        calls.append(id_)
        if id_=='bad':raise ValueError('missing state')
        return {'id':id_,'state':'ready','network_requests':0}
    monkeypatch.setattr(local,'refine',refine);progress=[]
    result=local.refine_batch(ws,['saved','already','bad','good'],lambda a,b:progress.append((a,b)))
    assert calls==['bad','good']
    assert (result['calibrated'],result['preserved'],result['failed'])==(1,2,1)
    assert progress[-1]==(4,4) and result['network_requests']==0
    assert (tmp_path/'saved-saved').read_text()=='human'
    assert list((tmp_path/'local-boundary-batches').glob('*.json'))


def test_already_calibrated_draft_is_idempotent(tmp_path):
    import json,threading
    from types import SimpleNamespace
    from convest.subtasks.local_boundaries import refine
    source={'id':'test'};ep=SimpleNamespace(identity=lambda:source)
    ws=SimpleNamespace(root=tmp_path,lock=threading.RLock(),store=SimpleNamespace(lock=threading.RLock(),episode=lambda _:ep,entries={'e':{'variant':'normal'}}),stage_path=lambda *args:tmp_path/'no-human-stage')
    path=tmp_path/'coarse-proposals/e.json';path.parent.mkdir();path.write_text(json.dumps({'source':source,'provenance':{'local_boundary_refinement':{'evidence':[]},'visual_boundary_resolution':{'checked':True}}}))
    before=path.read_bytes();assert refine(ws,'e')['state']=='preserved';assert before==path.read_bytes()
