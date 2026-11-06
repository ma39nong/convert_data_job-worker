import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from test_subtasks import dataset
from convest.subtasks.core import Episode, Store, atomic_json, sha, project
from convest.subtasks.workspace import Workspace
from convest.subtasks.quarantine import change,plan
from convest.subtasks import organization as org


def full_dataset(root,speedup=False):
    first=dataset(root,speedup=speedup,recording='recording-0');n=first.length;info=copy.deepcopy(first.info);rows=[]
    for i in range(3):
        if i:
            table=first.table
            table=table.set_column(table.schema.get_field_index('episode_index'),'episode_index',pa.array([i]*n,type=pa.int64()))
            table=table.set_column(table.schema.get_field_index('index'),'index',pa.array(range(i*n,(i+1)*n),type=pa.int64()))
            pq.write_table(table,root/f'data/chunk-000/episode_{i:06d}.parquet')
            for cam,p in first.cameras.items():shutil.copyfile(p,root/f'videos/chunk-000/{cam}/episode_{i:06d}.mp4')
            if speedup:
                doc=json.loads((root/'processing/episode_000000.json').read_text());doc['episode_index']=i;atomic_json(root/f'processing/episode_{i:06d}.json',doc)
        rows.append({**first.entry,'episode_index':i,'source_recording_id':f'recording-{i}'})
    info.update(total_episodes=3,total_frames=3*n,total_videos=9,total_tasks=1,total_chunks=1,splits={'train':'0:3'})
    atomic_json(root/'meta/info.json',info);(root/'meta/episodes.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in rows))


def setup_pair(tmp_path,monkeypatch):
    virtual='/mnt/data/train_platform/datasets/tomato_sharpa';server=tmp_path/'server';cache=tmp_path/'cache';native_run=subprocess.run
    def physical(path):return server/str(path).removeprefix('/mnt/data/train_platform/datasets/')
    sources={}
    for variant in ('normal','speedup'):
        path=f'{virtual}/{variant}/no-block-more/A/1102_{variant}_lerobot';source=physical(path);sources[variant]=path;full_dataset(source,speedup=variant=='speedup')
        # Three partial caches deliberately share the SAME remote package.
        for i in range(3):
            dest=cache/f'{variant}-{i}';shutil.copytree(source/'meta',dest/'meta')
            for folder in ['data/chunk-000']+[f'videos/chunk-000/observation.images.cam{c}' for c in range(3)]:
                p=dest/folder;p.mkdir(parents=True,exist_ok=True);ext='parquet' if folder.startswith('data') else 'mp4';shutil.copyfile(source/folder/f'episode_{i:06d}.{ext}',p/f'episode_{i:06d}.{ext}')
            if variant=='speedup':(dest/'processing').mkdir();shutil.copyfile(source/f'processing/episode_{i:06d}.json',dest/f'processing/episode_{i:06d}.json')
            ep=Episode(dest,i);identity=ep.identity();atomic_json(dest/'cache-source.json',{'host':'28','manifest':{'dataset':path,'files':{rel:{'sha256':value} for rel,value in identity['sha256'].items()}},'all_selected_sha256_verified':True})
    store=Store([cache],tmp_path/'review');ws=Workspace(store);ids={(e['variant'],e['episode_index']):e['id'] for e in store.entries.values()}
    for (variant,i),id_ in ids.items():
        ep=store.episode(id_);doc=ws.stages(ep)['coarse'];doc['segments']=[{'start_frame':0,'end_frame':ep.length,'label':'pickup','reviewed':not(variant=='speedup' and i==2)}];ws.save_stage(ep,'coarse',doc)
    change(ws,ids['normal',1],True,'empty hand placement',0)
    def remote(script,body):
        if script==org.READ_META:return {p:{} for p in body['sources']}
        body=copy.deepcopy(body);body['allowed_root']=str(server);body['journal']=str(physical(body['journal']));body['sources']={str(physical(p)):files for p,files in body['sources'].items()}
        for p in body['packages']:
            for key in ('destination','staging','backup'):
                if p.get(key):p[key]=str(physical(p[key]))
            for video in p['videos']:video['source']=str(physical(video['source']))
        r=native_run([sys.executable,'-c',script],input=json.dumps(body),text=True,capture_output=True)
        if r.returncode:raise ValueError(r.stderr)
        return json.loads(r.stdout)
    def run(cmd,*args,**kwargs):
        if cmd[0]=='rsync':
            src=Path(cmd[-2]);dest=physical(cmd[-1].removeprefix('28:').rstrip('/'))
            for p in src.rglob('*'):
                rel=p.relative_to(src)
                if p.is_file() and rel.parts[0]!='videos' and p.name!='cache-source.json':
                    q=dest/rel;q.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,q)
            return subprocess.CompletedProcess(cmd,0,'','')
        return native_run(cmd,*args,**kwargs)
    monkeypatch.setattr(org,'remote',remote);monkeypatch.setattr(org.subprocess,'run',run)
    return store,ws,ids,sources,physical


def test_pair_compaction_updates_real_files_all_indices_annotations_and_ui(tmp_path,monkeypatch):
    store,ws,ids,sources,physical=setup_pair(tmp_path,monkeypatch)
    try:
        before={v:{i:Episode(physical(p),i) for i in range(3)} for v,p in sources.items()}
        saved={(v,i):ws.stages(store.episode(id_))['coarse']['segments'] for (v,i),id_ in ids.items()}
        preview=plan(ws,ids['normal',0]);assert preview['ready'] and len(preview['review_warnings'])==1
        assert [r['source_episode_index'] for r in preview['packages'][0]['included']]==[0,2]
        result=org.execute(ws,ids['normal',0],preview['plan_hash'])
        assert result['selected_id']!=ids['normal',0]
        for variant,server_source in sources.items():
            path=physical(server_source);info=json.loads((path/'meta/info.json').read_text());assert info['total_episodes']==2
            tables=[pq.read_table(path/f'data/chunk-000/episode_{i:06d}.parquet') for i in range(2)]
            assert [set(t['episode_index'].to_pylist()) for t in tables]==[{0},{1}]
            assert pa.concat_tables(tables)['index'].to_pylist()==list(range(info['total_frames']))
            for new,old in enumerate([0,2]):
                original=before[variant][old];physical_keys=[k for k in original.table.column_names if k not in ('episode_index','index')]
                assert tables[new].select(physical_keys).equals(original.table.select(physical_keys))
                for cam,video in original.cameras.items():
                    # Original paths were swapped; compare to the retained backup bytes.
                    backup=path.parent.parent/'.subtask_backups'/result['operation']/path.name/video.relative_to(original.root)
                    assert sha(path/f'videos/chunk-000/{cam}/episode_{new:06d}.mp4')==sha(backup)
                annotation=json.loads((path/f'annotations/subtasks/coarse/episode_{new:06d}.json').read_text())
                assert annotation['source']['episode_index']==new and annotation['segments'][0]['reviewed']==saved[variant,old][0]['reviewed']
                if variant=='speedup':assert json.loads((path/f'processing/episode_{new:06d}.json').read_text())['episode_index']==new
            c=physical(preview['packages'][0 if variant=='normal' else 1]['c_target'])
            assert json.loads((c/'meta/info.json').read_text())['total_episodes']==1
            assert set(pq.read_table(c/'data/chunk-000/episode_000000.parquet')['episode_index'].to_pylist())=={0}
        catalog=ws.catalog();assert len(catalog['directories'])==2 and len(catalog['episodes'])==6
        good=[e for e in catalog['episodes'] if not e['excluded']]
        assert sorted((e['variant'],e['episode_index']) for e in good)==[('normal',0),('normal',1),('speedup',0),('speedup',1)]
        assert len([e for e in catalog['episodes'] if e['archived'] and e['excluded']])==2
        assert all(e['pair_state']=='matched' for e in catalog['episodes'])
        for e in catalog['episodes']:
            if e['variant']!='normal':continue
            n=store.episode(e['id']);sp=store.episode(e['pair_ids'][0]);mapped=project(n,sp,store.load(n))
            assert mapped['segments'][-1]['end_frame']==sp.length
        assert org.registry(ws)['retired'][ids['normal',2]] in {e['id'] for e in good}
        # Restart recovery reads the new roots and retains retirement/labels.
        restarted=Workspace(store);assert len(restarted.catalog()['episodes'])==6
    finally:store.lockfile.close()


def test_transaction_rolls_back_both_versions_on_mid_commit_failure(tmp_path):
    allowed=tmp_path;source=tmp_path/'A/data';source.mkdir(parents=True);(source/'old').write_text('source')
    stage=tmp_path/'staging';stage.mkdir();(stage/'new').write_text('new')
    cstage=tmp_path/'cstaging';cstage.mkdir();(cstage/'bad').write_text('bad')
    body={'operation':'test','allowed_root':str(allowed),'journal':str(tmp_path/'journal/state.json'),'sources':{},'action':'commit','test_fail_after':2,'packages':[
        {'staging':str(stage),'destination':str(source),'replace':True,'backup':str(tmp_path/'backup'),'files':{'new':sha(stage/'new')}},
        {'staging':str(cstage),'destination':str(tmp_path/'C/data'),'replace':False,'backup':None,'files':{'bad':sha(cstage/'bad')}}]}
    result=subprocess.run([sys.executable,'-c',org.TRANSACTION],input=json.dumps(body),text=True,capture_output=True)
    assert result.returncode and (source/'old').read_text()=='source'
    assert not (tmp_path/'C/data').exists() and (stage/'new').exists() and (cstage/'bad').exists()
    assert json.loads((tmp_path/'journal/state.json').read_text())['state']=='rolled_back'


def test_restart_recovers_commit_when_ssh_reply_was_lost(tmp_path,monkeypatch):
    store,ws,ids,_,_=setup_pair(tmp_path,monkeypatch)
    real_remote=org.remote
    lost=[False]
    def remote(script,body):
        result=real_remote(script,body)
        if script==org.TRANSACTION and body['action']=='commit' and not lost[0]:
            lost[0]=True;raise ValueError('SSH response lost after commit')
        return result
    monkeypatch.setattr(org,'remote',remote)
    try:
        with pytest.raises(ValueError,match='SSH response'):org.execute(ws,ids['normal',0],plan(ws,ids['normal',0])['plan_hash'])
        assert not org.registry(ws)['operations']
        recovered=Workspace(store);active=recovered.catalog()['episodes']
        assert len(active)==6 and sum(not e['excluded'] for e in active)==4
        assert org.registry(recovered)['operations']
        assert all(e['pair_state']=='matched' for e in active)
    finally:store.lockfile.close()


def test_http_execution_redirects_old_episode_and_retains_new_pair(tmp_path,monkeypatch):
    import threading
    import urllib.request
    from convest.subtasks.server import Server
    store,ws,ids,_,_=setup_pair(tmp_path,monkeypatch);store.lockfile.close()
    server=Server(('127.0.0.1',0),[tmp_path/'cache'],tmp_path/'review')
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start();base=f'http://127.0.0.1:{server.server_port}'
    try:
        preview=json.load(urllib.request.urlopen(base+'/api/workspace/organize-plan?id='+ids['normal',2]))
        req=urllib.request.Request(base+'/api/workspace/organize',data=json.dumps({'id':ids['normal',2],'plan_hash':preview['plan_hash']}).encode(),headers={'Content-Type':'application/json','X-Subtasks-Token':server.token})
        result=json.load(urllib.request.urlopen(req))['result']
        assert result['selected_id']!=ids['normal',2]
        view=json.load(urllib.request.urlopen(base+'/api/workspace/episode?id='+ids['normal',2]))
        assert view['episode']['episode_index']==1 and view['episode']['id']==result['selected_id']
        assert view['pair']['state']=='matched'
        assert not server.export_lock.locked()
    finally:server.shutdown();server.server_close();worker.join()


def test_second_compaction_appends_to_existing_c_and_flattens_old_links(tmp_path,monkeypatch):
    store,ws,ids,sources,physical=setup_pair(tmp_path,monkeypatch)
    try:
        org.execute(ws,ids['normal',0],plan(ws,ids['normal',0])['plan_hash'])
        current=next(e for e in ws.catalog()['episodes'] if e['variant']=='normal' and not e['excluded'] and e['episode_index']==1)
        change(ws,current['id'],True,'another rejected demonstration',1)
        retained=next(e for e in ws.catalog()['episodes'] if e['variant']=='normal' and not e['excluded'])
        preview=plan(ws,retained['id']);assert preview['ready']
        assert all(len(p['included'])==1 and len(p['rejected'])==2 for p in preview['packages'])
        org.execute(ws,retained['id'],preview['plan_hash'])
        cat=ws.catalog();assert sum(not e['excluded'] for e in cat['episodes'])==2
        assert sum(e['archived'] and e['excluded'] for e in cat['episodes'])==4
        assert all(e['pair_state']=='matched' for e in cat['episodes'])
        redirected=org.registry(ws)['retired'][ids['normal',2]]
        assert next(e for e in cat['episodes'] if e['id']==redirected)['excluded']
        for variant,source in sources.items():
            assert json.loads((physical(source)/'meta/info.json').read_text())['total_episodes']==1
            c=physical(next(p for p in preview['packages'] if p['variant']==variant)['c_target'])
            assert json.loads((c/'meta/info.json').read_text())['total_episodes']==2
    finally:store.lockfile.close()


def test_compaction_preserves_unsaved_coarse_only_proposal(tmp_path,monkeypatch):
    store,ws,ids,sources,physical=setup_pair(tmp_path,monkeypatch)
    try:
        old=ids['normal',2];ep=ws.store.episode(old);doc=ws.store.load(ep)
        ws.stage_path(old,'coarse').unlink()
        atomic_json(ws.root/'coarse-proposals'/(old+'.json'),{'source':doc['source'],
            'base_annotation_revision':doc['revision'],'segments':[{'start_frame':0,'end_frame':ep.length,'label':'harvest','reviewed':False}],
            'warning':'coarse overview'})
        result=org.execute(ws,old,plan(ws,old)['plan_hash'])
        new=result['selected_id'];layers=ws.stages(ws.store.episode(new))
        assert layers['coarse']['segments'][0]['label']=='harvest'
        assert layers['coarse']['saved_at'] is None and layers['coarse']['processing_mode']=='coarse'
        assert layers['fine']['segments'][0]['label']=='unknown'
    finally:ws.store.lockfile.close()
