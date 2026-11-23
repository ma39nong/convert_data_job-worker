"""Loopback-only web GUI. No web framework or Node installation required."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs, urlsplit
import webbrowser
from .config import ROOT, defaults, new_task
from .manager import Manager
from .transport import call, endpoint

STATIC=Path(__file__).parent/'static'


class Server(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,address):
        self.manager=Manager();self.token=secrets.token_urlsafe(32)
        super().__init__(address,Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def host_ok(self):
        return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}')
    def send(self,value,status=200,mime='application/json; charset=utf-8'):
        data=value if isinstance(value,bytes) else json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers();self.wfile.write(data)
    def do_GET(self):
        if not self.host_ok():return self.send({'error':'Invalid host'},403)
        route=urlsplit(self.path);query=parse_qs(route.query)
        try:
            if route.path=='/api/bootstrap':return self.send({'app':'convert-data-workflow','token':self.server.token,'snapshot':self.server.manager.snapshot()})
            if route.path=='/api/status':return self.send(self.server.manager.snapshot())
            if route.path=='/api/logs':return self.send({'text':self.server.manager.logs(query.get('task',[''])[0])})
            files={'/':'index.html','/app.js':'app.js','/style.css':'style.css','/brand-logo.png':'brand-logo.png'}
            if route.path in files:
                file=STATIC/files[route.path]
                mime={'html':'text/html','js':'text/javascript','css':'text/css','png':'image/png'}[file.suffix[1:]]
                if mime.startswith('text/'):mime+='; charset=utf-8'
                return self.send(file.read_bytes(),mime=mime)
            return self.send({'error':'Not found'},404)
        except Exception as e:self.send({'error':str(e)},400)
    def do_POST(self):
        if not self.host_ok() or self.headers.get('X-Workflow-Token')!=self.server.token:
            return self.send({'error':'请求校验失败，请刷新界面'},403)
        origin=self.headers.get('Origin')
        if origin and origin not in (f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'):
            return self.send({'error':'Cross-origin request refused'},403)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=512000:raise ValueError('请求大小无效')
            body=json.loads(self.rfile.read(length));route=urlsplit(self.path).path;m=self.server.manager
            if route=='/api/save':return self.send({'settings':m.save(body)})
            if route=='/api/start':return self.send(m.start(body.get('task')))
            if route=='/api/stop':return self.send(m.stop(body.get('task')))
            if route=='/api/retry':return self.send(m.retry(body['task']))
            if route=='/api/new-task':
                task=new_task(body.get('index',1))
                for group in ('A','B'):(Path(task['source'])/group).mkdir(parents=True,exist_ok=True)
                return self.send(task)
            if route=='/api/check':return self.send(call(endpoint(body['server']),{'operation':'check'},timeout=20))
            if route=='/api/browse':
                if body.get('server'):
                    return self.send(call(endpoint(body['server']),{'operation':'list','path':body.get('path','')},timeout=20))
                p=Path(body.get('path') or str(Path.home())).expanduser().resolve(strict=True)
                entries=[]
                for child in p.iterdir():
                    try:
                        if child.is_dir():entries.append({'name':child.name,'path':str(child),'symlink':child.is_symlink()})
                    except OSError:pass
                return self.send({'path':str(p),'parent':str(p.parent),'entries':sorted(entries,key=lambda e:e['name'])[:1000]})
            if route=='/api/mkdir':
                return self.send(call(endpoint(body['server']),{'operation':'mkdir','parent':body['parent'],
                                      'name':body['name'],'with_ab':body.get('with_ab',True)},timeout=20))
            return self.send({'error':'Not found'},404)
        except Exception as e:self.send({'error':str(e)},400)


def main(argv=None):
    p=argparse.ArgumentParser(description='ROSbag 自动清洗、转换和上传控制台')
    p.add_argument('--port',type=int,default=8765);p.add_argument('--no-browser',action='store_true')
    args=p.parse_args(argv)
    try:server=Server(('127.0.0.1',args.port))
    except OSError as exc:
        if exc.errno!=98:raise
        import urllib.request
        url=f'http://127.0.0.1:{args.port}'
        try:
            with urllib.request.urlopen(url+'/api/bootstrap',timeout=3) as response:
                if json.load(response).get('app')!='convert-data-workflow':raise ValueError('端口已被其他程序使用')
        except Exception:raise SystemExit(f'端口 {args.port} 已被占用，请用 --port 选择其他端口')
        if not args.no_browser:webbrowser.open(url)
        print('Workflow GUI already running: '+url,flush=True);return
    url=f'http://127.0.0.1:{server.server_port}'
    print('Workflow GUI: '+url,flush=True)
    if not args.no_browser:threading.Timer(.5,lambda:webbrowser.open(url)).start()
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:server.server_close()

if __name__=='__main__':main()
