"""Single consumer, SQLite journal. Publication is reconciled before any retry."""
import json
import sqlite3
import time
from pathlib import Path


class Queue:
    def __init__(self,path):
        self.db=sqlite3.connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs(
            route TEXT,episode TEXT,identity TEXT,state TEXT,attempts INTEGER DEFAULT 0,
            next_ns INTEGER DEFAULT 0,updated_ns INTEGER,error TEXT,proof TEXT,
            PRIMARY KEY(route,episode))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS observations(
            route TEXT,episode TEXT,signature TEXT,stable_since_ns INTEGER,last_scan_ns INTEGER,
            stable_count INTEGER,priority INTEGER DEFAULT 0,PRIMARY KEY(route,episode))''')
        self.db.commit()
    def observe(self,route,episode,signature,now,interval):
        self.db.row_factory=sqlite3.Row
        found=self.db.execute('SELECT * FROM observations WHERE route=? AND episode=?',(route,episode)).fetchone()
        old=dict(found) if found else None
        if not old or old['signature']!=signature:
            current={'route':route,'episode':episode,'signature':signature,'stable_since_ns':now,
                'last_scan_ns':now,'stable_count':1,'priority':old['priority'] if old else 0}
        else:
            current=old.copy()
            if now-old['last_scan_ns']>=interval:
                current.update(last_scan_ns=now,stable_count=old['stable_count']+1)
        self.db.execute('INSERT OR REPLACE INTO observations VALUES(:route,:episode,:signature,:stable_since_ns,:last_scan_ns,:stable_count,:priority)',current)
        self.db.commit();return current
    def prioritize(self,route,episode,priority):
        self.db.execute('UPDATE observations SET priority=? WHERE route=? AND episode=?',(priority,route,episode));self.db.commit()
    def close(self):self.db.close()
    def get(self,route,episode):
        self.db.row_factory=sqlite3.Row
        r=self.db.execute('SELECT * FROM jobs WHERE route=? AND episode=?',(route,episode)).fetchone()
        return dict(r) if r else None
    def put(self,route,episode,identity,state,**extra):
        old=self.get(route,episode) or {'attempts':0,'next_ns':0,'error':None,'proof':None}
        values={**old,**extra,'route':route,'episode':episode,'identity':identity,'state':state,'updated_ns':time.time_ns()}
        if isinstance(values['proof'],dict):values['proof']=json.dumps(values['proof'])
        self.db.execute('INSERT OR REPLACE INTO jobs VALUES(:route,:episode,:identity,:state,:attempts,:next_ns,:updated_ns,:error,:proof)',values)
        self.db.commit()
        if old.get('state')!=state or old.get('error')!=values['error']:
            print(json.dumps({k:values[k] for k in ('route','episode','state','attempts','error')},ensure_ascii=False),flush=True)
        return values
    def rows(self):
        self.db.row_factory=sqlite3.Row
        return [dict(r) for r in self.db.execute('SELECT * FROM jobs ORDER BY updated_ns,route,episode')]
