from types import SimpleNamespace
from convest.subtasks.core import Store
from convest.subtasks.workspace import Workspace
from convest.subtasks import style
from test_subtasks import dataset


def test_reviewed_coarse_boundaries_are_references_without_becoming_fine_labels(tmp_path,monkeypatch):
    ref=dataset(tmp_path/'normal'/'full-block-more'/'ref',recording='reference')
    mapped=dataset(tmp_path/'speedup'/'full-block-more'/'ref',recording='reference',speedup=True)
    target=dataset(tmp_path/'normal'/'full-block-more'/'target',recording='target')
    store=Store([mapped.root,ref.root,target.root],tmp_path/'review');ws=Workspace(store)
    monkeypatch.setattr(style,'images_at',lambda ep,indices,**kw:[{'frame':i,'sha256':str(i),'data_url':'data:image/jpeg;base64,eA=='} for i in indices])
    try:
        doc=ws.stages(ref)['coarse'];labels=['pickup','harvest','adjust','harvest','return']
        doc['segments']=[dict(start_frame=i*10,end_frame=(i+1)*10 if i<4 else ref.length,label=k,reviewed=False) for i,k in enumerate(labels)]
        ws.save_stage(ref,'coarse',doc)
        server=SimpleNamespace(doubao_key='test',doubao_model='test')
        assert ws.model_client(target,server).style_examples==[]
        saved=ws.save_stage(ref,'coarse',ws.stages(ref)['coarse'],confirm_review=True)
        ws.map_saved(ref.id,mapped.id,['coarse'])
        examples=ws.model_client(target,server).style_examples
        assert {tuple(e['transition']) for e in examples}=={('pickup','harvest'),('harvest','adjust'),('adjust','harvest'),('harvest','return')}
        assert all(e['label_space']=='coarse' and e['source_id']==ref.id and e['source_revision']==saved['revision'] for e in examples)
        assert {e['label'] for e in examples}<={'pickup','harvest','adjust','return'}
        assert style.for_video(examples,'LOCAL FINE PASS: fruit evidence')==[]
        assert style.for_video(examples,'Whole video overview')==examples
        selected=style.for_boundaries(examples,{('pick_and_place','rotate_stem')})
        assert selected and all(e['transition']==['harvest','adjust'] for e in selected)
        assert style.for_boundaries(examples,{('grasp_fruit','pick_and_place')})==[]
        assert style.for_boundaries(examples,{('grasp_truss','lift_truss')})==[]
        assert all(r['label']=='unknown' for r in ws.stages(ref)['fine']['segments'])
        assert ws.model_client(ref,server).style_examples==[]
        ws.stage_path(ref.id,'coarse').unlink()
        assert ws.model_client(target,server).style_examples==[]
    finally:store.lockfile.close()
