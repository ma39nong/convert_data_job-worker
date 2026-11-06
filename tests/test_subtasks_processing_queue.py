import json
from types import SimpleNamespace

import pytest

from convest.subtasks.core import Store
from convest.subtasks.workspace import Workspace
from convest.subtasks import processing_queue as queue
from test_subtasks import dataset


def workspace(tmp_path):
    eps=[dataset(tmp_path/name,recording=name) for name in ['one','two','three']]
    store=Store([ep.root for ep in eps],tmp_path/'review')
    return Workspace(store),[ep.id for ep in eps]


def test_errors_retry_only_after_initial_pass_and_do_not_loop_forever(tmp_path,monkeypatch):
    ws,ids=workspace(tmp_path);calls=[]
    def process(selected,server,**kw):
        id_=selected[0];calls.append((id_,kw['retry_namespace']))
        state='failed' if id_==ids[1] or (id_==ids[0] and not kw['retry_namespace']) else 'ready'
        return {'episodes':[{'id':id_,'state':state,'error':'bad response' if state=='failed' else ''}]}
    monkeypatch.setattr(ws,'process',process)
    try:
        batch=queue.create(ws,ids,'coarse');result=queue.run(ws,None,batch['id'])
        assert [i for i,_ in calls]==ids+[ids[0],ids[1]]
        assert all(token is None for _,token in calls[:3])
        assert all(token for _,token in calls[3:])
        assert [r['state'] for r in result['episodes']]==['ready','failed','ready']
        assert len(result['episodes'][1]['attempts'])==2
        assert queue.active(ws)['state']=='completed'
        assert queue.summary(ws)['retry_completed']==2
    finally:ws.store.lockfile.close()


def test_resume_keeps_completed_items_and_continues_interrupted_item(tmp_path,monkeypatch):
    ws,ids=workspace(tmp_path);calls=[]
    def crash(selected,server,**kw):
        id_=selected[0];calls.append(id_)
        if id_==ids[1]:raise KeyboardInterrupt()
        return {'episodes':[{'id':id_,'state':'ready'}]}
    monkeypatch.setattr(ws,'process',crash)
    try:
        batch=queue.create(ws,ids,'coarse')
        with pytest.raises(KeyboardInterrupt):queue.run(ws,None,batch['id'])
        assert queue.active(ws)['initial_done']==[ids[0]]
        resumed=Workspace(ws.store)
        monkeypatch.setattr(resumed,'process',lambda selected,server,**kw:(calls.append(selected[0]) or {'episodes':[{'id':selected[0],'state':'ready'}]}))
        queue.run(resumed,None,batch['id'])
        assert calls==[ids[0],ids[1],ids[1],ids[2]]
        assert queue.active(resumed)['state']=='completed'
    finally:ws.store.lockfile.close()


def test_remaining_normal_is_grade_ordered_and_preserves_saved_excluded(tmp_path,monkeypatch):
    ws,ids=workspace(tmp_path)
    rows=[]
    for id_,grade,date in zip(ids,['B','A','A'],['1101','1104','1102']):
        rows.append(dict(id=id_,variant='normal',remote_directory=f'/normal/{grade}/{date}_lerobot',episode_index=0,
                         excluded=False,coarse_saved=False,coarse_ready=False,human_reviewed=False))
    monkeypatch.setattr(ws,'catalog',lambda:{'episodes':rows})
    try:
        assert queue.remaining(ws)==[ids[2],ids[1],ids[0]]
        rows[2]['coarse_saved']=True;rows[1]['excluded']=True
        assert queue.remaining(ws)==[ids[0]]
        rows[0]['variant']='speedup';assert queue.remaining(ws)==[]
    finally:ws.store.lockfile.close()


def test_server_automatically_resumes_durable_batch(tmp_path,monkeypatch):
    from convest.subtasks.server import Server
    ws,ids=workspace(tmp_path);roots=list(ws.store.roots);output=ws.store.output
    batch=queue.create(ws,ids,'coarse');batch['state']='running';batch['initial_done']=[ids[0]]
    batch['results'][ids[0]]={'id':ids[0],'state':'ready','attempts':[]};queue.checkpoint(ws,batch)
    ws.store.lockfile.close();calls=[]
    def process(self,selected,server,**kw):
        calls.extend(selected)
        return {'episodes':[{'id':selected[0],'state':'ready'}]}
    monkeypatch.setattr(Workspace,'process',process)
    server=Server(('127.0.0.1',0),roots,output,doubao_key='test')
    try:
        server.pool.shutdown(wait=True)
        assert calls==ids[1:]
        assert queue.active(server.workspace)['state']=='completed'
        assert server.process_job is not None
    finally:server.server_close()
