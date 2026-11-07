"""Reversible paired exclusions; source datasets are never moved during review."""
import json
import time
from pathlib import Path, PurePosixPath
from .core import atomic_json


def read(ws):
    path=ws.root/'quarantine.json'
    return json.loads(path.read_text()) if path.exists() else {'revision':0,'entries':{},'history':[]}


def paired(ws,id_):
    from .organization import registry
    retired=registry(ws)['retired']
    if id_ in retired:raise ValueError('片段已整理并重新编号，请重新载入')
    entry=ws.store.entries.get(id_)
    if entry is None:raise ValueError('未知片段')
    keys=('source_recording_id','source_start_ns','source_end_ns')
    matches=[e for e in ws.store.entries.values() if e['id'] not in retired and e['variant']!=entry['variant'] and
             all(entry.get(k) is not None and str(entry[k])==str(e.get(k)) for k in keys) and e.get('robot')==entry.get('robot')]
    if len(matches)!=1:raise ValueError('同源normal/speedup缺失或不唯一，不能自动剔除；请先核对配对')
    other=matches[0]
    reverse=[e for e in ws.store.entries.values() if e['id'] not in retired and e['variant']==entry['variant'] and
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
        from .organization import registry
        if any(registry(ws)['entries'].get(e['id'],{}).get('archived') for e in entries):raise ValueError('已实体归档到C；恢复需要整包恢复，不能仅取消剔除标记')
        if any(e['id'] in getattr(ws,'organizing_ids',set()) for e in entries):raise ValueError('这一对正在整理，请稍后')
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
    from .organization import registry
    if id_ in getattr(ws,'organizing_ids',set()):raise ValueError('本包正在整理，请稍后')
    if id_ in registry(ws)['retired']:raise ValueError('片段已重新编号，请重新载入')
    if read(ws)['entries'].get(id_,{}).get('excluded'):raise ValueError('这条及同源版本已剔除到C，不参与自动处理、映射或训练导出')


def plan(ws,id_):
    """Plan the complete SERVER packages, even when episodes occupy separate caches."""
    from .organization import remote_path,registry
    import hashlib
    with ws.lock,ws.store.lock:
        seeds=paired(ws,id_);ledger=read(ws);reg=registry(ws);retired=reg['retired'];packages=[];blockers=[];warnings=[];missing_packages=[]
        if any(reg['entries'].get(e['id'],{}).get('archived') for e in seeds):raise ValueError('C隔离包不作为整理入口，请选择A/B保留数据包')
        groups=[];archive_groups=[];base_groups=[]
        for seed in seeds:
            server_path=remote_path(seed)
            rows=sorted((e for e in ws.store.entries.values() if e['id'] not in retired and remote_path(e)==server_path),key=lambda e:e['episode_index'])
            expected=[json.loads(line)['episode_index'] for line in (Path(seed['dataset'])/'meta/episodes.jsonl').read_text().splitlines() if line.strip()]
            missing=sorted(set(expected)-{e['episode_index'] for e in rows})
            if missing:missing_packages.append({'path':server_path,'episodes':missing})
            if len(rows)!=len(expected) or {e['episode_index'] for e in rows}!=set(expected):blockers.append(f"{seed['variant']}: 本包未完整导入或存在重复缓存，不能整理")
            base_groups.append(rows)
            archived=[e for e in ws.store.entries.values() if e['id'] not in retired and reg['entries'].get(e['id'],{}).get('archived') and reg['entries'][e['id']].get('directory_override')==server_path]
            archived.sort(key=lambda e:e['episode_index']);archive_groups.append(archived);groups.append(rows+archived)
        normal=base_groups[0];order={};counts={False:0,True:0}
        normalized_order=[e for e in normal if not ledger['entries'].get(e['id'],{}).get('excluded')]+archive_groups[0]+[e for e in normal if ledger['entries'].get(e['id'],{}).get('excluded')]
        for e in normalized_order:
            excluded=bool(ledger['entries'].get(e['id'],{}).get('excluded'))
            key=tuple(str(e.get(k)) for k in ('source_recording_id','source_start_ns','source_end_ns'))
            order[key]=(excluded,counts[excluded]);counts[excluded]+=1
        for seed,rows in zip(seeds,groups):
            included=[];rejected=[];unpaired=[]
            for e in rows:
                key=tuple(str(e.get(k)) for k in ('source_recording_id','source_start_ns','source_end_ns'))
                excluded=bool(ledger['entries'].get(e['id'],{}).get('excluded'))
                standalone=False
                if key not in order:
                    # A speedup package can contain additional recordings absent
                    # from normal. Preserve them after paired recordings; never
                    # silently drop them or guess an exclusion.
                    try:paired(ws,e['id'])
                    except ValueError as exc:
                        if (seed['variant']=='speedup' and not excluded and all(e.get(k) not in (None,'') for k in ('source_recording_id','source_start_ns','source_end_ns'))
                                and '缺失或不唯一' in str(exc)):
                            opposite=[other for other in ws.store.entries.values() if other['id'] not in retired and other['variant']=='normal' and
                                      tuple(str(other.get(k)) for k in ('source_recording_id','source_start_ns','source_end_ns'))==key]
                            same=[other for other in rows if tuple(str(other.get(k)) for k in ('source_recording_id','source_start_ns','source_end_ns'))==key]
                            standalone=not opposite and len(same)==1
                    if not standalone:blockers.append('normal/speedup配对或剔除状态不一致');continue
                elif excluded!=order[key][0]:blockers.append('normal/speedup配对或剔除状态不一致');continue
                new=counts[False]+len(unpaired) if standalone else order[key][1]
                if standalone:unpaired.append(e['episode_index'])
                doc=ws.stages(ws.store.episode(e['id']));coarse=doc['coarse']
                reviewed=bool(coarse['saved_at'] and all(s['reviewed'] and s['label']!='unknown' for s in coarse['segments']))
                row={'id':e['id'],'source_episode_index':e['episode_index'],'new_episode_index':new,'reviewed':reviewed,'coarse_revision':coarse['revision'],'fine_revision':doc['fine']['revision'],'annotation_revision':ws.store.load(ws.store.episode(e['id']))['revision'],'unpaired':standalone}
                (rejected if excluded else included).append(row)
                if not excluded and not reviewed:warnings.append(f"{e['variant']} episode {e['episode_index']}: 标注尚未全部确认，整理后仍需审核，不代表可训练")
                if ws.queue.get(e['id'],{}).get('state')=='running':blockers.append(f"{e['variant']} episode {e['episode_index']}: 正在处理")
                if standalone:
                    warnings.append(f"speedup episode {e['episode_index']}: 无normal对应，默认保留并排在已配对录制之后，仍待人工审核")
                    continue
                try:
                    other=paired(ws,e['id'])
                    if any(o['id'] not in {entry['id'] for group in groups for entry in group} for o in other):raise ValueError('配对落在其他数据包')
                except ValueError as exc:blockers.append(str(exc))
            packages.append({'variant':seed['variant'],'source':seed['dataset'],'remote_source':remote_path(seed),'c_target':target(seed),'included':sorted(included,key=lambda r:r['new_episode_index']),'rejected':sorted(rejected,key=lambda r:r['new_episode_index']),'unpaired_preserved':unpaired})
        result={'quarantine_revision':ledger['revision'],'packages':packages,'missing_packages':missing_packages,'blockers':list(dict.fromkeys(blockers)),'review_warnings':warnings,'ready':not blockers,'operation':'compact_pair','source_files_changed':False}
        result['plan_hash']=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()
        return result
