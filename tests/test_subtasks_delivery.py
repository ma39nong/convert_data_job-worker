import copy
import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest

from convest.subtasks.core import Store,segment,values
from convest.subtasks.delivery import publish,DEFAULT_TASK_PROMPT
from convest.subtasks.workspace import Workspace
from test_subtasks import dataset


def reviewed(ws,ep,stage='fine'):
    doc=ws.stages(ep)[stage]
    doc['segments']=[segment(0,20,'grasp_truss',reviewed=True),segment(20,61,'lift_truss',reviewed=True)] if stage=='fine' else [dict(start_frame=0,end_frame=61,label='pickup',reviewed=True)]
    return ws.save_stage(ep,stage,doc)


def test_local_json_separate_paths_idempotent_and_no_source_mutation(tmp_path):
    ep=dataset(tmp_path/'source');store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    try:
        before=ep.identity();fine=reviewed(ws,ep);coarse=reviewed(ws,ep,'coarse')
        f=publish(ws,ep,'fine',target='local',root=str(tmp_path/'chosen'),revision=fine['revision'])
        c=publish(ws,ep,'coarse',target='local',root=str(tmp_path/'chosen'),revision=coarse['revision'])
        assert '/fine/' in f['path'] and '/coarse/' in c['path'] and f['path']!=c['path']
        assert json.loads(Path(f['path']).read_text())['stage']=='fine'
        assert publish(ws,ep,'fine',target='local',root=str(tmp_path/'chosen'),revision=fine['revision'])['path']==f['path']
        assert ep.identity()==before
        with pytest.raises(ValueError,match='版本'):publish(ws,ep,'fine',target='local',root=str(tmp_path/'chosen'),revision=0)
        with pytest.raises(ValueError,match='输入'):publish(ws,ep,'fine',target='local',root=str(ep.root),revision=1)
    finally:store.lockfile.close()


def test_training_copy_has_frame_tasks_intact_state_and_videos(tmp_path):
    ep=dataset(tmp_path/'source');store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    try:
        before=ep.identity();doc=reviewed(ws,ep)
        result=publish(ws,ep,'fine',kind='training',target='local',root=str(tmp_path/'training'),revision=doc['revision'])
        root=Path(result['path']);table=pq.read_table(root/'data/chunk-000/episode_000000.parquet')
        assert values(table,'task_index').tolist()==[0]*61
        assert values(table,'fine_stage_index').tolist()==[1]*20+[2]*41
        assert np.array_equal(values(table,'observation.state'),values(ep.table,'observation.state'))
        assert values(table,'episode_index').tolist()==[0]*61
        info=json.loads((root/'meta/info.json').read_text());assert info['total_episodes']==1 and info['total_frames']==61 and info['total_tasks']==1
        tasks=[json.loads(s) for s in (root/'meta/tasks.jsonl').read_text().splitlines()];assert len(tasks)==1
        assert tasks[0]['task']==DEFAULT_TASK_PROMPT
        prompt=json.loads((root/'meta/subtask_prompts.json').read_text());assert prompt['original_task_prompts']==['pick'] and prompt['stages'][0]['text_zh']
        assert prompt['profile']=='tomato-goal-and-stages-en-v2' and prompt['prompt_mode']=='goal'
        for camera,path in ep.cameras.items():assert (root/f'videos/chunk-000/{camera}/episode_000000.mp4').read_bytes()==path.read_bytes()
        assert ep.identity()==before
        again=publish(ws,ep,'fine',kind='training',target='local',root=str(tmp_path/'training'),revision=1);assert again['path']==str(root)
        (root/'meta/tasks.jsonl').write_text('tampered')
        with pytest.raises(ValueError,match='changed'):publish(ws,ep,'fine',kind='training',target='local',root=str(tmp_path/'training'),revision=1)
    finally:store.lockfile.close()


def test_training_rejects_drafts_without_creating_destination(tmp_path):
    ep=dataset(tmp_path/'source');store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    try:
        doc=ws.stages(ep)['fine'];ws.save_stage(ep,'fine',doc)
        with pytest.raises(ValueError,match='人工确认'):publish(ws,ep,'fine',kind='training',target='local',root=str(tmp_path/'training'),revision=1)
        assert not (tmp_path/'training').exists()
    finally:store.lockfile.close()


def test_mapping_saved_stages_uses_source_frames_and_preserves_history(tmp_path):
    normal=dataset(tmp_path/'normal');speedup=dataset(tmp_path/'speedup',speedup=True)
    store=Store([normal.root,speedup.root],tmp_path/'review');ws=Workspace(store)
    try:
        reviewed(ws,normal);reviewed(ws,normal,'coarse')
        result=ws.map_saved(normal.id,speedup.id,['fine','coarse']);fine=result['stages']['fine']
        assert all(not s['reviewed'] for s in fine['segments'])
        assert fine['projection']['normal_stage_revision']==1
        assert fine['projection']['policy']=='nearest_observation_source_frame'
        assert fine['segments'][-1]['end_frame']==speedup.length
        assert fine['segments'][0]['end_frame']!=20
        assert ws.catalog()['episodes'][0]['pair_state']=='matched'
        with pytest.raises(ValueError,match='已有保存'):ws.map_saved(normal.id,speedup.id,['fine'])
        ws.map_saved(normal.id,speedup.id,['fine'],replace=True)
        assert (ws.root/'history'/speedup.id/'fine/000001.json').exists()
        assert ws.stages(speedup)['fine']['revision']==2
        assert store.load(speedup)['revision']==0
    finally:store.lockfile.close()


def test_mapping_refuses_wrong_recording_and_does_not_publish_half_pair(tmp_path):
    normal=dataset(tmp_path/'normal');speedup=dataset(tmp_path/'speedup',speedup=True,recording='other')
    store=Store([normal.root,speedup.root],tmp_path/'review');ws=Workspace(store)
    try:
        reviewed(ws,normal)
        with pytest.raises(ValueError,match='同一录制'):ws.map_saved(normal.id,speedup.id,['fine'])
        assert ws.catalog()['episodes'][0]['pair_state']=='missing'
        assert not ws.stage_path(speedup.id,'fine').exists()
    finally:store.lockfile.close()


def test_batch_import_isolated_failures_and_multiple_paths(tmp_path,monkeypatch):
    ep=dataset(tmp_path/'normal');store=Store([ep.root],tmp_path/'review');ws=Workspace(store);calls=[]
    def load(path,selection):
        calls.append((path,selection))
        if path=='bad':raise ValueError('offline')
        return {'imported':1,'reused':0,'episodes':1}
    monkeypatch.setattr(ws,'import_directory',load)
    try:
        result=ws.import_many([{'path':'first','episodes':[0]},{'path':'bad'},{'path':'third','episodes':[2]}])
        assert result['failed']==1 and result['imported']==2 and len(calls)==3
        assert result['packages'][1]['error']=='offline'
    finally:store.lockfile.close()


def test_stage_prompt_experiment_is_explicit_and_preserves_original_goal(tmp_path):
    from convest.subtasks.delivery import training_files
    import base64
    import pyarrow as pa
    ep=dataset(tmp_path/'source');store=Store([ep.root],tmp_path/'review');ws=Workspace(store)
    try:
        doc=reviewed(ws,ep);files,_=training_files(ep,doc,prompt_mode='stage')
        table=pq.read_table(pa.BufferReader(base64.b64decode(files['data/chunk-000/episode_000000.parquet'])))
        assert values(table,'task_index').tolist()==[0]*20+[1]*41
        prompts=json.loads(base64.b64decode(files['meta/subtask_prompts.json']))
        assert prompts['task_prompt']==DEFAULT_TASK_PROMPT and prompts['original_task_prompts']==['pick']
        assert prompts['prompt_mode']=='stage'
    finally:store.lockfile.close()
