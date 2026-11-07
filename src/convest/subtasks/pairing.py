"""Read-only counterpart discovery by recording identity, never episode number."""
from pathlib import Path

KEYS=('source_recording_id','source_start_ns','source_end_ns')


def identity(row):
    if any(row.get(k) is None or row.get(k)=='' for k in KEYS):return None
    return tuple(str(row[k]) for k in KEYS)


def attach_speedup(ws,directory):
    from .remote import discover
    rows=[e for e in ws.catalog()['episodes'] if e['remote_directory']==directory and e['variant']=='normal' and not e['excluded'] and not e.get('archived')]
    report={'normal_count':len(rows),'matched':0,'imported':0,'issues':[]}
    pending=[e for e in rows if e['pair_state']!='matched']
    if not pending:
        report['matched']=len(rows);return report
    parts=list(Path(directory).parts)
    if parts.count('normal')!=1:raise ValueError('无法确定对应speedup目录')
    parts[parts.index('normal')]='speedup'
    scope=str(Path(*parts).parent)
    packages=discover(scope)['packages']
    candidates={}
    for package in packages:
        for e in package['episodes']:
            key=identity(e)
            if key is not None:candidates.setdefault(key,set()).add((package['path'],e['episode_index']))
    selected={}
    for e in pending:
        matches=candidates.get(identity(e),set())
        if len(matches)!=1:
            report['issues'].append({'normal_id':e['id'],'episode_index':e['episode_index'],
                                    'reason':'缺少来源信息' if identity(e) is None else '存在多个同源speedup，需人工选择' if matches else '未找到同源speedup'})
            continue
        path,index=next(iter(matches));selected.setdefault(path,set()).add(index)
    for path,indices in selected.items():
        try:report['imported']+=ws._import_directory(path,sorted(indices))['imported']
        except Exception as exc:report['issues'].append({'path':path,'reason':str(exc)})
    fresh={e['id']:e for e in ws.catalog()['episodes']}
    report['matched']=sum(fresh[e['id']]['pair_state']=='matched' for e in rows)
    return report
