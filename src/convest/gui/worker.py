"""Four independent processing/upload roles, supervised with graceful drain on stop."""
import argparse
import fcntl
import json
import os
import re
from pathlib import Path
import signal
import subprocess
import sys
import time
from convest.config import digest
from convest.ingest.batches import durable_json
from convest.ingest.inputs import require_same
from convest.capture import daily, ledger
from convest.capture.retention import delete_original
from convest.capture.worker import context as capture_context, scan
from convest.training import worker as training
from .transport import transfer, verify_backup


def read(p): return json.loads(Path(p).read_text())


def mirror_capture(spec,queue):
    import hashlib
    import shutil
    state=Path(spec['capture']['state_root'])
    if not (state/'recordings.csv').exists() or not queue.db.execute('SELECT count(*) FROM recordings').fetchone()[0]:return
    ack=read(state/'ledger_mirror.json') if (state/'ledger_mirror.json').exists() else {}
    if ack.get('retry_after_ns',0)>time.time_ns():return
    with (state/'ledger.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_SH)
        data=(state/'recordings.csv').read_bytes();document=read(state/'recordings.json')
    signature=hashlib.sha256(data).hexdigest()
    if ack.get('csv_sha256')==signature:return
    source=state/'ledger-snapshots'/signature
    if not source.exists():
        source.mkdir(parents=True);(source/'recordings.csv').write_bytes(data)
        durable_json(source/'recordings.json',document)
    producer=queue.db.execute('SELECT identity FROM capture_producer WHERE id=1').fetchone()[0]
    dest=Path(spec['task']['lerobot_root'])/'.workflow-ledgers'/spec['task'].get('id','task')/producer/signature
    try:
        from convest.capture.transfer import manifest
        result=transfer(spec['data_server'],source,dest,manifest(source),signature)
        durable_json(state/'ledger_mirror.json',{'csv_sha256':signature,'destination':str(dest),'receipt':result['receipt']})
    except Exception as e:
        durable_json(state/'ledger_mirror.json',{'error':str(e),'retry_after_ns':time.time_ns()+30*10**9})
        print('台账远端备份待重试：'+str(e),flush=True)


def capture_upload(spec, queue):
    cfg = spec['capture']; task = spec['task']; db = queue.db
    rows = [dict(r) for r in db.execute("SELECT * FROM uploads WHERE state IN ('pending','retry','uploading') AND next_ns<=? ORDER BY updated_ns,id", (time.time_ns(),))]
    if not rows:
        mirror_capture(spec,queue)
        return False
    streak = db.execute('SELECT raw_streak FROM capture_schedule WHERE id=1').fetchone()[0]
    batches = [r for r in rows if json.loads(r['payload'])['kind']=='batch']
    raw = [r for r in rows if json.loads(r['payload'])['kind']=='raw']
    row = batches[0] if batches and (not raw or streak) else raw[0]
    p = json.loads(row['payload']); kind = p['kind']
    def update(**kw):
        kw['updated_ns'] = time.time_ns()
        with db: db.execute('UPDATE uploads SET '+','.join(k+'=?' for k in kw)+' WHERE id=?', [*kw.values(),row['id']])
    def progress(**kw):
        value={'route':row['route'],'episode':row['episode'],'kind':kind,'updated_ns':time.time_ns(),**kw}
        durable_json(Path(cfg['state_root'])/'upload_progress.json',value)
        durable_json(Path(cfg['state_root'])/('upload_progress_'+kind+'.json'),value)
    try:
        update(state='uploading', error=None)
        identity = digest([cfg['fingerprint'], row['id']])
        if kind == 'raw':
            dest = Path(task['raw_root'])/p['group']/p['episode']
            def guard(): require_same(p['source'],p['source_snapshot'])
            if not row['raw_done']:
                receipt = transfer(spec['data_server'],p['source'],dest,p['raw_files'],identity,progress,guard)
                durable_json(Path(cfg['state_root'])/'receipts'/(row['id']+'.json'), receipt)
                update(raw_done=1)
            else:
                receipt = verify_backup(spec['data_server'], dest, p['raw_files'], identity)
            ledger.change(queue,p,{'raw_uploaded_ns':receipt['completed_ns'],'raw_receipt':receipt['receipt']},'raw_uploaded')
            ledger.export(cfg,queue)
            if cfg['delete_raw_after_upload']:
                progress(phase='deleting_verified_original')
                delete_original(cfg,queue,p,row['id'])
            with db: db.execute('UPDATE capture_schedule SET raw_streak=raw_streak+1 WHERE id=1')
            update(state='completed', next_ns=0, error=None)
        else:
            dest = Path(task['lerobot_root'])/p['group']/p['batch_name']
            def guard(): require_same(p['dataset'],p['dataset_snapshot'])
            receipt = transfer(spec['data_server'],p['dataset'],dest,p['dataset_files'],identity,progress,guard)
            durable_json(Path(cfg['state_root'])/'receipts'/(row['id']+'.json'),receipt)
            with db:
                db.execute("UPDATE capture_batches SET state='uploaded',uploaded_ns=?,remote_path=? WHERE route=? AND batch_id=?",
                           (receipt['completed_ns'],str(dest),row['route'],p['batch_id']))
                ledger.event(queue,row['route'],p['batch_id'],'batch_uploaded',{'destination':str(dest)})
                db.execute('UPDATE capture_schedule SET raw_streak=0 WHERE id=1')
            update(state='completed', dataset_done=1, next_ns=0, error=None)
        total=sum(f['size'] for f in p['raw_files' if kind=='raw' else 'dataset_files'])
        progress(phase='completed',destination=str(dest),bytes_done=total,bytes_total=total); ledger.export(cfg,queue)
    except Exception as e:
        attempts=row['attempts']+1
        update(state='retry', attempts=attempts, error=f'{type(e).__name__}: {e}', next_ns=time.time_ns()+cfg['retry_seconds']*10**9)
        progress(phase='retry',error=str(e));ledger.export(cfg,queue)
    mirror_capture(spec,queue)
    return True


def training_destination_name(payload, variant):
    if variant not in ('normal', 'speedup'):raise ValueError('Invalid training variant')
    batch=payload.get('batch_name', '')
    if re.fullmatch(r'[0-9]{4}_[0-9]+_[0-9]+_data_lerobot', batch):
        return batch.removesuffix('_data_lerobot')+'_'+variant+'_lerobot'
    # Existing queued payloads retain their original destination and receipt identity.
    return payload['name']


def training_upload(spec, db):
    cfg=spec['training'];task=spec['task']
    row=db.execute("SELECT * FROM tasks WHERE state IN ('local_ready','upload_retry','uploading') AND next_ns<=? ORDER BY updated_ns,id LIMIT 1",(time.time_ns(),)).fetchone()
    if not row: return False
    p=json.loads(row['payload']); tid=row['id']; results={}
    def guard(): require_same(p['source'],p['snapshot'])
    try:
        training.update(db,tid,state='uploading',error=None)
        for variant in ('normal','speedup'):
            def progress(**kw):
                value={'task':tid,'route':p['robot']+'-'+p['group'],'variant':variant,'updated_ns':time.time_ns(),**kw}
                durable_json(Path(cfg['state_root'])/'upload_progress.json',value)
                durable_json(Path(cfg['state_root'])/('upload_progress_'+variant+'.json'),value)
            files=[{**f,'path':f['path'][len(variant)+1:]} for f in p['files'] if f['path'].startswith(variant+'/')]
            if not files: raise ValueError('训练成品文件清单为空')
            dest=Path(task[variant+'_root'])/p['group']/training_destination_name(p,variant)
            results[variant]=transfer(spec['training_server'],Path(p['source'])/variant,dest,files,
                                     digest([cfg['fingerprint'],tid,variant]),progress,guard)
        # The two roots cannot share one atomic rename. Publish paired completion
        # evidence only after BOTH immutable variants have passed remote SHA checks.
        import shutil
        from convest.capture.transfer import manifest
        audit=Path(cfg['state_root'])/'pair-reports'/tid
        if not (audit/'completion.json').exists():
            audit.mkdir(parents=True,exist_ok=True)
            for name in ('source.json','pipeline.json','normal.complete.json','speedup.complete.json'):
                shutil.copyfile(Path(p['source'])/name,audit/name)
            durable_json(audit/'completion.json',{'status':'completed','task':tid,'robot':p['robot'],'group':p['group'],
                         'name':p['name'],'variants':results,'created_ns':time.time_ns()})
        report_receipts={}
        for variant in ('normal','speedup'):
            dest=Path(task[variant+'_root'])/p['group']/'.workflow-pairs'/p['name']
            report_receipts[variant]=transfer(spec['training_server'],audit,dest,manifest(audit),
                                           digest([cfg['fingerprint'],tid,'pair-report']),guard=guard)
        durable_json(Path(cfg['state_root'])/'receipts'/(tid+'.json'),
                     {'status':'completed','variants':results,'pair_reports':report_receipts})
        training.update(db,tid,state='uploaded',next_ns=0,error=None)
    except Exception as e:
        training.update(db,tid,state='upload_retry',error=f'{type(e).__name__}: {e}',next_ns=time.time_ns()+cfg['retry_seconds']*10**9)
        durable_json(Path(cfg['state_root'])/'upload_progress.json',{'task':tid,'phase':'retry','error':str(e),'updated_ns':time.time_ns()})
    return True


class VideoProgress:
    def __init__(self,stream,cfg):self.stream=stream;self.cfg=cfg;self.buffer=''
    def write(self,value):
        import re
        self.stream.write(value);self.buffer+=value
        while '\n' in self.buffer:
            line,self.buffer=self.buffer.split('\n',1)
            m=re.search(r'Videos complete: (\d+)/(\d+)',line)
            if m:
                state=Path(self.cfg['state_root']);current=read(state/'progress.json')
                durable_json(state/'video_progress.json',{'done':int(m[1]),'total':int(m[2]),'task':current.get('task'),'updated_ns':time.time_ns()})
        return len(value)
    def flush(self):self.stream.flush()


def role(spec, name):
    stop=[False]
    def request(*_): stop[0]=True
    signal.signal(signal.SIGTERM,request);signal.signal(signal.SIGINT,request)
    capture=name.startswith('capture')
    cfg=spec['capture'] if capture else spec['training']
    context=capture_context if capture else training.context
    if name=='training-process':sys.stdout=VideoProgress(sys.stdout,cfg)
    with context(cfg,'upload' if name.endswith('upload') else 'processing') as q:
        while not stop[0]:
            if name=='capture-process': scan(cfg,q,should_stop=lambda:stop[0])
            elif name=='capture-upload': capture_upload(spec,q)
            elif name=='training-process':
                training.discover(cfg,q)
                if not stop[0]:training.process_once(cfg,q)
            else:
                training_upload(spec,q)
                from .retention import release_complete_batches
                release_complete_batches(spec,q,training_destination_name)
            for _ in range(cfg['poll_seconds']*4):
                if stop[0]:break
                time.sleep(.25)


def supervise(spec):
    root=Path(spec['root']); children={};state='running';error=None;stop=[False]
    def request(*_):stop[0]=True
    signal.signal(signal.SIGTERM,request);signal.signal(signal.SIGINT,request)
    with (root/'supervisor.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            # Initialize ownership and schema before independent roles race to open them.
            with capture_context(spec['capture']) as q:ledger.export(spec['capture'],q)
            with training.context(spec['training']):pass
            for name in ('capture-process','capture-upload','training-process','training-upload'):
                with (root/(name+'.log')).open('a') as log:
                    children[name]=subprocess.Popen([sys.executable,'-u','-m','convest.gui.worker','--spec',str(root/'spec.json'),'--role',name],stdout=log,stderr=subprocess.STDOUT)
            while True:
                if (root/'stop-request.json').exists():stop[0]=True
                if not stop[0] and any(c.poll() is not None for c in children.values()):
                    error='工作进程意外退出：'+', '.join(n+' ('+str(c.poll())+')' for n,c in children.items() if c.poll() is not None)+'，请查看日志后重试';stop[0]=True
                if stop[0] and state=='running':
                    state='stopping'
                    for c in children.values():
                        if c.poll() is None:c.send_signal(signal.SIGTERM)
                durable_json(root/'runtime.json',{'state':state,'pid':os.getpid(),'heartbeat_ns':time.time_ns(),
                             'error':error,'children':{n:{'pid':c.pid,'exit_code':c.poll()} for n,c in children.items()}})
                if stop[0] and all(c.poll() is not None for c in children.values()):break
                time.sleep(.5)
        except Exception as exc:
            error=f'{type(exc).__name__}: {exc}'
            print(error,flush=True)
        finally:
            for c in children.values():
                if c.poll() is None:c.send_signal(signal.SIGTERM)
            for c in children.values():c.wait()
            durable_json(root/'runtime.json',{'state':'error' if error else 'stopped','pid':os.getpid(),
                         'heartbeat_ns':time.time_ns(),'error':error,'children':{n:{'pid':c.pid,'exit_code':c.poll()} for n,c in children.items()}})


def main():
    p=argparse.ArgumentParser();p.add_argument('--spec',type=Path,required=True)
    p.add_argument('--role',choices=['supervisor','capture-process','capture-upload','training-process','training-upload'],default='supervisor')
    args=p.parse_args();spec=read(args.spec)
    supervise(spec) if args.role=='supervisor' else role(spec,args.role)

if __name__=='__main__':main()
