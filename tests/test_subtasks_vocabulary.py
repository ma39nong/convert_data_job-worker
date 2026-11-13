import copy
import json
import re
from pathlib import Path

from convest.subtasks.vocabulary import CATALOG, DEFAULT_TASK_PROMPT, english_document, stage_prompt
from convest.subtasks.delivery import training_files
from convest.subtasks.workspace import Workspace
from convest.subtasks.core import Store
from test_subtasks import dataset


def test_ui_and_export_vocabulary_are_identical():
    source=(Path(__file__).parents[1]/'src/convest/subtasks/static/app.js').read_text()
    catalog=json.loads(re.search(r'^const vocabulary=(.*);$',source,re.M)[1])
    assert catalog==CATALOG
    assert DEFAULT_TASK_PROMPT=='Harvest all tomatoes into the left basket and return stem to right plate'
    assert all(re.fullmatch('[A-Za-z ]+',v) for kind in ('fine','coarse') for v in CATALOG[kind].values())


def test_english_export_preserves_review_and_shares_return_prompt():
    labels=['pickup','harvest','return','adjust','pickup','harvest','return']
    doc={'stage':'coarse','revision':5,'source':{'id':'source'},'note':'中文人工备注','segments':[
        dict(start_frame=i*10,end_frame=(i+1)*10,label=label,text='中文',reviewed=True)
        for i,label in enumerate(labels)]}
    before=copy.deepcopy(doc);out=english_document(doc)
    assert doc==before
    assert not re.search('[\u4e00-\u9fff]',json.dumps(out,ensure_ascii=False))
    assert out['segments'][2]['text']==CATALOG['coarse']['return']
    assert out['segments'][6]['text']==CATALOG['coarse']['return']
    assert [(r['start_frame'],r['end_frame'],r['reviewed'],r['label']) for r in out['segments']]==[(r['start_frame'],r['end_frame'],r['reviewed'],r['label']) for r in doc['segments']]
    doc['segments']=doc['segments'][:3]
    assert stage_prompt('coarse',doc['segments'],2)==CATALOG['coarse']['return']


def test_stage_training_copy_uses_four_action_prompts(tmp_path):
    import base64
    ep=dataset(tmp_path/'source');store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    try:
        doc=ws.stages(ep)['coarse'];labels=['pickup','harvest','return','adjust','pickup','harvest','return']
        doc['segments']=[dict(start_frame=i*8,end_frame=(i+1)*8 if i<6 else ep.length,label=k,reviewed=True,text='中文') for i,k in enumerate(labels)]
        files,_=training_files(ep,doc,prompt_mode='stage')
        tasks=[json.loads(line)['task'] for line in base64.b64decode(files['meta/tasks.jsonl']).decode().splitlines()]
        assert len(tasks)==4 and CATALOG['coarse']['return'] in tasks
        for name in ('stage_annotations.json','meta/subtask_prompts.json'):
            assert not re.search('[\u4e00-\u9fff]',base64.b64decode(files[name]).decode())
    finally:store.lockfile.close()


def test_export_preserves_explicit_inheritance_without_inventing_review():
    for value in (False, True):
        doc={'stage':'coarse','segments':[], 'inherited':value}
        assert english_document(doc)['inherited'] is value
    assert 'inherited' not in english_document({'stage':'coarse','segments':[]})


def test_four_action_names_numbers_and_adjustment_are_location_neutral():
    from convest.subtasks.workspace import COARSE_LABELS
    expected=[('pickup','右手拿起番茄'),('harvest','左手采摘番茄'),('adjust','右手调整番茄'),('return','右手放回番茄')]
    assert [(label,COARSE_LABELS[label]) for label,_ in expected]==expected
    assert [CATALOG['numbers'][label] for label,_ in expected]==[1,2,3,4]
    assert CATALOG['coarse']['adjust']=='Right hand adjusts the tomato truss'
    for dataset in ('/normal/no-block-more/A/old','/normal/full-block-more/subtask-roi/A/new'):
        doc={'stage':'coarse','source':{'remote_source':{'dataset':dataset}},'segments':[{'label':'adjust','start_frame':10,'end_frame':20,'reviewed':True}]}
        out=english_document(doc)
        from convest.subtasks.workflow import IN_HAND_ADJUST
        expected=IN_HAND_ADJUST if '/full-block-more/' in dataset else CATALOG['coarse']['adjust']
        assert out['segments'][0]['text']==expected
        assert (out['segments'][0]['start_frame'],out['segments'][0]['end_frame'],out['segments'][0]['reviewed'])==(10,20,True)
