import copy
import json
import threading
import urllib.request
from types import SimpleNamespace
import pytest
from test_subtasks import dataset
from convest.subtasks.core import Store, atomic_json
from convest.subtasks.workspace import Workspace
from convest.subtasks.quarantine import preview,change,read,plan
from convest.subtasks.delivery import publish
from convest.subtasks.server import Server


def make_pair(tmp_path):
    for variant in ('normal','speedup'):
        ep=dataset(tmp_path/variant,speedup=variant=='speedup')
        hashes=ep.identity()['sha256']
        remote=f'/mnt/data/train_platform/datasets/tomato_sharpa/{variant}/no-block-more/A/1102_test_{variant}_lerobot'
        atomic_json(ep.root/'cache-source.json',{'host':'28','all_selected_sha256_verified':True,'manifest':{'dataset':remote,'files':{k:{'sha256':v} for k,v in hashes.items()}}})
    store=Store([tmp_path/'normal',tmp_path/'speedup'],tmp_path/'review')
    ids={e['variant']:e['id'] for e in store.entries.values()}
    return store,Workspace(store),ids


def test_exclude_pair_restore_persist_and_training_block(tmp_path):
    store,ws,ids=make_pair(tmp_path)
    try:
        before={i:store.episode(i).identity() for i in ids.values()}
        p=preview(ws,ids['normal']);assert len(p['items'])==2 and '/normal/no-block-more/C/' in p['items'][0]['target']
        change(ws,ids['normal'],True,'空手投放',p['revision'])
        catalog=ws.catalog();assert all(e['excluded'] and e['state']=='excluded' for e in catalog['episodes'])
        assert all(g['pending']==0 and g['excluded']==1 for g in catalog['directories'])
        with pytest.raises(ValueError,match='剔除'):publish(ws,store.episode(ids['normal']),'coarse',kind='training',target='local',root=str(tmp_path/'out'),revision=0)
        with pytest.raises(ValueError,match='剔除'):ws.map_saved(ids['normal'],ids['speedup'],['coarse'])
        # Excluded entries are skipped without constructing a model client or changing annotations.
        assert ws.process(list(ids.values()),SimpleNamespace(doubao_key='not-used'))['episodes']==[{'id':ids['normal'],'state':'excluded'},{'id':ids['speedup'],'state':'excluded'}]
        assert all(store.episode(i).identity()==before[i] for i in ids.values())
        assert len(read(ws)['history'])==1
        change(ws,ids['speedup'],False,'',1)
        assert not any(e['excluded'] for e in ws.catalog()['episodes'])
        assert read(ws)['revision']==2 and len(read(ws)['history'])==2
        assert all(store.episode(i).identity()==before[i] for i in ids.values())
    finally:store.lockfile.close()


def test_conflict_running_missing_pair_and_invalid_reason(tmp_path):
    store,ws,ids=make_pair(tmp_path)
    try:
        for args,match in [((True,'',0),'原因'),((True,'bad',2),'其他窗口')]:
            with pytest.raises(ValueError,match=match):change(ws,ids['normal'],*args)
        ws.queue[ids['speedup']]={'state':'running'}
        with pytest.raises(ValueError,match='正在处理'):change(ws,ids['normal'],True,'bad',0)
        assert read(ws)['revision']==0
        ws.queue.clear();del store.entries[ids['speedup']]
        with pytest.raises(ValueError,match='缺失'):preview(ws,ids['normal'])
    finally:store.lockfile.close()


def test_plan_preview_blocks_unreviewed_then_lists_reindex_without_source_writes(tmp_path):
    store,ws,ids=make_pair(tmp_path)
    try:
        p=plan(ws,ids['normal']);assert p['ready'] and len(p['review_warnings'])==2
        before={i:store.episode(i).identity() for i in ids.values()}
        change(ws,ids['normal'],True,'invalid demonstration',0)
        p=plan(ws,ids['normal']);assert p['ready'] and p['operation']=='compact_pair'
        assert all(len(pack['rejected'])==1 and pack['rejected'][0]['id']==ids[pack['variant']] and pack['rejected'][0]['new_episode_index']==0 for pack in p['packages'])
        assert all(store.episode(i).identity()==before[i] for i in ids.values())
        assert not (tmp_path/'out').exists()
    finally:store.lockfile.close()


def test_http_quarantine_is_authenticated_and_roundtrips(tmp_path):
    store,ws,ids=make_pair(tmp_path);store.lockfile.close()
    server=Server(('127.0.0.1',0),[tmp_path/'normal',tmp_path/'speedup'],tmp_path/'review')
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start();base=f'http://127.0.0.1:{server.server_port}'
    try:
        p=json.load(urllib.request.urlopen(base+'/api/workspace/quarantine?id='+ids['normal']))
        data=json.dumps({'id':ids['normal'],'excluded':True,'reason':'no fruit in hand','revision':p['revision']}).encode()
        req=urllib.request.Request(base+'/api/workspace/quarantine',data=data,headers={'Content-Type':'application/json'})
        with pytest.raises(urllib.error.HTTPError) as exc:urllib.request.urlopen(req)
        assert exc.value.code==403
        req.add_header('X-Subtasks-Token',server.token)
        assert json.load(urllib.request.urlopen(req))['excluded']
        assert json.load(urllib.request.urlopen(base+'/api/workspace/organize-plan?id='+ids['normal']))['operation']=='compact_pair'
    finally:server.shutdown();server.server_close();worker.join()
