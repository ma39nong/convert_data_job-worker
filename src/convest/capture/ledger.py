"""Durable source lifecycle journal and live human-readable CSV/JSON ledger."""
from pathlib import Path
import csv
import datetime
import hashlib
import uuid
import fcntl
import io
import json
import os
import sqlite3
import time
from convest.ingest.batches import durable_json


def init(queue):
    queue.db.row_factory=sqlite3.Row
    queue.db.executescript('''
    CREATE TABLE IF NOT EXISTS recordings(
      route TEXT,episode TEXT,identity TEXT,payload TEXT,batch_id TEXT,
      converted_ns INTEGER,raw_uploaded_ns INTEGER,raw_receipt TEXT,
      deletion_state TEXT DEFAULT 'retained',quarantine TEXT,raw_deleted_ns INTEGER,
      PRIMARY KEY(route,episode));
    CREATE TABLE IF NOT EXISTS capture_batches(
      route TEXT,batch_id TEXT,name TEXT,generation TEXT,state TEXT,
      opened_ns INTEGER,deadline_ns INTEGER,frames INTEGER,sources TEXT,
      uploaded_ns INTEGER,remote_path TEXT,PRIMARY KEY(route,batch_id));
    CREATE TABLE IF NOT EXISTS capture_events(
      id INTEGER PRIMARY KEY AUTOINCREMENT,route TEXT,episode TEXT,event TEXT,
      recorded_ns INTEGER,detail TEXT);
    CREATE TABLE IF NOT EXISTS capture_producer(id INTEGER PRIMARY KEY,identity TEXT);
    CREATE TABLE IF NOT EXISTS capture_schedule(id INTEGER PRIMARY KEY,raw_streak INTEGER);
    INSERT OR IGNORE INTO capture_schedule VALUES(1,0);
    ''')
    queue.db.execute('INSERT OR IGNORE INTO capture_producer VALUES(1,?)',(uuid.uuid4().hex,))
    queue.db.commit()


def event(queue,route,episode,name,detail=None):
    queue.db.execute('INSERT INTO capture_events(route,episode,event,recorded_ns,detail) VALUES(?,?,?,?,?)',
                     (route,episode,name,time.time_ns(),json.dumps(detail or {},ensure_ascii=False)))


def converted(queue,payload):
    old=queue.db.execute('SELECT identity FROM recordings WHERE route=? AND episode=?',(payload['route'],payload['episode'])).fetchone()
    if old and old[0]!=payload['identity']:
        raise ValueError('Recording ledger identity conflict')
    if not old:
        with queue.db:
            queue.db.execute('INSERT INTO recordings(route,episode,identity,payload,batch_id,converted_ns) VALUES(?,?,?,?,?,?)',
                             (payload['route'],payload['episode'],payload['identity'],json.dumps(payload),payload['batch_id'],time.time_ns()))
            event(queue,payload['route'],payload['episode'],'converted',{'batch_id':payload['batch_id']})


def change(queue,payload,values,name):
    with queue.db:
        queue.db.execute('UPDATE recordings SET '+','.join(k+'=?' for k in values)+' WHERE route=? AND episode=?',
                         [*values.values(),payload['route'],payload['episode']])
        event(queue,payload['route'],payload['episode'],name,values)


def export(cfg,queue):
    """Serialize exporters across processing/upload processes; DB is the truth."""
    state=Path(cfg['state_root'])
    with (state/'ledger.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        with sqlite3.connect(state/'queue.sqlite3') as db:
            db.row_factory=sqlite3.Row
            db.execute('BEGIN')
            rows=[dict(r) for r in db.execute('''SELECT j.route,j.episode,j.state AS local_state,j.error,u.state AS raw_upload_state,u.error AS raw_upload_error,
              r.identity,r.converted_ns,r.raw_uploaded_ns,r.raw_receipt,r.deletion_state,
              r.raw_deleted_ns,r.batch_id,b.name AS batch_name,b.deadline_ns,
              b.state AS batch_state,b.uploaded_ns AS batch_uploaded_ns,b.remote_path AS remote_dataset
              FROM jobs j LEFT JOIN recordings r ON j.route=r.route AND j.episode=r.episode
              LEFT JOIN uploads u ON j.route=u.route AND j.episode=u.episode
              LEFT JOIN capture_batches b ON r.route=b.route AND r.batch_id=b.batch_id
              ORDER BY j.route,CAST(substr(j.episode,8) AS INTEGER)''')]
            batches=[dict(r) for r in db.execute('SELECT * FROM capture_batches ORDER BY route,batch_id')]
        routes={r['id']:r for r in cfg['routes']}
        for row in rows:
            route=routes[row['route']]
            row['local_original']=str(Path(route['source'])/row['episode'])
            row['remote_original']=str(Path(cfg['remote_raw_root'])/route['robot']/route['group']/row['episode'])
            for src,dest in [('converted_ns','converted_at'),('raw_uploaded_ns','raw_uploaded_at'),('raw_deleted_ns','raw_deleted_at'),('deadline_ns','batch_deadline_at'),('batch_uploaded_ns','batch_uploaded_at')]:
                row[dest]=datetime.datetime.fromtimestamp(row[src]/1e9).astimezone().isoformat(timespec='seconds') if row.get(src) else ''
        durable_json(state/'recordings.json',{'updated_ns':time.time_ns(),'recordings':rows,'batches':batches})
        columns=['route','episode','local_state','raw_upload_state','raw_upload_error','identity','converted_at','raw_uploaded_at','deletion_state','raw_deleted_at',
                 'batch_id','batch_name','batch_deadline_at','batch_state','batch_uploaded_at','local_original','remote_original','remote_dataset','error']
        stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=columns,extrasaction='ignore')
        writer.writeheader();writer.writerows(rows)
        tmp=state/'recordings.csv.tmp';tmp.write_text(stream.getvalue(),encoding='utf-8-sig')
        with tmp.open('rb') as f:os.fsync(f.fileno())
        tmp.replace(state/'recordings.csv')


def mirror(cfg,queue):
    """Uploader-only small remote ledger backup; never blocks local processing."""
    from .transfer import check_mount,ensure_real_path
    state=Path(cfg['state_root'])
    if not (state/'recordings.csv').exists():return
    with (state/'ledger.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_SH)
        data=(state/'recordings.csv').read_bytes()
        document=json.loads((state/'recordings.json').read_text())
    signature=hashlib.sha256(data).hexdigest()
    ack=state/'ledger_mirror.json'
    if ack.exists() and json.loads(ack.read_text()).get('csv_sha256')==signature:return
    check_mount(cfg)
    producer=queue.db.execute('SELECT identity FROM capture_producer WHERE id=1').fetchone()[0]
    remote=Path(cfg['remote_dataset_root'])/'.capture-ledgers'/cfg['fingerprint']/producer
    ensure_real_path(remote);remote.mkdir(parents=True,exist_ok=True)
    atomic=remote/'recordings.json'
    from convest.config import atomic_json
    atomic_json(atomic,document)
    with atomic.open('rb') as f:os.fsync(f.fileno())
    tmp=remote/'recordings.csv.tmp';tmp.write_bytes(data)
    with tmp.open('rb') as f:os.fsync(f.fileno())
    tmp.replace(remote/'recordings.csv')
    if hashlib.sha256((remote/'recordings.csv').read_bytes()).hexdigest()!=signature:
        raise ValueError('Remote ledger read-back differs')
    durable_json(ack,{'csv_sha256':signature,'remote':str(remote),'mirrored_ns':time.time_ns()})
