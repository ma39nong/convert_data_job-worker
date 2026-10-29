"""SSH directory browsing and resumable, verified publication with live transfer metrics."""
import json
from pathlib import Path
import re
import shlex
import subprocess
import time
from convest.config import digest


def endpoint(value):
    if not isinstance(value, dict): raise ValueError('服务器设置无效')
    result = dict(value)
    for key in ('host', 'user'):
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', str(result.get(key, ''))):
            raise ValueError('服务器地址或用户名无效')
    if type(result.get('port')) is not int or not 1 <= result['port'] <= 65535:
        raise ValueError('SSH 端口无效')
    return {k: result[k] for k in ('host', 'port', 'user')}


def ssh_args(target):
    target = endpoint(target)
    return ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=20', '-o', 'ServerAliveCountMax=3',
            '-p', str(target['port']), '-l', target['user']]


def agent_code(*, transit=False):
    code = Path(__file__).with_name('remote_agent.py').read_text()
    if transit:
        # Reuse the final CPFS publication protocol in a standalone SSH program.
        code = code.split("if __name__ == '__main__':")[0] + '\n' + Path(__file__).with_name('oss_remote.py').read_text()
    return code


def call(target, request, timeout=120, *, transit=False, progress=None):
    code = agent_code(transit=transit)
    command = [*ssh_args(target), target['host'], shlex.join(['python3', '-c', code])]
    if progress is not None:
        result = progress_process(command, {**request, 'report_progress': True}, timeout, progress)
    else:
        result = subprocess.run(command, input=json.dumps(request)+'\n', capture_output=True, text=True, timeout=timeout)
    try: response = json.loads(result.stdout)
    except ValueError:
        raise OSError((result.stderr or 'SSH 未返回有效结果').strip()[-2000:]) from None
    if result.returncode or response.get('status') == 'error':
        raise OSError(response.get('error') or result.stderr[-2000:])
    return response


def transfer(target, source, destination, files, identity, progress=lambda **kw: None, guard=lambda: None):
    token = digest({'destination': str(destination), 'identity': identity, 'files': files})
    request = {'destination': str(destination), 'token': token, 'files': files}
    guard(); progress(phase='remote_prepare')
    response = call(target, {**request, 'operation': 'prepare'}, timeout=1800)
    total = sum(r['size'] for r in files)
    if response['status'] != 'existing':
        # Keep partials under their manifest names on interruption; never delete the source.
        cmd = ['rsync', '-rlt', '--partial', '--checksum', '--protect-args', '--info=progress2',
               '--outbuf=L', '--no-perms', '--no-owner', '--no-group', '--timeout=120',
               '-e', shlex.join(ssh_args(target)), str(Path(source)) + '/',
               target['host'] + ':' + response['staging'] + '/']
        progress(phase='transferring', bytes_done=0, bytes_total=total, speed_bps=None, eta_seconds=None)
        started = time.monotonic(); buffer = ''; recent = []
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1) as child:
            # rsync emits CR-delimited progress; universal-newline mode gives complete updates.
            for line in child.stdout:
                recent.append(line.strip()); recent = recent[-15:]
                match = re.match(r'\s*([\d,]+)\s+(\d+)%', line)
                if match:
                    done = min(int(match[1].replace(',', '')), total)
                    elapsed = max(.1, time.monotonic() - started)
                    rate = done / elapsed
                    progress(phase='transferring', bytes_done=done, bytes_total=total,
                             speed_bps=rate, eta_seconds=max(0, (total - done) / rate) if rate else None)
            code = child.wait()
        if code: raise OSError('rsync 传输中断，可重试续传：' + '\n'.join(recent)[-1800:])
    guard(); progress(phase='remote_sha256', bytes_done=total, bytes_total=total, eta_seconds=None)
    result = call(target, {**request, 'operation': 'commit'}, timeout=1800)
    progress(phase='completed', bytes_done=total, bytes_total=total, **result)
    return {**result, 'token': token}


def verify_backup(target, destination, files, identity):
    token = digest({'destination': str(destination), 'identity': identity, 'files': files})
    return call(target, {'operation': 'verify', 'destination': str(destination), 'token': token,
                         'files': files}, timeout=1800)


def progress_process(command, request, timeout, progress):
    """Read real SSH phase events while preserving the final JSON response."""
    import queue
    import tempfile
    import threading
    events = queue.Queue()
    with tempfile.TemporaryFile(mode='w+t') as errors, subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
            text=True, bufsize=1) as child:
        def read_lines():
            try:
                for line in child.stdout: events.put(line)
            finally: events.put(None)
        reader = threading.Thread(target=read_lines, daemon=True)
        reader.start(); deadline = time.monotonic() + timeout; response = ''
        try:
            child.stdin.write(json.dumps(request) + '\n'); child.stdin.close()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise subprocess.TimeoutExpired(command, timeout)
                try: line = events.get(timeout=remaining)
                except queue.Empty: raise subprocess.TimeoutExpired(command, timeout) from None
                if line is None: break
                item = json.loads(line)
                if item.get('event') == 'progress':
                    progress(**{k: v for k, v in item.items() if k != 'event'})
                else: response = line
            code = child.wait(timeout=max(.01, deadline-time.monotonic()))
            errors.seek(0)
            return subprocess.CompletedProcess(command, code, response, errors.read())
        except BaseException:
            child.kill(); child.wait()
            raise
        finally: reader.join(timeout=2)
