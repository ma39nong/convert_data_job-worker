"""Reversible paired exclusions; source datasets are never moved during review."""
import json
import time
from pathlib import Path, PurePosixPath
from .core import atomic_json


def read(ws):
    path=ws.root/'quarantine.json'
    return json.loads(path.read_text()) if path.exists() else {'revision':0,'entries':{},'history':[]}


def paired(ws,id_):
    entry=ws.store.entries.get(id_)
    if entry is None:raise ValueError('未知片段')
    keys=('source_recording_id','source_start_ns','source_end_ns')
    matches=[e for e in ws.store.entries.values() if e['variant']!=entry['variant'] and
             all(entry.get(k) is not None and str(entry[k])==str(e.get(k)) for k in keys) and e.get('robot')==entry.get('robot')]
    if len(matches)!=1:raise ValueError('同源normal/speedup缺失或不唯一，不能自动剔除；请先核对配对')
    other=matches[0]
    reverse=[e for e in ws.store.entries.values() if e['variant']==entry['variant'] and
             all(str(other.get(k))==str(e.get(k)) for k in keys) and e.get('robot')==other.get('robot')]
    if len(reverse)!=1:raise ValueError('同源配对不唯一，不能自动剔除')
    return sorted((entry,other),key=lambda e:e['variant'])


def target(entry):
    receipt=Path(entry['dataset'])/'cache-source.json'
    remote=json.loads(receipt.read_text()) if receipt.exists() else {}
    path=PurePosixPath(remote.get('manifest',{}).get('dataset','/'))
    if remote.get('host')!='28' or not str(path).startswith('/mnt/data/train_platform/datasets/') or path.parent.name not in ('A','B'):
        raise ValueError('只支持28服务器A/B来源包；不能猜测C归档位置')
    return str(path.parent.parent/'C'/(path.name+'__from_'+path.parent.name+'__rejected_v001'))


def preview(ws,id_):
    with ws.lock,ws.store.lock:
        ledger=read(ws);entries=paired(ws,id_)
        return {'revision':ledger['revision'],'excluded':any(ledger['entries'].get(e['id'],{}).get('excluded') for e in entries),
                'items':[{'id':e['id'],'variant':e['variant'],'episode_index':e['episode_index'],'target':target(e),
                          'running':ws.queue.get(e['id'],{}).get('state')=='running'} for e in entries],
                'reason':ledger['entries'].get(id_,{}).get('reason','')}


def change(ws,id_,excluded,reason,revision):
    with ws.lock,ws.store.lock:
        if type(excluded) is not bool or type(revision) is not int:raise ValueError('剔除参数无效')
        ledger=read(ws)
        if revision!=ledger['revision']:raise ValueError('剔除清单已在其他窗口更新，请重新打开')
        entries=paired(ws,id_)
        if any(ws.queue.get(e['id'],{}).get('state')=='running' for e in entries):raise ValueError('这一对正在处理，请等当前片段结束后再操作')
        if not isinstance(reason,str) or len(reason)>1000 or (excluded and not reason.strip()):raise ValueError('请填写剔除原因，最长1000字')
        event={'revision':revision+1,'time':time.time(),'excluded':excluded,'reason':reason.strip(),'ids':[e['id'] for e in entries]}
        for e in entries:
            ledger['entries'][e['id']]={**event,'target':target(e),'source_recording_id':e.get('source_recording_id'),
                                      'source_start_ns':str(e.get('source_start_ns')),'source_end_ns':str(e.get('source_end_ns'))}
        ledger['revision']+=1;ledger['history'].append(event)
        # Single atomic file commits both variants and their audit history together.
        atomic_json(ws.root/'quarantine.json',ledger)
        return preview(ws,id_)


def assert_included(ws,id_):
    if read(ws)['entries'].get(id_,{}).get('excluded'):raise ValueError('这条及同源版本已剔除到C，不参与自动处理、映射或训练导出')


def plan(ws,id_):
    """Read-only package plan: explicit blockers, no source file writes or renames."""
    with ws.lock,ws.store.lock:
        pair=paired(ws,id_);ledger=read(ws);packages=[];blockers=[]
        for seed in pair:
            rows=sorted((e for e in ws.store.entries.values() if e['dataset']==seed['dataset']),key=lambda e:e['episode_index'])
            expected=[json.loads(line)['episode_index'] for line in (Path(seed['dataset'])/'meta/episodes.jsonl').read_text().splitlines() if line.strip()]
            if set(expected)!={e['episode_index'] for e in rows}:blockers.append(f"{seed['variant']}: 本包未完整导入，不能整理")
            included=[];rejected=[]
            for e in rows:
                if ledger['entries'].get(e['id'],{}).get('excluded'):
                    rejected.append({'id':e['id'],'source_episode_index':e['episode_index'],'new_episode_index':len(rejected)})
                else:
                    doc=ws.stages(ws.store.episode(e['id']))['coarse']
                    reviewed=bool(doc['saved_at'] and all(s['reviewed'] and s['label']!='unknown' for s in doc['segments']))
                    included.append({'id':e['id'],'source_episode_index':e['episode_index'],'new_episode_index':len(included),'reviewed':reviewed})
                    if not reviewed:blockers.append(f"{e['variant']} episode {e['episode_index']}: 粗阶段尚未保存并全部确认")
                if ws.queue.get(e['id'],{}).get('state')=='running':blockers.append(f"{e['variant']} episode {e['episode_index']}: 正在处理")
            packages.append({'variant':seed['variant'],'source':seed['dataset'],'c_target':target(seed),'included':included,'rejected':rejected})
        # Every recording must be paired inside these exact two packages.
        ids={e['id'] for seed in pair for e in ws.store.entries.values() if e['dataset']==seed['dataset']}
        for id0 in ids:
            try:
                if any(e['id'] not in ids for e in paired(ws,id0)):raise ValueError('配对落在其他数据包')
            except ValueError as exc:blockers.append(f'{id0}: {exc}')
        return {'quarantine_revision':ledger['revision'],'packages':packages,'blockers':list(dict.fromkeys(blockers)),
                'ready':not blockers,'operation':'preview_only','source_files_changed':False}
