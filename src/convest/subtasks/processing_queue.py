"""Durable processing with legacy one-retry and user-authorized continuous retries."""
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


def remaining(ws,mode='coarse',variant='normal',workflow=None):
    rows=ws.catalog()['episodes']
    rows=[e for e in rows if e['variant']==variant and not e['excluded'] and not e.get('archived')
          and not e.get('superseded') and (workflow is None or e.get('workflow')==workflow)
          and (mode!='fine' or e.get('workflow')!='without_return' or e.get('coarse_saved'))
          and not e[mode+'_saved'] and not e.get(mode+'_ready') and not e['human_reviewed']]
    def key(e):
        path=Path(e['remote_directory']);grade=next((s for s in path.parts if s in {'A','B','C'}),'C')
        return grade,path.name,e['episode_index']
    return [e['id'] for e in sorted(rows,key=key)]


def create(ws,ids,mode,workers=None,provider='doubao',retry_policy='once'):
    if retry_policy not in {'once','until_success'}:raise ValueError('未知重试策略')
    if workers is not None and (type(workers) is not int or not 1<=workers<=8):raise ValueError('同时处理数量须为1–8的整数')
    if mode not in {'coarse','fine'}:raise ValueError('无效处理模式')
    if provider not in {'doubao','qwen'}:raise ValueError('未知标注模型')
    if not ids or len(ids)!=len(set(ids)) or any(i not in ws.store.entries for i in ids):raise ValueError('没有可排队的片段')
    if any(e.get('superseded') for e in ws.catalog()['episodes'] if e['id'] in ids):raise ValueError('所选旧三路版本已由四路ROI版本替代，请选择当前数据')
    previous=active(ws)
    if previous and previous['state'] in {'queued','running'}:raise ValueError('已有持久处理队列')
    # Keep normal sources ahead of speedup dependencies, preserving UI A/B order.
    ids=sorted(ids,key=lambda i:ws.store.entries[i]['variant']=='speedup')
    batch={'id':uuid.uuid4().hex,'mode':mode,'provider':provider,'state':'queued','order':ids,'phase':'initial',
           'retry_policy':retry_policy,'initial_done':[],'retry_order':None,'retry_done':[],'results':{},'current_id':None,'created_at':time.time()}
    if workers is not None:batch['workers']=workers
    checkpoint(ws,batch)
    with ws.lock:
        for id_ in ids:ws.queue[id_]={'state':'queued','mode':mode,'batch_id':batch['id']}
        ws._checkpoint()
    return batch


def _run_once(ws,server,batch_id,*,finish=True):
    batch=active(ws)
    if not batch or batch['id']!=batch_id:raise ValueError('处理队列来源不一致')
    workers=max(1,min(8,batch.get('workers',getattr(server,'processing_workers',1))))
    batch.update(state='running',workers=workers,current_ids=[]);checkpoint(ws,batch)
    for phase in ('initial','retry'):
        if phase=='retry' and batch['retry_order'] is None:
            batch['retry_order']=[i for i in batch['order'] if batch['results'].get(i,{}).get('state') in {'failed','interrupted','waiting_normal'}]
            checkpoint(ws,batch)
        order=batch['order'] if phase=='initial' else batch['retry_order']
        done=batch['initial_done'] if phase=='initial' else batch['retry_done']
        def process_one(id_):
            # New response identity on retry avoids replaying the same malformed cached output.
            namespace=(batch['id']+':retry:'+str(batch.get('retry_round',1))+':'+id_) if phase=='retry' else None
            try:
                result=ws.process([id_],server,mode=batch['mode'],retry_namespace=namespace,provider=batch.get('provider','doubao'))['episodes'][0]
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
                if stop_requested(ws,batch_id):exhausted=True
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
                    batch['results'][id_]={**result,'attempts':history+[{'phase':phase,'round':batch.get('retry_round',1) if phase=='retry' else 0,'state':result['state'],'error':result.get('error'),'finished_at':time.time()}]}
                    done.append(id_)
                batch['current_ids']=[item[1] for item in running.values()]
                batch['current_id']=batch['current_ids'][0] if running else None
                checkpoint(ws,batch)
    batch['state']=('stopped' if stop_requested(ws,batch_id) else 'completed') if finish else 'running';batch['current_id']=None;batch['current_ids']=[];checkpoint(ws,batch)
    return {'episodes':[batch['results'][i] for i in batch['order'] if i in batch['results']], 'batch_id':batch['id'],
            'retry_count':len(batch['retry_order']), 'warning':'首轮处理后只重试失败项一次，成功和已保存结果不重复处理。'}


def summary(ws):
    batch=active(ws)
    if not batch:return None
    return {k:copy.deepcopy(batch.get(k)) for k in ('id','mode','provider','state','phase','current_id','current_ids','current_index','current_total','workers','retry_policy','retry_round','next_retry_at','attention_reason')}|{
        'total':len(batch['order']),'initial_completed':len(batch['initial_done']),
        'retry_total':len(batch['retry_order'] or []),'retry_completed':len(batch['retry_done']),
        'stop_requested':stop_requested(ws,batch['id']),
        'failed':sum(r['state']=='failed' for r in batch['results'].values())}


def stop_requested(ws,batch_id):
    return (ws.root/'processing-stop'/(batch_id+'.json')).exists()


def request_stop(ws,batch_id):
    batch=active(ws)
    if not batch or batch['id']!=batch_id or batch['state'] not in {'queued','running'}:
        raise ValueError('没有匹配的活动队列')
    atomic_json(ws.root/'processing-stop'/(batch_id+'.json'),{'at':time.time(),'reason':'user_requested'})
    return {'batch_id':batch_id,'stop_requested':True}


def run(ws,server,batch_id):
    batch=active(ws)
    if not batch or batch['id']!=batch_id:raise ValueError('处理队列来源不一致')
    if batch.get('retry_policy','once')!='until_success':return _run_once(ws,server,batch_id)
    batch.setdefault('retry_round',1);checkpoint(ws,batch)
    while True:
        result=_run_once(ws,server,batch_id,finish=False);batch=active(ws)
        pending=[i for i in batch['order'] if batch['results'].get(i,{}).get('state') in {'failed','interrupted','waiting_normal'}]
        if stop_requested(ws,batch_id):
            batch.update(state='stopped',current_ids=[],current_id=None,next_retry_at=None)
            break
        if not pending:
            batch.update(state='completed',current_ids=[],current_id=None,next_retry_at=None)
            break
        # Credentials and source identity cannot be repaired by repeated paid calls.
        fatal=[batch['results'][i].get('error','') for i in pending if any(x in batch['results'][i].get('error','') for x in ('HTTP 401','HTTP 403','密钥未配置','来源已变化','来源变化'))]
        if fatal:
            batch.update(state='needs_attention',attention_reason=fatal[0],next_retry_at=None)
            break
        # Back off between rounds, but keep the stop endpoint responsive.
        delay=min(60,2**min(batch['retry_round'],5));batch['next_retry_at']=time.time()+delay;checkpoint(ws,batch)
        deadline=time.monotonic()+delay
        while time.monotonic()<deadline and not stop_requested(ws,batch_id):time.sleep(.2)
        if stop_requested(ws,batch_id):
            batch.update(state='stopped',current_ids=[],current_id=None,next_retry_at=None)
            break
        batch.setdefault('retry_round_history',[]).append({'round':batch['retry_round'],'order':batch['retry_order'],'done':batch['retry_done']})
        batch.update(retry_round=batch['retry_round']+1,retry_order=pending,retry_done=[],next_retry_at=None)
        checkpoint(ws,batch)
    checkpoint(ws,batch)
    return {'episodes':[batch['results'][i] for i in batch['order'] if i in batch['results']],
        'batch_id':batch_id,'state':batch['state'],'retry_round':batch['retry_round'],
        'warning':'失败项持续重试，成功和人工保存结果跳过；用户停止或凭据/来源问题会结束队列。'}
