#!/usr/bin/env python3
"""Read-only acceptance of completed capture uploads and paired receipts.

Transfer already hashes every remote file before commit. --rehash independently
repeats all remote hashes (can read many GB over the mount).
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time
from convest.capture.config import load
from convest.capture.transfer import check_mount, verify_tree
from convest.config import atomic_json
from convest.ingest.inputs import require_same


def validate(cfg, rehash=False):
    check_mount(cfg)
    results=[]
    state=Path(cfg['state_root'])
    with sqlite3.connect((state/'queue.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        rows=[dict(row) for row in db.execute('SELECT * FROM uploads ORDER BY route,episode')]
        jobs=[dict(row) for row in db.execute('SELECT route,episode,state,error FROM jobs ORDER BY route,episode')]
    if not rows or any(job['state']!='converted' for job in jobs):
        raise ValueError('Trial local jobs are not all converted')
    if {(row['route'],row['episode']) for row in rows} != {(job['route'],job['episode']) for job in jobs}:
        raise ValueError('Local jobs and upload tasks differ')
    for row in rows:
        if row['state']!='completed' or not row['raw_done'] or not row['dataset_done']:
            raise ValueError('Upload not yet completed: '+row['route']+'/'+row['episode'])
        data=json.loads(row['payload'])
        require_same(data['source'],data['source_snapshot'])
        receipt_path=Path(cfg['remote_dataset_root'])/'.capture-transfer'/cfg['fingerprint']/(row['id']+'.json')
        receipt=json.loads(receipt_path.read_text())
        expected_raw=Path(cfg['remote_raw_root'])/data['robot']/data['group']/data['episode']
        expected_data=Path(cfg['remote_dataset_root'])/data['robot']/data['group']/(data['episode']+'_data_lerobot')
        for key,expected in [('raw',str(expected_raw)),('dataset',str(expected_data)),('status','completed'),('identity',data['identity']),('route',row['route']),
                             ('episode',row['episode']),('raw_files',data['raw_files']),('dataset_files',data['dataset_files'])]:
            if receipt.get(key)!=expected:
                raise ValueError('Receipt differs: '+key)
        for rootkey,fileskey in [('raw','raw_files'),('dataset','dataset_files')]:
            root=Path(receipt[rootkey])
            if rehash:
                verify_tree(root,data[fileskey])
            else:
                for item in data[fileskey]:
                    if (root/item['path']).stat().st_size!=item['size']:
                        raise ValueError('Remote file size differs: '+str(root/item['path']))
        dataset=Path(receipt['dataset'])
        info=json.loads((dataset/'meta/info.json').read_text())
        verification=json.loads((dataset/'conversion/verification.json').read_text())
        local=json.loads((Path(data['dataset']).parent/'summary.json').read_text())['jobs'][0]
        if verification['status']!='passed' or local['state']!='completed' or not local['original_sha256_verified']:
            raise ValueError('Local/remote quality records are incomplete')
        results.append({'route':row['route'],'episode':row['episode'],'frames':info['total_frames'],
                        'episodes':info['total_episodes'],'raw_bytes':sum(f['size'] for f in data['raw_files']),
                        'dataset_bytes':sum(f['size'] for f in data['dataset_files']),
                        'local_quality':'passed','original_unchanged':True,'upload':'completed',
                        'receipt':str(receipt_path),'raw':receipt['raw'],'dataset':receipt['dataset']})
    return {'status':'passed','checked_ns':time.time_ns(),'uploads':results,'count':len(results),
            'full_remote_hashes_verified_before_commit':True,'additional_remote_rehash':rehash}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True,type=Path)
    p.add_argument('--report',required=True,type=Path)
    p.add_argument('--rehash',action='store_true')
    args=p.parse_args()
    result=validate(load(args.config),args.rehash)
    atomic_json(args.report,result)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}',file=sys.stderr)
        raise SystemExit(2)
