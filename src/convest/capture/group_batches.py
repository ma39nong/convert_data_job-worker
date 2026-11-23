"""Seal A+B together at a persistent source-count boundary, without mixing datasets."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import time
from convest.ingest.batches import BatchPublisher, durable_json


@contextmanager
def locked(cfg):
    limit=cfg['batch_group_size']
    if type(limit) is not int or limit<1 or any(r.get('batch_group_size')!=limit for r in cfg['routes']):
        raise ValueError('Invalid combined batch size/routes')
    state=Path(cfg['state_root']);state.mkdir(parents=True,exist_ok=True)
    with (state/'group_batches.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        path=state/'group_batches.json'
        owner={'schema_version':1,'fingerprint':cfg['fingerprint'],'limit':cfg['batch_group_size']}
        data=json.loads(path.read_text()) if path.exists() else {**owner,'seals':[]}
        if not path.exists():
            # Older partial rounds had no shared journal until their first seal.
            old_limits=[]
            for route in cfg['routes']:
                catalogue=Path(route['output'])/'.ingest/batches.json'
                if catalogue.exists():
                    old_limits.extend(b.get('group_size',0) for b in json.loads(catalogue.read_text())['batches']
                                      if b['state']=='active')
            previous=max(old_limits,default=0)
            if previous>limit:data['previous_limit']=previous
        if any(data.get(k)!=v for k,v in owner.items() if k!='limit'):raise ValueError('Count batch journal ownership mismatch')
        if data['limit']!=limit:
            # Retain each historical boundary when changing the prospective limit.
            for decision in data['seals']:decision.setdefault('limit',data['limit'])
            data['previous_limit']=data['limit']
            data['limit']=limit
            durable_json(path,data)
        if not path.exists():durable_json(path,data)
        yield path,data


def settle(cfg,path,data,manual=False):
    routes={r['id']:r for r in cfg['routes']}
    def finish(decision):
        members=decision['members']
        limit=decision.get('limit',data['limit'])
        valid_total=(1<=decision['total']<=limit if decision.get('reason') in ('manual','limit-change') else decision['total']==limit)
        if (not valid_total or sum(len(m['sources']) for m in members)!=decision['total']
                or len({m['route'] for m in members})!=len(members)
                or any(routes[m['route']]['robot']!=decision['robot'] for m in members)):
            raise ValueError('Shared batch boundary/members mismatch; retained')
        pubs=[]
        for member in decision['members']:
            pub=BatchPublisher(routes[member['route']],cfg['fingerprint'])
            b=next(b for b in pub.status() if b['id']==member['batch_id'])
            if b.get('sources')!=member['sources'] or b.get('generation')!=member['generation']:
                raise ValueError('Count batch source/generation changed; retained')
            pub.request_group_seal(b['id'],decision['prepared_ns'],decision['id'],decision['total'])
            pubs.append((pub,b['id']))
        # No member is consumable until ALL participating route seals verify.
        for pub,bid in pubs:pub.finish_group_seal(bid,decision['id'])
        decision['state']='completed';durable_json(path,data)
    for decision in data['seals']:
        if decision['state'] not in ('sealing','completed'):raise ValueError('Invalid shared batch state')
        if decision['state']=='sealing':finish(decision)
    for robot in sorted({r['robot'] for r in cfg['routes']}):
        members=[]
        for route in cfg['routes']:
            if route['robot']!=robot:continue
            pub=BatchPublisher(route,cfg['fingerprint']);pub.published()
            for batch in pub.status():
                if batch['state']=='active' and batch.get('source_count'):
                    members.append({'route':route['id'],'batch_id':batch['id'],
                                    'sources':batch['sources'],'generation':batch['generation']})
        total=sum(len(m['sources']) for m in members)
        carryover=total>data['limit'] and total<=data.get('previous_limit',0)
        if total>data['limit'] and not carryover:
            raise ValueError(f"已有未封批次超过{data['limit']}条，已保留数据；需要先处理历史超额批次，不能静默重新分组")
        if not total or (total<data['limit'] and not manual):continue
        decision={'id':f"group-{len(data['seals'])+1:06d}",'robot':robot,'state':'sealing',
                  'prepared_ns':time.time_ns(),'members':members,'total':total,
                  'limit':max(total,data['limit']),
                  'reason':'limit-change' if carryover else 'manual' if manual else 'count'}
        data['seals'].append(decision);durable_json(path,data)
        finish(decision)
    if 'previous_limit' in data:
        del data['previous_limit']
        durable_json(path,data)


def maintain(cfg):
    if not cfg.get('batch_group_size'):return
    with locked(cfg) as (path,data):settle(cfg,path,data)


def publish(cfg,action):
    if not cfg.get('batch_group_size'):return action()
    with locked(cfg) as (path,data):
        settle(cfg,path,data)
        result=action()
        settle(cfg,path,data)
        return result


def flush(cfg):
    """Seal the currently committed sources under the same lock as publication."""
    if not cfg.get('batch_group_size'):raise ValueError('当前任务不支持按条数封包')
    with locked(cfg) as (path,data):
        before=sum(d['state']=='completed' for d in data['seals'])
        settle(cfg,path,data,manual=True)
        return sum(d['state']=='completed' for d in data['seals'])>before
