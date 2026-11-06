"""Separate loopback review server; does not import or control workflow workers."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import threading
from urllib.parse import parse_qs, urlsplit
import uuid
import webbrowser

from .core import LABELS, Store, project
from .behavior import KINDS
from .actions import ACTION_LABELS,simplify
from .workspace import Workspace,COARSE_LABELS
from .vocabulary import PROMPT_PROFILE, english_document
from .learning import predict, train

STATIC = Path(__file__).parent/'static'


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, roots, output, *, doubao_key=None, doubao_model="doubao-seed-2-1-pro-260628"):
        self.store = Store(roots, output)
        self.workspace = Workspace(self.store)
        self.token = os.environ.pop('SUBTASKS_HTTP_TOKEN', None) or secrets.token_urlsafe(32)
        self.doubao_key = doubao_key or os.environ.get('ARK_API_KEY', '')
        self.doubao_model = doubao_model
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.jobs, self.jobs_lock = {}, threading.Lock()
        self.cached_episodes = {}
        self.export_lock = threading.Lock()
        self.process_job = None
        super().__init__(address, Handler)

    def doubao_status(self):
        return {'enabled': bool(self.doubao_key), 'configured': bool(self.doubao_key),
                'model': self.doubao_model}

    def progress(self, done, total):
        with self.jobs_lock:
            for job in self.jobs.values():
                if job['state'] == 'running':
                    job['progress'] = {'completed_chunks': done, 'total_chunks': total}

    def episode(self, id_):
        # Reload after edits so source changes cannot be hidden behind an in-memory table.
        return self.store.episode(id_)

    def submit(self, action, *, lock_store=True):
        with self.jobs_lock:
            if any(j['state'] in ('running', 'queued') for j in self.jobs.values()):
                raise ValueError('已有任务正在处理，请等待完成')
            id_ = uuid.uuid4().hex
            self.jobs[id_] = {'state': 'queued'}
        def run():
            with self.jobs_lock: self.jobs[id_] = {'state': 'running'}
            try:
                if lock_store:
                    with self.store.lock: result = action()
                else:
                    result = action()
                with self.jobs_lock: self.jobs[id_] = {'state': 'completed', 'result': result}
            except Exception as exc:
                with self.jobs_lock: self.jobs[id_] = {'state': 'failed', 'error': str(exc)}
        self.pool.submit(run)
        return {'job': id_}

    def server_close(self):
        self.pool.shutdown(wait=True)
        super().server_close()
        self.store.lockfile.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def host_ok(self):
        return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

    def send(self, value, status=200, mime='application/json; charset=utf-8', attachment=None):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        for key, val in [('Content-Type', mime), ('Content-Length', str(len(data))), ('Cache-Control', 'no-store'),
                         ('X-Content-Type-Options', 'nosniff'),
                         ('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; media-src 'self'; img-src 'self' data:; frame-ancestors 'none'")]:
            self.send_header(key, val)
        if attachment is not None: self.send_header('Content-Disposition', f'attachment; filename="{attachment}"')
        self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        if not self.host_ok(): return self.send({'error': 'Invalid host'}, 403)
        route = urlsplit(self.path); q = parse_qs(route.query)
        try:
            if route.path == '/api/bootstrap':
                return self.send({'app': 'subtasks-review', 'token': self.server.token,
                                  'labels': {k: {'coarse': v[0], 'text': v[1]} for k, v in LABELS.items()},
                                  'action_labels':list(ACTION_LABELS),'behavior_kinds':KINDS,'episodes': list(self.server.store.entries.values()), 'output': str(self.server.store.output),
                                  'active_process_job':({'id':self.server.process_job,'kind':'process'} if self.server.process_job and self.server.jobs.get(self.server.process_job,{}).get('state') in ('queued','running') else None),'quarantine_available':True,'export_profile':PROMPT_PROFILE,'coarse_labels':COARSE_LABELS,'workspace':self.server.workspace.catalog(),'doubao': self.server.doubao_status()})
            if route.path == '/api/quality-review':
                path=self.server.store.output/'quality-review'/'report.json'
                return self.send(json.loads(path.read_text()) if path.exists() else {'state':'not_started','episodes':[]})
            if route.path == '/api/workspace/quarantine':
                from .quarantine import preview
                return self.send(preview(self.server.workspace,q['id'][0]))
            if route.path == '/api/workspace/organize-plan':
                from .quarantine import plan
                return self.send(plan(self.server.workspace,q['id'][0]))
            if route.path == '/api/workspace':
                return self.send(self.server.workspace.catalog())
            if route.path == '/api/workspace/episode':
                from .organization import registry
                selected=q['id'][0];selected=registry(self.server.workspace)['retired'].get(selected,selected)
                ep=self.server.episode(selected);doc=self.server.store.load(ep)
                from .delivery import locations
                stages=self.server.workspace.stages(ep,doc);catalog=self.server.workspace.catalog()
                entry=next(e for e in catalog['episodes'] if e['id']==ep.id)
                paired=[e for e in catalog['episodes'] if e['id'] in entry['pair_ids']]
                mapping={}
                mapping_stages=stages
                if entry['variant']=='normal' and len(paired)==1:
                    mapping_stages=self.server.workspace.stages(self.server.episode(paired[0]['id']))
                for stage in ('coarse','fine'):
                    projection=mapping_stages[stage].get('projection')
                    if projection:
                        normal=self.server.episode(projection['normal_id']);current=self.server.workspace.stages(normal)[stage]
                        mapping[stage]={'normal_id':normal.id,'source_revision':projection['normal_stage_revision'],'current_revision':current['revision'],'stale':projection['normal_stage_revision']!=current['revision'],'policy':projection['policy']}
                return self.send({'episode':ep.detail(),'annotation':doc,'stages':stages,'locations':locations(self.server.workspace,ep),'pair':{'state':entry['pair_state'],'episodes':paired,'mapping':mapping}})
            if route.path == '/api/workspace/download':
                ep=self.server.episode(q['id'][0]);stage=q['stage'][0]
                if stage not in {'coarse','fine'}:raise ValueError('未知阶段')
                doc=self.server.workspace.stages(ep)[stage]
                if not doc['saved_at']:raise ValueError('请先保存这个阶段')
                return self.send(english_document(doc),attachment=f'episode_{ep.index:06d}_{ep.id}_{stage}.json')
            if route.path == '/api/remote/packages':
                from .remote import discover,DATA_ROOT
                return self.send(discover(q.get('path',[DATA_ROOT])[0]))
            if route.path == '/api/remote/browse':
                from .remote import browse,DATA_ROOT
                return self.send(browse(q.get('path',[DATA_ROOT])[0]))
            if route.path == '/api/remote/inventory':
                from .remote import web_inventory
                data=web_inventory(q['path'][0])
                return self.send({'dataset':data['dataset'],'episodes':[{'episode_index':e['episode_index'],'length':e['length']} for e in data['episodes']],'fps':data['info']['fps']})
            if route.path == '/quality':
                self.send_response(302);self.send_header('Location','/');self.send_header('Content-Length','0');self.end_headers();return
            if route.path == '/api/episode':
                ep = self.server.episode(q['id'][0])
                return self.send({'episode': ep.detail(), 'annotation': self.server.store.load(ep)})
            if route.path == '/api/job':
                with self.server.jobs_lock: job = dict(self.server.jobs[q['id'][0]])
                return self.send(job)
            if route.path == '/api/video':
                ep = self.server.episode(q['id'][0]); path = ep.cameras[q['camera'][0]]
                return self.video(path)
            if route.path == '/api/download':
                ep = self.server.episode(q['id'][0]); doc = self.server.store.load(ep)
                return self.send(english_document({**doc,'stage':'fine'}), attachment=f'episode_{ep.index:06d}_{ep.id}_subtasks.json')
            files = {'/': ('index.html','text/html; charset=utf-8'),
                     '/app.js': ('app.js','text/javascript; charset=utf-8'), '/style.css': ('style.css','text/css; charset=utf-8')}
            if route.path in files:
                name, mime = files[route.path]
                return self.send((STATIC/name).read_bytes(), mime=mime)
            self.send({'error': 'Not found'}, 404)
        except (BrokenPipeError, ConnectionResetError): pass
        except Exception as exc: self.send({'error': str(exc)}, 400)

    def video(self, path):
        # Browser seeks require byte ranges. Only cameras in the registered dataset are served.
        with path.open('rb') as file:
            size = path.stat().st_size
            header = self.headers.get('Range')
            start, end, status = 0, size-1, 200
            if header:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
                if not match or not any(match.groups()):
                    return self.send({'error': 'Invalid range'}, 416)
                a, b = match.groups()
                if a: start, end = int(a), min(int(b), size-1) if b else size-1
                else: start, end = max(0, size-int(b)), size-1
                if not 0 <= start <= end < size:
                    self.send_response(416); self.send_header('Content-Range', f'bytes */{size}')
                    self.send_header('Content-Length', '0'); self.end_headers(); return
                status = 206
            self.send_response(status); self.send_header('Content-Type', 'video/mp4')
            self.send_header('Accept-Ranges', 'bytes'); self.send_header('Content-Length', str(end-start+1))
            self.send_header('X-Content-Type-Options', 'nosniff')
            if status == 206: self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers();file.seek(start)
            remaining = end-start+1
            while remaining:
                block = file.read(min(256*1024, remaining))
                if not block: break
                self.wfile.write(block); remaining -= len(block)

    def do_POST(self):
        if not self.host_ok() or self.headers.get('X-Subtasks-Token') != self.server.token:
            return self.send({'error': '请求校验失败，请刷新页面'}, 403)
        origin = self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}', f'http://localhost:{self.server.server_port}'):
            return self.send({'error': 'Cross-origin request refused'}, 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2_000_000: raise ValueError('请求大小无效')
            body = json.loads(self.rfile.read(length)); route = urlsplit(self.path).path
            store = self.server.store
            if route in {'/api/models/connect','/api/jev-plan'}:
                return self.send({'error':'该入口已移除；当前仅使用豆包'},404)
            if route == '/api/workspace/quarantine':
                from .quarantine import change
                return self.send(change(self.server.workspace,body['id'],body.get('excluded'),body.get('reason',''),body.get('revision')))
            if route == '/api/workspace/organize':
                from .organization import execute
                if not self.server.export_lock.acquire(blocking=False):raise ValueError('导出或整理正在执行，请稍后')
                try:return self.send({'result':execute(self.server.workspace,body['id'],body.get('plan_hash'))})
                finally:self.server.export_lock.release()
            if route == '/api/workspace/export-direct':
                from .delivery import publish,DEFAULT_TASK_PROMPT
                if not self.server.export_lock.acquire(blocking=False):raise ValueError('已有导出正在执行，请稍后再试')
                try:
                    ep=self.server.episode(body['id'])
                    result=publish(self.server.workspace,ep,body.get('stage'),target=body.get('target','dataset'),root=body.get('root'),kind=body.get('kind','json'),revision=body.get('revision'),prompt_mode=body.get('prompt_mode','goal'),task_prompt=body.get('task_prompt',DEFAULT_TASK_PROMPT))
                    return self.send({'result':result})
                finally:self.server.export_lock.release()
            if route == '/api/workspace/export':
                if self.server.workspace.organizing_ids:raise ValueError('整理期间请勿使用旧版导出，请刷新后再试')
                from .delivery import publish,DEFAULT_TASK_PROMPT
                ep=self.server.episode(body['id'])
                return self.send(self.server.submit(lambda:publish(self.server.workspace,ep,body.get('stage'),target=body.get('target','dataset'),root=body.get('root'),kind=body.get('kind','json'),revision=body.get('revision'),prompt_mode=body.get('prompt_mode','goal'),task_prompt=body.get('task_prompt',DEFAULT_TASK_PROMPT)),lock_store=False))
            if route == '/api/workspace/map':
                return self.send({'result':self.server.workspace.map_saved(body['normal_id'],body['speedup_id'],body.get('stages'),replace=body.get('replace') is True)})
            if route == '/api/workspace/import-many':
                items=body.get('items')
                return self.send(self.server.submit(lambda:self.server.workspace.import_many(items),lock_store=False))
            if route == '/api/workspace/import':
                path=body.get('path');selection=body.get('episodes')
                from .remote import dataset_path
                dataset_path(path)
                return self.send(self.server.submit(lambda:self.server.workspace.import_directory(path,selection),lock_store=False))
            if route == '/api/workspace/process':
                ids=body.get('ids')
                if not isinstance(ids,list) or not ids or len(ids)!=len(set(ids)) or any(i not in store.entries for i in ids):raise ValueError('请选择有效片段')
                if not self.server.doubao_key:raise ValueError('豆包连接不可用，请检查启动凭据')
                job=self.server.submit(lambda:self.server.workspace.process(ids,self.server),lock_store=False)
                self.server.process_job=job['job']
                return self.send(job)
            if route == '/api/workspace/save':
                ep=self.server.episode(body['id']);stage=body.get('stage')
                if stage not in {'coarse','fine'}:raise ValueError('未知标注阶段')
                return self.send({'saved':self.server.workspace.save_stage(ep,stage,body['document'])})
            if route == '/api/train':
                return self.send(self.server.submit(lambda: {'models': train(store)}))
            ep = self.server.episode(body['id'])
            if route == '/api/apply-proposal':
                from .template import validate_template, POLICY
                current = store.load(ep)
                proposal = body['proposal']
                if proposal.get('source') != ep.identity(): raise ValueError('预测结果来源指纹不匹配')
                if proposal.get('method') != 'doubao_template_source_frames_v1' or proposal.get('policy') != POLICY:
                    raise ValueError('仅接收已完成七阶段与原帧复查的豆包结果')
                if current['revision'] != body['revision']: raise ValueError('标注版本已变化，未覆盖新修改')
                if any(s['label'] != 'unknown' for s in current['segments']) and not body.get('replace'):
                    raise ValueError('已有标签，须明确选择替换；历史版本仍保留')
                validate_template(proposal['segments'])
                return self.send({'annotation': store.save(ep, {**current,'segments':[{**s,'reviewed':False} for s in proposal['segments']]},
                    origin=proposal['method'],provenance={k:v for k,v in proposal.items() if k not in ('source','segments')})})
            if route == '/api/apply-quality-review':
                from .template import POLICY,validate_template
                from .behavior import merge_candidates
                current=store.load(ep);proposal=body['proposal'];behavior=body['behavior_review']
                if current['revision']!=body['revision']:raise ValueError('人工或其他窗口已有新修改，全量结果未覆盖')
                if any(s['reviewed'] for s in current['segments']):raise ValueError('已人工确认的动作仅给复查报告，不自动改写')
                if proposal.get('source')!=ep.identity() or behavior.get('source')!=ep.identity():raise ValueError('全量复查来源不匹配')
                if proposal.get('policy')!=POLICY or not proposal.get('boundary_audit',{}).get('all_boundaries_checked'):raise ValueError('未完成当前策略的所有边界复查')
                validate_template(proposal['segments'])
                evidence=proposal.get('boundary_evidence',{})
                if set(evidence)!=set(map(str,range(len(proposal['segments'])-1))):raise ValueError('边界复查记录不完整')
                projection=proposal.get('projection')
                if projection:
                    normal_source=projection['normal_source']
                    matches=[e for e in store.entries.values() if e['dataset']==normal_source['dataset'] and e['episode_index']==normal_source['episode_index']]
                    if len(matches)!=1:raise ValueError('映射来源normal不唯一或已移除')
                    normal=store.episode(matches[0]['id']);normal_doc=store.load(normal)
                    if normal_doc['source']!=normal_source or normal_doc['revision']!=projection['normal_revision']:
                        raise ValueError('normal在speedup视觉复查期间已更新，未保存旧映射')
                provenance={k:v for k,v in proposal.items() if k not in {'source','segments'}}
                provenance.update(quality_review_run=body['run_id'],behavior_review={k:v for k,v in behavior.items() if k not in {'source','events'}})
                candidates=behavior['events']
                if projection:candidates=merge_candidates(candidates,projection.get('review_events',[]),ep.length)
                doc={**current,'segments':[{**s,'reviewed':False} for s in proposal['segments']],
                     'review_events':merge_candidates(current.get('review_events',[]),candidates,ep.length)}
                return self.send({'annotation':store.save(ep,doc,origin=proposal['method'],provenance=provenance)})
            if route == '/api/save':
                return self.send({'annotation': store.save(ep, body['annotation'])})
            if route == '/api/predict':
                current = store.load(ep)
                if current['revision'] != body['revision']: raise ValueError('标注版本已变化，请重新载入')
                if any(s['label'] != 'unknown' for s in current['segments']) and not body.get('replace'):
                    raise ValueError('已有语义标注，须明确选择替换；历史版本仍保留')
                def propose():
                    backend = body.get('backend', 'local')
                    if backend == 'doubao':
                        if not self.server.doubao_key: raise ValueError('尚未配置豆包 API key')
                        from .doubao import annotate, reference_style, reference_examples, Client
                        with store.lock:
                            refs = reference_style(store)
                            examples = reference_examples(store,ep)
                            from .style import transition_examples
                            style_examples=transition_examples({id_:store.load(store.episode(id_)) for id_ in store.entries},ep)
                        result = annotate(ep, store.output, api_key=self.server.doubao_key,
                                          model=self.server.doubao_model, references=refs,
                                          initial_proposal=current if current.get('origin','').startswith('doubao_') and current['status']!='reviewed' else None,
                                          client=Client(store.output/'doubao', self.server.doubao_key, self.server.doubao_model,
                                                        examples=examples,style_examples=style_examples),
                                          max_requests=body.get('max_requests',128), progress=self.server.progress)
                    elif backend == 'local':
                        result = predict(store, ep)
                        if result.get('in_reference_set'): raise ValueError('当前是模型参考样例，请保留参考标签，在其他 episode 上运行预标注')
                    else: raise ValueError('未知预标注方式')
                    result['segments'],simplified=simplify(result['segments'],ep.length,ep.fps,result.get('harvest_evidence',[]))
                    if simplified:result['simplified_helper_stages']=simplified
                    doc = {**current, 'segments': result['segments']}
                    annotation = store.save(ep, doc, origin=result['method'],
                                            provenance={k: v for k, v in result.items() if k != 'segments'})
                    return {'annotation': annotation, 'proposal': {k: v for k, v in result.items() if k != 'segments'}}
                return self.send(self.server.submit(propose, lock_store=body.get('backend','local') == 'local'))
            if route == '/api/scan-behavior':
                if not self.server.doubao_key:raise ValueError('尚未配置豆包 API key')
                current=store.load(ep)
                if current['revision']!=body['revision']:raise ValueError('标注版本已变化，未覆盖修改')
                def scan_behavior():
                    from .behavior import scan,merge_candidates
                    result=scan(ep,store.output,api_key=self.server.doubao_key,model=self.server.doubao_model)
                    events=merge_candidates(current.get('review_events',[]),result['events'],ep.length)
                    annotation=store.save(ep,{**current,'review_events':events},origin=current['origin'],
                        provenance={**current.get('provenance',{}),'behavior_review':{k:v for k,v in result.items() if k not in {'source','events'}}})
                    return {'annotation':annotation,'behavior_review':result}
                return self.send(self.server.submit(scan_behavior,lock_store=False))
            if route == '/api/project':
                target = self.server.episode(body['target_id'])
                current = store.load(ep); existing = store.load(target)
                if current['revision'] != body['revision']: raise ValueError('normal 标注版本已变化')
                if any(s['label'] != 'unknown' for s in existing['segments']) and not body.get('replace'):
                    raise ValueError('speedup 已有标注，须明确选择替换')
                def transfer():
                    if store.load(ep)['revision'] != current['revision']:
                        raise ValueError('normal 标注在开始映射前已变化，请重新发起')
                    result = project(ep, target, current)
                    doc = {**existing, 'segments': result['segments'], 'outcome': result['outcome']}
                    if 'review_events' in result:
                        from .behavior import merge_candidates
                        doc['review_events']=merge_candidates(existing.get('review_events',[]),result['review_events'],target.length)
                    saved = store.save(target, doc, origin='normal_projection',
                                       provenance={k: v for k, v in result.items() if k not in {'segments', 'source'}})
                    return {'annotation': saved, 'target_id': target.id, 'projection': saved['provenance']}
                return self.send(self.server.submit(transfer))
            return self.send({'error': 'Not found'}, 404)
        except (BrokenPipeError, ConnectionResetError): pass
        except Exception as exc: self.send({'error': str(exc)}, 400)


def main(argv=None):
    parser = argparse.ArgumentParser(description='LeRobot 粗细阶段预标注与人工审核')
    parser.add_argument('--root', action='append', required=True, help='本地数据集或缓存父目录，可重复')
    parser.add_argument('--output', default='outputs/subtasks/review')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--doubao-key-source', help='从指定现有 Python 文件读取 DEFAULT_ARK_API_KEY 字面量，不执行文件、不复制到配置')
    parser.add_argument('--doubao-model', default='doubao-seed-2-1-pro-260628')
    args = parser.parse_args(argv)
    doubao_key = None
    if args.doubao_key_source:
        from .doubao import credential_from_source
        doubao_key = credential_from_source(args.doubao_key_source)
    server = Server(('127.0.0.1', args.port), args.root, args.output, doubao_key=doubao_key, doubao_model=args.doubao_model)
    url = f'http://127.0.0.1:{server.server_port}'
    print('Subtasks review: '+url, flush=True)
    if not args.no_browser: threading.Timer(.5, lambda: webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == '__main__': main()
