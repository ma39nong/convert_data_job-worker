"""Rsync staging with independent server-side SHA256 and atomic pair publication."""
import json
from pathlib import Path
import shlex
import subprocess

from convest.capture.transfer import TransferConflict


def ssh_args(target):
    args = ['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'ConnectTimeout=15', '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3']
    if 'port' in target:
        args += ['-p', str(target['port'])]
    if 'user' in target:
        args += ['-l', target['user']]
    return args


def remote(target, request):
    code = Path(__file__).with_name('ssh_remote.py').read_text()
    command = shlex.join([target['python'], '-c', code])
    result = subprocess.run([*ssh_args(target), target['host'], command],
                            input=json.dumps(request), check=True, capture_output=True,
                            text=True, timeout=1800)
    response = json.loads(result.stdout)
    if response.get('status') == 'conflict':
        raise TransferConflict(response['error'])
    if response.get('status') == 'error':
        raise OSError(response['error'])
    return response


def upload(target, payload, task, fingerprint, guard, progress):
    request = {'root': target['dataset_root'], 'robot': payload['robot'], 'group': payload['group'],
               'name': payload['name'], 'task': task, 'fingerprint': fingerprint,
               'files': payload['files'], 'operation': 'prepare'}
    guard()
    progress(phase='ssh_prepare')
    response = remote(target, request)
    if response['status'] != 'existing':
        # --protect-args avoids remote shell expansion of destination paths.
        # Completed and partial files are checked by rsync AND the final SHA read-back.
        progress(phase='rsync')
        subprocess.run(['rsync', '-rlt', '--partial', '--checksum', '--protect-args',
                        '--timeout=120', '--no-perms', '--no-owner', '--no-group',
                        '--rsync-path=' + target['rsync'], '-e', shlex.join(ssh_args(target)),
                        str(Path(payload['source'])) + '/',
                        target['host'] + ':' + response['staging'] + '/'], check=True)
    guard()
    progress(phase='remote_sha256')
    result = remote(target, {**request, 'operation': 'commit'})
    progress(phase='uploaded', receipt=result['receipt'], destination=result['destination'])
    return result
