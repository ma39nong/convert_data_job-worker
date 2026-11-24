import copy
from types import SimpleNamespace
import numpy as np
from convest.subtasks import fine_local
from test_subtasks_fine_cycles import setup,response,mock_request
from convest.subtasks import fine_cycles


def signal_ep(monkeypatch):
    names=[f'left_fr3_joint{i}.position' for i in range(1,8)]+['left_index.position','left_thumb.position']
    q=np.zeros((240,9));q[80:,7]=.1;q[100:,7]=.2;q[180:,7]=.3
    monkeypatch.setattr(fine_local,'values',lambda *a:q)
    ep=SimpleNamespace(info={'features':{'observation.state':{'names':names}}},table=None,length=240,fps=30)
    coarse={'segments':[{'start_frame':0,'end_frame':240,'label':'harvest'}]}
    fine={'segments':[{'start_frame':0,'end_frame':90,'label':'harvest'}, {'start_frame':90,'end_frame':240,'label':'harvest'}]}
    return ep,coarse,fine,q


def test_ambiguous_events_never_move_cuts_or_count_success(monkeypatch):
    ep,coarse,fine,q=signal_ep(monkeypatch);before=copy.deepcopy(fine)
    r=fine_local.analyze(ep,coarse,fine)
    assert r['boundaries'][0]['state']=='multiple_candidates'
    assert len(r['boundaries'][0]['candidate_windows'])==2
    assert r['extra_motion_events'][0]['frame']==180
    assert fine==before and r['automatic_changes']==r['network_requests']==0
    assert not r['semantic_release_proven']


def test_no_event_or_invalid_signal_preserves_annotations(monkeypatch):
    ep,coarse,fine,q=signal_ep(monkeypatch);q[:]=0
    r=fine_local.analyze(ep,coarse,fine)
    assert r['boundaries'][0]['state']=='no_candidate'
    q[0,0]=np.nan
    assert '无效' in fine_local.analyze(ep,coarse,fine)['notes'][0]
    ep.info['features']['observation.state']['names']=[]
    assert '缺少' in fine_local.analyze(ep,coarse,fine)['notes'][0]


def test_saved_layer_readonly_audit_stale_evidence_not_displayed(tmp_path,monkeypatch):
    ep,store,ws,coarse=setup(tmp_path)
    calls=[];mock_request(monkeypatch,ep,tmp_path,response(ep),calls)
    try:
        fine_cycles.process(ws,ep,store.load(ep));fine=ws.stages(ep)['fine'];ws.save_stage(ep,'fine',fine,confirm_review=True)
        path=ws.stage_path(ep.id,'fine');before=path.read_bytes()
        r=fine_local.audit(ws,ep.id)
        assert r['network_requests']==0 and path.read_bytes()==before and len(calls)==1
        assert ws.stages(ep)['fine']['fine_local_evidence']['fine_sha256']
        edited=ws.stages(ep)['fine'];edited['segments'][1]['end_frame']+=1;edited['segments'][2]['start_frame']+=1
        ws.save_stage(ep,'fine',edited,confirm_review=True)
        assert not ws.stages(ep)['fine'].get('fine_local_evidence')
    finally:store.lockfile.close()
