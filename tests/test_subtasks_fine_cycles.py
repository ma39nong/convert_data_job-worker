import copy
import json
from types import SimpleNamespace
from pathlib import Path

import pytest
from convest.subtasks import fine_cycles, qwen
from convest.subtasks.core import Store, atomic_json, project
from convest.subtasks.workspace import Workspace
from convest.subtasks.vocabulary import FINE_CYCLE_PROFILE, english_document
from test_subtasks import dataset


def setup(tmp_path):
    ep=dataset(tmp_path/'full-block-more'/'normal')
    store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    coarse=ws.stages(ep)['coarse'];ends=[0,10,25,30,50,ep.length]
    coarse['segments']=[{'start_frame':a,'end_frame':b,'label':label,'reviewed':True} for a,b,label in zip(ends,ends[1:],fine_cycles.LABELS)]
    coarse=ws.save_stage(ep,'coarse',coarse,confirm_review=True)
    return ep,store,ws,coarse


def response(ep):
    def cycle(a,b,t):return dict(start_seconds=a,end_seconds=b,delivery_seconds=b,successful=True,needs_review=False,observation='visible release and subsequent return')
    return {'sequence_supported':True,'harvests':[
        {'coarse_phase_index':2,'cycles':[cycle(1,1.8,1.5),cycle(1.8,2.5,2.2)]},
        {'coarse_phase_index':4,'cycles':[cycle(3,4,3.7),cycle(4,5,4.7)]}], 'review_notes':[]}


def mock_request(monkeypatch,ep,tmp_path,doc,calls,action=None):
    def request(*a,**kw):
        calls.append(kw)
        if action:action()
        raw={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(doc)}}]}
        return raw,tmp_path/'response.json','key',False,.01,ep.identity()
    monkeypatch.setattr(qwen,'video_response',request)


def test_proposal_coarse_unchanged_adjacent_harvests_preserved_idempotent_save_export(tmp_path,monkeypatch):
    ep,store,ws,coarse=setup(tmp_path);calls=[]
    mock_request(monkeypatch,ep,tmp_path,response(ep),calls)
    before=ws.stage_path(ep.id,'coarse').read_bytes();annotation=store.path(ep.id).read_bytes() if store.path(ep.id).exists() else None
    try:
        result=ws.process([ep.id],SimpleNamespace(doubao_key=''),mode='fine',provider='qwen')
        assert result['episodes'][0]['state']=='ready' and len(calls)==1
        fine=ws.stages(ep)['fine'];assert not fine['saved_at'] and fine['fine_profile']==FINE_CYCLE_PROFILE
        assert [r['label'] for r in fine['segments']]==['pickup','harvest','harvest','adjust','harvest','harvest','return']
        assert all(not r['reviewed'] for r in fine['segments'])
        assert next(e for e in ws.catalog()['episodes'] if e['id']==ep.id)['fine_ready']
        assert fine_cycles.process(ws,ep,store.load(ep))=='preserved' and len(calls)==1
        saved=ws.save_stage(ep,'fine',fine,confirm_review=True)
        doc=english_document(saved);prompts=[r['text'] for r in doc['segments'] if r['label']=='harvest']
        assert len(set(prompts))==1 and 'one tomato' in prompts[0]
        assert doc['prompt_profile']==FINE_CYCLE_PROFILE and 'Unlabeled' not in str(doc)
        assert ws.stage_path(ep.id,'coarse').read_bytes()==before and (store.path(ep.id).read_bytes() if store.path(ep.id).exists() else None)==annotation
        assert fine_cycles.process(ws,ep,store.load(ep))=='preserved' and len(calls)==1
    finally:store.lockfile.close()


@pytest.mark.parametrize('fault',['gap','outside','overlap','missing','unknown_delivery','false_success','wrong_phase','nan','no_evidence'])
def test_invalid_response_rejected_without_inventing_cycles(tmp_path,fault):
    ep,store,ws,coarse=setup(tmp_path);doc=response(ep);r=doc['harvests'][0]['cycles'][0]
    try:
        if fault=='gap':doc['harvests'][0]['cycles'][1]['start_seconds']=1.9
        if fault=='outside':r['start_seconds']=.8
        if fault=='overlap':r['end_seconds']=2
        if fault=='missing':doc['harvests'].pop()
        if fault=='unknown_delivery':r['successful']=None
        if fault=='false_success':r['successful']=False
        if fault=='wrong_phase':doc['harvests'][0]['coarse_phase_index']=4
        if fault=='nan':r['end_seconds']=float('nan')
        if fault=='no_evidence':r['delivery_seconds']=None
        with pytest.raises(ValueError):fine_cycles.parse(doc,ep,coarse)
    finally:store.lockfile.close()


def test_uncertain_span_unknown_cannot_approve(tmp_path):
    ep,store,ws,coarse=setup(tmp_path);doc=response(ep)
    try:
        doc['harvests'][0]['cycles'][0].update(successful=None,delivery_seconds=None,needs_review=True)
        rows=fine_cycles.parse(doc,ep,coarse)
        layer=ws.stages(ep)['fine'];layer.update(segments=rows)
        with pytest.raises(ValueError,match='标签'):ws.save_stage(ep,'fine',layer,confirm_review=True)
    finally:store.lockfile.close()


def test_requires_reviewed_coarse_and_blocks_races_and_stale_save(tmp_path,monkeypatch):
    ep,store,ws,coarse=setup(tmp_path);calls=[]
    try:
        def change():
            new=copy.deepcopy(coarse);new['segments'][1]['end_frame']+=1;new['segments'][2]['start_frame']+=1
            ws.save_stage(ep,'coarse',new,confirm_review=True)
        mock_request(monkeypatch,ep,tmp_path,response(ep),calls,change)
        with pytest.raises(ValueError,match='粗阶段变化'):fine_cycles.process(ws,ep,store.load(ep))
        assert not (ws.root/'fine-proposals'/(ep.id+'.json')).exists()
        current=ws.stages(ep)['coarse'];current['segments']=coarse['segments'];ws.save_stage(ep,'coarse',current,confirm_review=True)
        mock_request(monkeypatch,ep,tmp_path,response(ep),calls)
        fine_cycles.process(ws,ep,store.load(ep));fine=ws.stages(ep)['fine']
        current=ws.stages(ep)['coarse'];current['segments'][1]['end_frame']+=1;current['segments'][2]['start_frame']+=1
        ws.save_stage(ep,'coarse',current,confirm_review=True)
        assert ws.stages(ep)['fine']['coarse_binding_stale']
        with pytest.raises(ValueError,match='粗阶段已变化'):ws.save_stage(ep,'fine',fine,confirm_review=True)
        coarse_path=ws.stage_path(ep.id,'coarse');coarse_path.unlink()
        with pytest.raises(ValueError,match='先审核'):fine_cycles.process(ws,ep,store.load(ep))
    finally:store.lockfile.close()


def test_manual_nonharvest_split_rejected_and_mapping_preserves_each_cycle(tmp_path,monkeypatch):
    ep,store,ws,coarse=setup(tmp_path);calls=[]
    speedup=dataset(tmp_path/'full-block-more'/'speedup',speedup=True)
    store.roots.append(speedup.root);store.scan()
    mock_request(monkeypatch,ep,tmp_path,response(ep),calls)
    try:
        fine_cycles.process(ws,ep,store.load(ep));fine=ws.stages(ep)['fine']
        first=fine['segments'][0];fine['segments'][0:1]=[{**first,'end_frame':5},{**first,'start_frame':5}]
        with pytest.raises(ValueError,match='各保持一段'):ws.save_stage(ep,'fine',fine,confirm_review=True)
        fine=ws.stages(ep)['fine'];ws.save_stage(ep,'fine',fine,confirm_review=True)
        result=ws.map_saved(ep.id,speedup.id,['fine'])['stages']['fine']
        assert result['fine_profile']==FINE_CYCLE_PROFILE
        assert len(result['segments'])==7
        assert all(r['reviewed'] for r in result['segments'])
        fine_cycles.validate_layer(ws,speedup,result)
        assert len({r['text'] for r in english_document(result)['segments'] if r['label']=='harvest'})==1
    finally:store.lockfile.close()


def test_reference_delivery_disagreement_flags_without_changing_labels_or_times(tmp_path):
    ep,store,ws,coarse=setup(tmp_path)
    try:
        doc=response(ep);doc['harvests'][1]['cycles'][-1].update(successful=None,delivery_seconds=None)
        coarse['model_analysis']={'visual_timing_resolution':{'phases':{'second_harvest':{'complete':True,'release_intervals_seconds':[[4.6,4.8]]}}}}
        before=copy.deepcopy(doc);notes=fine_cycles.audit_notes(doc,coarse)
        assert any('未落入模型成功周期' in n for n in notes)
        assert any('待确认区间' in n for n in notes) and doc==before
    finally:store.lockfile.close()


def test_release_internal_cuts_and_last_cycle_preserves_coarse_end(tmp_path):
    ep,store,ws,coarse=setup(tmp_path)
    try:
        doc=response(ep);last=doc['harvests'][0]['cycles'][-1]
        last['delivery_seconds']=2.3
        rows=fine_cycles.parse(doc,ep,coarse)
        assert [(r['start_frame'],r['end_frame'],r['label']) for r in rows[1:3]]==[(10,18,'harvest'),(18,25,'harvest')]
        assert not any(r['label']=='harvest_wait' for r in rows)
        last['end_seconds']=2.3
        with pytest.raises(ValueError,match='粗阶段'):fine_cycles.parse(doc,ep,coarse)
        last['end_seconds']=2.5;doc['harvests'][0]['tail']={'kind':'wait'}
        with pytest.raises(ValueError,match='不再'):fine_cycles.parse(doc,ep,coarse)
        doc=response(ep);doc['harvests'][0]['cycles'][0]['delivery_seconds']=1.5
        with pytest.raises(ValueError,match='释放入盘'):fine_cycles.parse(doc,ep,coarse)
    finally:store.lockfile.close()


def test_old_boundary_policy_proposal_is_reprocessable_saved_still_protected(tmp_path,monkeypatch):
    ep,store,ws,coarse=setup(tmp_path);calls=[]
    mock_request(monkeypatch,ep,tmp_path,response(ep),calls)
    try:
        fine_cycles.process(ws,ep,store.load(ep))
        path=ws.root/'fine-proposals'/(ep.id+'.json');old=json.loads(path.read_text())
        old['provenance']['policy']=FINE_CYCLE_PROFILE;atomic_json(path,old)
        assert not next(e for e in ws.catalog()['episodes'] if e['id']==ep.id)['fine_ready']
        assert not ws.stages(ep)['fine']['fine_policy_current']
        with pytest.raises(ValueError,match='旧规则'):ws.save_stage(ep,'fine',ws.stages(ep)['fine'],confirm_review=True)
        assert fine_cycles.process(ws,ep,store.load(ep))=='ready' and len(calls)==2
        assert list((ws.root/'fine-proposal-history'/ep.id).glob('*.json'))
        ws.save_stage(ep,'fine',ws.stages(ep)['fine'],confirm_review=True)
        assert fine_cycles.process(ws,ep,store.load(ep))=='preserved' and len(calls)==2
    finally:store.lockfile.close()


def test_last_cycle_coarse_end_save_map_export_keeps_four_actions(tmp_path,monkeypatch):
    ep,store,ws,coarse=setup(tmp_path);calls=[];doc=response(ep)
    doc['harvests'][0]['cycles'][-1]['delivery_seconds']=2.3
    speedup=dataset(tmp_path/'full-block-more'/'speedup',speedup=True);store.roots.append(speedup.root);store.scan()
    mock_request(monkeypatch,ep,tmp_path,doc,calls)
    try:
        fine_cycles.process(ws,ep,store.load(ep));fine=ws.stages(ep)['fine']
        assert sum(r['label']=='harvest' for r in fine['segments'])==4
        assert not any(r['label']=='harvest_wait' for r in fine['segments'])
        ws.save_stage(ep,'fine',fine,confirm_review=True)
        mapped=ws.map_saved(ep.id,speedup.id,['fine'])['stages']['fine']
        assert mapped['fine_boundary_policy']==fine_cycles.POLICY
        exported=english_document(mapped)
        assert exported['fine_boundary_policy']==fine_cycles.POLICY
        assert sum(r['label']=='harvest' for r in exported['segments'])==4
        assert set(r['label'] for r in exported['segments'])=={'pickup','harvest','adjust','return'}
        assert len({r['text'] for r in exported['segments'] if r['label']=='harvest'})==1
        assert 'Unlabeled' not in str(exported)
    finally:store.lockfile.close()


def test_retire_old_unsaved_hides_editor_and_keeps_exact_history_and_current(tmp_path,monkeypatch):
    ep,store,ws,coarse=setup(tmp_path);calls=[]
    mock_request(monkeypatch,ep,tmp_path,response(ep),calls)
    try:
        fine_cycles.process(ws,ep,store.load(ep));path=ws.root/'fine-proposals'/(ep.id+'.json')
        current_bytes=path.read_bytes()
        assert ws.archive_obsolete_fine_proposals()==[] and path.read_bytes()==current_bytes
        old=json.loads(current_bytes);old['provenance']['policy']='old-boundaries';atomic_json(path,old)
        old_layer=ws.stages(ep)['fine'];old_bytes=path.read_bytes()
        assert ws.archive_obsolete_fine_proposals()==[ep.id]
        assert not path.exists()
        assert any(p.read_bytes()==old_bytes for p in (ws.root/'fine-proposal-history'/ep.id).glob('*.json'))
        layer=ws.stages(ep)['fine']
        assert layer['needs_fine_generation'] and all(s['label']=='unknown' for s in layer['segments'])
        with pytest.raises(ValueError,match='旧内容'):ws.save_stage(ep,'fine',old_layer,confirm_review=True)
        with pytest.raises(ValueError,match='先按'):ws.save_stage(ep,'fine',layer,confirm_review=True)
        assert fine_cycles.process(ws,ep,store.load(ep))=='ready'
        fresh=ws.stages(ep)['fine'];assert not fresh.get('needs_fine_generation')
        ws.save_stage(ep,'fine',fresh,confirm_review=True)
        assert ws.archive_obsolete_fine_proposals()==[]
        saved_bytes=ws.stage_path(ep.id,'fine').read_bytes()
        archived_doc=json.loads(path.read_text());archived_doc['provenance']['policy']='old-boundaries';atomic_json(path,archived_doc)
        assert ws.archive_obsolete_fine_proposals()==[] and path.exists()
        assert ws.stage_path(ep.id,'fine').read_bytes()==saved_bytes
    finally:store.lockfile.close()


def test_replaced_proposal_does_not_accept_old_window_even_with_same_revision(tmp_path,monkeypatch):
    ep,store,ws,coarse=setup(tmp_path);calls=[]
    mock_request(monkeypatch,ep,tmp_path,response(ep),calls)
    try:
        fine_cycles.process(ws,ep,store.load(ep));old_layer=ws.stages(ep)['fine']
        p=ws.root/'fine-proposals'/(ep.id+'.json');d=json.loads(p.read_text());d['created_at']+=1;atomic_json(p,d)
        fresh=ws.stages(ep)['fine'];assert fresh['revision']==old_layer['revision']==0
        assert fresh['base_annotation_revision']==old_layer['base_annotation_revision']
        with pytest.raises(ValueError,match='旧内容'):ws.save_stage(ep,'fine',old_layer,confirm_review=True)
        ws.save_stage(ep,'fine',fresh,confirm_review=True)
    finally:store.lockfile.close()


def test_local_v2_migration_reuses_delivery_times_preserves_exact_history_and_saved(tmp_path,monkeypatch):
    ep,store,ws,coarse=setup(tmp_path);calls=[];doc=response(ep)
    mock_request(monkeypatch,ep,tmp_path,doc,calls)
    try:
        fine_cycles.process(ws,ep,store.load(ep));path=ws.root/'fine-proposals'/(ep.id+'.json')
        old=json.loads(path.read_text());old['provenance']['policy']=fine_cycles.PREVIOUS_RELEASE_POLICY
        old['provenance']['model_analysis']['harvests'][0]['cycles'][-1].update(end_seconds=2.3,delivery_seconds=2.3)
        old['provenance']['model_analysis']['harvests'][0]['tail']={'kind':'wait','needs_review':False,'observation':'visible stationary'}
        old['segments'][2]['end_frame']=23
        old['segments'].insert(3,{'start_frame':23,'end_frame':25,'label':'harvest_wait','reviewed':False})
        atomic_json(path,old);raw=path.read_bytes();coarse_raw=ws.stage_path(ep.id,'coarse').read_bytes()
        assert fine_cycles.migrate_release_proposals(ws)==[ep.id] and len(calls)==1
        new=json.loads(path.read_text());assert new['provenance']['network_requests']==0
        assert new['provenance']['model_analysis']['harvests'][0]['cycles'][-1]['delivery_seconds']==2.3
        assert new['segments'][2]['end_frame']==25 and len(new['segments'])==7
        assert all(not s['reviewed'] for s in new['segments'])
        assert Path(new['provenance']['local_resegmentation']['history']).read_bytes()==raw
        assert ws.stage_path(ep.id,'coarse').read_bytes()==coarse_raw
        assert fine_cycles.migrate_release_proposals(ws)==[]
        # A real historical saved v2 layer remains valid and is never migrated.
        legacy={**ws.stages(ep)['fine'],'saved_at':1,'revision':1,'fine_boundary_policy':fine_cycles.PREVIOUS_RELEASE_POLICY,'segments':old['segments']}
        atomic_json(ws.stage_path(ep.id,'fine'),legacy);saved_raw=ws.stage_path(ep.id,'fine').read_bytes();atomic_json(path,old)
        assert fine_cycles.migrate_release_proposals(ws)==[]
        fine_cycles.validate_layer(ws,ep,ws.stages(ep)['fine'])
        assert ws.stage_path(ep.id,'fine').read_bytes()==saved_raw
    finally:store.lockfile.close()


@pytest.mark.parametrize('uncertainty',['tail','cycle','time'])
def test_local_resegmentation_does_not_infer_missing_success(tmp_path,uncertainty):
    ep,store,ws,coarse=setup(tmp_path);doc=response(ep)
    try:
        if uncertainty=='tail':doc['harvests'][0]['tail']={'kind':'uncertain'}
        if uncertainty=='cycle':doc['harvests'][0]['cycles'][0]['successful']=None
        if uncertainty=='time':doc['harvests'][0]['cycles'][0]['delivery_seconds']=None
        before=copy.deepcopy(doc)
        with pytest.raises(ValueError):fine_cycles.resegment_from_deliveries(doc,ep,coarse)
        assert doc==before
    finally:store.lockfile.close()


def test_one_fruit_covers_whole_parent_even_if_release_precedes_coarse_end(tmp_path):
    ep,store,ws,coarse=setup(tmp_path);doc=response(ep)
    try:
        first=doc['harvests'][0]['cycles'][0]
        first.update(start_seconds=1,end_seconds=2.5,delivery_seconds=1.8)
        doc['harvests'][0]['cycles']=[first]
        rows=fine_cycles.parse(doc,ep,coarse)
        assert [(r['start_frame'],r['end_frame'],r['label']) for r in rows if r['start_frame']<25 and r['end_frame']>10]==[(10,25,'harvest')]
    finally:store.lockfile.close()


def test_user_discard_qwen_fine_archives_exact_bytes_preserves_coarse_and_rejects_stale(tmp_path,monkeypatch):
    ep,store,ws,coarse=setup(tmp_path);calls=[]
    mock_request(monkeypatch,ep,tmp_path,response(ep),calls)
    try:
        fine_cycles.process(ws,ep,store.load(ep));fine=ws.stages(ep)['fine']
        path=ws.root/'fine-proposals'/(ep.id+'.json');raw=path.read_bytes();before=ws.stage_path(ep.id,'coarse').read_bytes()
        with pytest.raises(ValueError,match='变化'):ws.discard_qwen_fine(ep.id,'stale')
        assert path.read_bytes()==raw
        ws.queue[ep.id]={'state':'running'}
        with pytest.raises(ValueError,match='队列'):ws.discard_qwen_fine(ep.id,fine['proposal_key'])
        ws.queue[ep.id]={'state':'ready'}
        result=ws.discard_qwen_fine(ep.id,fine['proposal_key'])
        assert Path(result['archived_path']).read_bytes()==raw and not path.exists()
        assert result['network_requests']==0 and len(calls)==1
        assert ws.stage_path(ep.id,'coarse').read_bytes()==before
        cleared=ws.stages(ep)['fine'];assert cleared['needs_fine_generation']
        assert [r['label'] for r in cleared['segments']]==['unknown']
        assert not next(e for e in ws.catalog()['episodes'] if e['id']==ep.id)['fine_ready']
        with pytest.raises(ValueError):ws.save_stage(ep,'fine',fine,confirm_review=True)
        fine_cycles.process(ws,ep,store.load(ep));new=ws.stages(ep)['fine']
        ws.save_stage(ep,'fine',new,confirm_review=True);saved=ws.stage_path(ep.id,'fine').read_bytes()
        with pytest.raises(ValueError,match='人工保存'):ws.discard_qwen_fine(ep.id,new['proposal_key'])
        assert ws.stage_path(ep.id,'fine').read_bytes()==saved
    finally:store.lockfile.close()


def test_fine_failure_retains_mode_and_old_batch_failures_display_correctly(tmp_path,monkeypatch):
    ep,store,ws,coarse=setup(tmp_path);calls=[]
    mock_request(monkeypatch,ep,tmp_path,{'sequence_supported':False},calls)
    try:
        result=ws.process([ep.id],SimpleNamespace(doubao_key=''),mode='fine',provider='qwen')
        assert result['episodes'][0]['state']=='failed'
        assert ws.queue[ep.id]['mode']=='fine'
        row=next(e for e in ws.catalog()['episodes'] if e['id']==ep.id)
        assert row['fine_state']=='failed' and row['coarse_state']=='ready'
        error=ws.queue[ep.id]['error'];ws.queue[ep.id].pop('mode')
        atomic_json(ws.root/'active-processing-batch.json',{'state':'completed','mode':'fine','results':{ep.id:{'state':'failed','error':error}}})
        assert next(e for e in ws.catalog()['episodes'] if e['id']==ep.id)['fine_state']=='failed'
        assert 'mode' not in ws.queue[ep.id]  # display recovery never rewrites historical data
        ws.queue[ep.id]['error']='different failure'
        assert next(e for e in ws.catalog()['episodes'] if e['id']==ep.id)['coarse_state']=='failed'
    finally:store.lockfile.close()
