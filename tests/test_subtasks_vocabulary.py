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


def test_english_export_preserves_review_and_distinguishes_final_return():
    labels=['pickup','harvest','return','adjust','pickup','harvest','return']
    doc={'stage':'coarse','revision':5,'source':{'id':'source'},'note':'中文人工备注','segments':[
        dict(start_frame=i*10,end_frame=(i+1)*10,label=label,text='中文',reviewed=True)
        for i,label in enumerate(labels)]}
    before=copy.deepcopy(doc);out=english_document(doc)
    assert doc==before
    assert not re.search('[\u4e00-\u9fff]',json.dumps(out,ensure_ascii=False))
    assert out['segments'][2]['text']==CATALOG['coarse']['return']
    assert out['segments'][6]['text']==CATALOG['return_stem']
    assert [(r['start_frame'],r['end_frame'],r['reviewed'],r['label']) for r in out['segments']]==[(r['start_frame'],r['end_frame'],r['reviewed'],r['label']) for r in doc['segments']]
    doc['segments']=doc['segments'][:3]
    assert stage_prompt('coarse',doc['segments'],2)==CATALOG['coarse']['return']


def test_stage_training_copy_uses_occurrence_specific_english(tmp_path):
    import base64
    ep=dataset(tmp_path/'source');store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    try:
        doc=ws.stages(ep)['coarse'];labels=['pickup','harvest','return','adjust','pickup','harvest','return']
        doc['segments']=[dict(start_frame=i*8,end_frame=(i+1)*8 if i<6 else ep.length,label=k,reviewed=True,text='中文') for i,k in enumerate(labels)]
        files,_=training_files(ep,doc,prompt_mode='stage')
        tasks=[json.loads(line)['task'] for line in base64.b64decode(files['meta/tasks.jsonl']).decode().splitlines()]
        assert len(tasks)==5 and CATALOG['return_stem'] in tasks
        for name in ('stage_annotations.json','meta/subtask_prompts.json'):
            assert not re.search('[\u4e00-\u9fff]',base64.b64decode(files[name]).decode())
    finally:store.lockfile.close()
