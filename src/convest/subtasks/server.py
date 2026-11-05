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
from .learning import predict, train

STATIC = Path(__file__).parent/'static'


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, roots, output, *, enable_jev=False, jev_model="jev-1.14.0"):
        self.enable_jev, self.jev_model = enable_jev, jev_model
        self.store = Store(roots, output)
        self.token = os.environ.pop('SUBTASKS_HTTP_TOKEN', None) or secrets.token_urlsafe(32)
        self.jev_key = os.environ.get('TYPESAFE_API_KEY', '')
        self.jev_verified = False
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.jobs, self.jobs_lock = {}, threading.Lock()
        self.cached_episodes = {}
        super().__init__(address, Handler)

    def model_status(self):
        return {'enabled': self.enable_jev, 'configured': bool(self.jev_key),
                'verified': self.jev_verified, 'model': self.jev_model}

    def episode(self, id_):
        # Reload after edits so source changes cannot be hidden behind an in-memory table.
        return self.store.episode(id_)

    def submit(self, action):
        with self.jobs_lock:
            if any(j['state'] in ('running', 'queued') for j in self.jobs.values()):
                raise ValueError('已有任务正在处理，请等待完成')
            id_ = uuid.uuid4().hex
            self.jobs[id_] = {'state': 'queued'}
        def run():
            with self.jobs_lock: self.jobs[id_] = {'state': 'running'}
            try:
                with self.store.lock: result = action()
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
                                  'episodes': list(self.server.store.entries.values()), 'output': str(self.server.store.output),
                                  'jev': self.server.model_status()})
            if route.path == '/api/models/status':
                return self.send(self.server.model_status())
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
                return self.send(doc, attachment=f'episode_{ep.index:06d}_{ep.id}_subtasks.json')
            files = {'/': ('index.html', 'text/html; charset=utf-8'),
                     '/models': ('models.html', 'text/html; charset=utf-8'),
                     '/models.js': ('models.js', 'text/javascript; charset=utf-8'),
                     '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
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
            if route == '/api/models/connect':
                from .model_config import validate_key
                key = validate_key(body.get('key'))
                self.server.jev_key = key
                self.server.enable_jev = True
                self.server.jev_verified = True
                return self.send(self.server.model_status())
            if route == '/api/train':
                return self.send(self.server.submit(lambda: {'models': train(store)}))
            ep = self.server.episode(body['id'])
            if route == '/api/save':
                return self.send({'annotation': store.save(ep, body['annotation'])})
            if route == '/api/predict':
                current = store.load(ep)
                if current['revision'] != body['revision']: raise ValueError('标注版本已变化，请重新载入')
                if any(s['label'] != 'unknown' for s in current['segments']) and not body.get('replace'):
                    raise ValueError('已有语义标注，须明确选择替换；历史版本仍保留')
                def propose():
                    backend = body.get('backend', 'local')
                    if backend == 'jev':
                        if not self.server.enable_jev: raise ValueError('服务未启用 JEV；请用 --enable-jev 启动')
                        from .jev import annotate, Client
                        if not self.server.jev_key: raise ValueError('请先在视觉模型配置页填写 API key')
                        result = annotate(ep, store.output, model=self.server.jev_model, max_requests=body.get('max_requests',256), client=Client(store.output/'jev', api_key=self.server.jev_key))
                    elif backend == 'local':
                        result = predict(store, ep)
                        if result.get('in_reference_set'): raise ValueError('当前是模型参考样例，请保留参考标签，在其他 episode 上运行预标注')
                    else: raise ValueError('未知预标注方式')
                    doc = {**current, 'segments': result['segments']}
                    annotation = store.save(ep, doc, origin=result['method'],
                                            provenance={k: v for k, v in result.items() if k != 'segments'})
                    return {'annotation': annotation, 'proposal': {k: v for k, v in result.items() if k != 'segments'}}
                return self.send(self.server.submit(propose))
            if route == '/api/jev-plan':
                from .jev import plan
                return self.send({'plan':plan(ep, store.output, self.server.jev_model)})
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
    parser.add_argument('--enable-jev', action='store_true', help='显式允许向 TypeSafe 发送本次所选 episode 的抽帧；API key 可从环境或本机视觉模型配置页提供')
    parser.add_argument('--jev-model', default='jev-1.14.0')
    args = parser.parse_args(argv)
    server = Server(('127.0.0.1', args.port), args.root, args.output, enable_jev=args.enable_jev, jev_model=args.jev_model)
    url = f'http://127.0.0.1:{server.server_port}'
    print('Subtasks review: '+url, flush=True)
    if not args.no_browser: threading.Timer(.5, lambda: webbrowser.open(url)).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()


if __name__ == '__main__': main()
