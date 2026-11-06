"""Durable two-pass processing: finish the initial order, then retry failures once."""
import copy
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

from .core import atomic_json


def active(ws):
    path=ws.root/'active-processing-batch.json'
    if not path.exists():return None
    return json.loads(path.read_text())


def checkpoint(ws,batch):
    batch['updated_at']=time.time()
    atomic_json(ws.root/'processing-batches'/(batch['id']+'.json'),batch)
    atomic_json(ws.root/'active-processing-batch.json',batch)


def remaining(ws,mode='coarse',variant='normal'):
    rows=ws.catalog()['episodes']
    rows=[e for e in rows if e['variant']==variant and not e['excluded'] and not e.get('archived')
          and not e[mode+'_saved'] and not e.get(mode+'_ready') and not e['human_reviewed']]
    def key(e):
        path=Path(e['remote_directory']);grade=next((s for s in path.parts if s in {'A','B','C'}),'C')
        return grade,path.name,e['episode_index']
    return [e['id'] for e in sorted(rows,key=key)]


def create(ws,ids,mode):
    if mode not in {'coarse','fine'}:raise ValueError('无效处理模式')
    if not ids or len(ids)!=len(set(ids)) or any(i not in ws.store.entries for i in ids):raise ValueError('没有可排队的片段')
    previous=active(ws)
    if previous and previous['state'] in {'queued','running'}:raise ValueError('已有持久处理队列')
    # Keep normal sources ahead of speedup dependencies, preserving UI A/B order.
    ids=sorted(ids,key=lambda i:ws.store.entries[i]['variant']=='speedup')
    batch={'id':uuid.uuid4().hex,'mode':mode,'state':'queued','order':ids,'phase':'initial',
           'initial_done':[],'retry_order':None,'retry_done':[],'results':{},'current_id':None,'created_at':time.time()}
    checkpoint(ws,batch)
    with ws.lock:
        for id_ in ids:ws.queue[id_]={'state':'queued','mode':mode,'batch_id':batch['id']}
        ws._checkpoint()
    return batch


def run(ws,server,batch_id):
    batch=active(ws)
    if not batch or batch['id']!=batch_id:raise ValueError('处理队列来源不一致')
    workers=max(1,min(8,getattr(server,'processing_workers',1)))
    batch.update(state='running',workers=workers,current_ids=[]);checkpoint(ws,batch)
    for phase in ('initial','retry'):
        if phase=='retry' and batch['retry_order'] is None:
            batch['retry_order']=[i for i in batch['order'] if batch['results'].get(i,{}).get('state') in {'failed','interrupted','waiting_normal'}]
            checkpoint(ws,batch)
        order=batch['order'] if phase=='initial' else batch['retry_order']
        done=batch['initial_done'] if phase=='initial' else batch['retry_done']
        def process_one(id_):
            # New response identity on retry avoids replaying the same malformed cached output.
            namespace=(batch['id']+':retry:'+id_) if phase=='retry' else None
            try:
                result=ws.process([id_],server,mode=batch['mode'],retry_namespace=namespace)['episodes'][0]
            except Exception as exc:
                result={'id':id_,'state':'failed','error':str(exc)}
                with ws.lock:ws.queue[id_]={**result,'mode':batch['mode']};ws._checkpoint()
            return result
        # Only this coordinator updates the durable manifest. Each worker owns a
        # different episode; Workspace/Store retain their existing write locks.
        pending=iter((index,id_) for index,id_ in enumerate(order) if id_ not in done)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            running={}
            exhausted=False
            while running or not exhausted:
                while len(running)<workers and not exhausted:
                    try:index,id_=next(pending)
                    except StopIteration:exhausted=True;break
                    running[pool.submit(process_one,id_)]=(index,id_)
                    batch.update(phase=phase,current_ids=[item[1] for item in running.values()],
                                 current_id=next(iter(running.values()))[1],current_index=index+1,current_total=len(order))
                    checkpoint(ws,batch)
                if not running:break
                finished,_=wait(running,return_when=FIRST_COMPLETED)
                for future in finished:
                    index,id_=running.pop(future)
                    result=future.result()
                    history=batch['results'].get(id_,{}).get('attempts',[])
                    batch['results'][id_]={**result,'attempts':history+[{'phase':phase,'state':result['state'],'error':result.get('error'),'finished_at':time.time()}]}
                    done.append(id_)
                batch['current_ids']=[item[1] for item in running.values()]
                batch['current_id']=batch['current_ids'][0] if running else None
                checkpoint(ws,batch)
    batch['state']='completed';batch['current_id']=None;batch['current_ids']=[];checkpoint(ws,batch)
    return {'episodes':[batch['results'][i] for i in batch['order']], 'batch_id':batch['id'],
            'retry_count':len(batch['retry_order']), 'warning':'两轮后仍失败的片段保留错误，需人工处理。'}


def summary(ws):
    batch=active(ws)
    if not batch:return None
    return {k:copy.deepcopy(batch.get(k)) for k in ('id','mode','state','phase','current_id','current_ids','current_index','current_total','workers')}|{
        'total':len(batch['order']),'initial_completed':len(batch['initial_done']),
        'retry_total':len(batch['retry_order'] or []),'retry_completed':len(batch['retry_done']),
        'failed':sum(r['state']=='failed' for r in batch['results'].values())}
