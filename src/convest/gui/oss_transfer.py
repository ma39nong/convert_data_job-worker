"""Workstation -> mounted OSS -> verified CPFS publication -> owned transit cleanup."""
from pathlib import Path
from convest.config import digest
import json
import os
import selectors
import shlex
import subprocess
import time
from .transport import call, ssh_args, agent_code

DEFAULT_DELIVERY = {
    'mode': 'oss',
    'transit_root': '/mnt/public_data/local-realrobot-train-data',
    'final_root': '/mnt/data/train_platform/datasets',
}


def transfer(target, source, destination, files, identity, progress=lambda **kw: None,
             guard=lambda: None, *, delivery):
    token = digest({'destination': str(destination), 'identity': identity, 'files': files})
    request = {'destination': str(destination), 'token': token, 'files': files,
               'transit_root': delivery['transit_root'], 'final_root': delivery['final_root']}
    # Reject impossible mappings before any SSH write.
    Path(destination).relative_to(Path(delivery['final_root']))
    total = sum(r['size'] for r in files)
    guard(); progress(phase='oss_prepare')
    result = call(target, {**request, 'operation': 'prepare'}, timeout=1800, transit=True)
    if result['status'] != 'completed':
        stream_files(target, source, request, result['pending'], total, progress)
        guard(); progress(phase='oss_publish', bytes_done=total, bytes_total=total, eta_seconds=None)
        result = call(target, {**request, 'operation': 'commit'}, timeout=1800, transit=True)
    guard()
    progress(phase='completed', bytes_done=total, bytes_total=total, **result)
    return {**result, 'token': token}


def stream_files(target, source, request, pending, total, progress):
    """Bounded SSH stream; OSS needs sequential writes rather than rsync ftruncate."""
    expected = {r['path']: r for r in request['files']}
    done = total - sum(expected[name]['size'] for name in pending)
    started = time.monotonic(); resumed = done
    progress(phase='oss_upload', bytes_done=done, bytes_total=total)
    if not pending: return
    command = [*ssh_args(target), target['host'], shlex.join(['python3', '-c', agent_code(transit=True)])]
    # stderr goes to a temporary file to avoid deadlock on SSH diagnostics.
    import tempfile
    with tempfile.TemporaryFile() as errors, subprocess.Popen(command, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=errors, bufsize=0) as child:
        selector = selectors.DefaultSelector()
        selector.register(child.stdin, selectors.EVENT_WRITE)
        os.set_blocking(child.stdin.fileno(), False)
        def write(data):
            view = memoryview(data)
            while view:
                if not selector.select(timeout=120):
                    raise OSError('OSS 上传连续 120 秒无法写入，保留中转并重试')
                try: count = os.write(child.stdin.fileno(), view)
                except BlockingIOError: continue
                view = view[count:]
        try:
            write((json.dumps({**request, 'operation': 'receive', 'paths': pending}) + '\n').encode())
            for name in pending:
                with (Path(source) / name).open('rb') as data:
                    remaining = expected[name]['size']
                    while remaining:
                        chunk = data.read(min(8 * 1024 * 1024, remaining))
                        if not chunk: raise OSError('本机成品在上传时缩短')
                        write(chunk); remaining -= len(chunk); done += len(chunk)
                        rate = (done - resumed) / max(.1, time.monotonic() - started)
                        progress(phase='oss_upload', bytes_done=done, bytes_total=total,
                                 speed_bps=rate, eta_seconds=(total-done)/rate if rate else None)
            child.stdin.close()
            child.stdin = None
            output, _ = child.communicate(timeout=1800)
            code = child.returncode
            try: response = json.loads(output)
            except ValueError: response = {}
            if code or response.get('status') != 'staged':
                errors.seek(0)
                raise OSError(response.get('error') or errors.read().decode(errors='replace')[-2000:] or 'OSS 上传流失败')
        except BaseException:
            child.kill(); child.wait()
            raise
        finally:
            selector.close()
