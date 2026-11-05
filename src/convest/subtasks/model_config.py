"""Validate a TypeSafe credential without sending images or running inference."""
import json
import urllib.error
import urllib.request

from .jev import NoRedirect


def validate_key(key):
    if not isinstance(key, str) or not key.strip() or len(key) > 4096:
        raise ValueError('请输入 TypeSafe API key')
    key = key.strip()
    if any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError('API key 格式无效')
    request = urllib.request.Request('https://api.typesafe.ai/v1/models',
                                     headers={'Authorization': 'Bearer '+key})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            data = json.loads(response.read(1024*1024))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError('TypeSafe 拒绝此密钥，请检查密钥或账号权限') from None
        raise ValueError(f'TypeSafe 连接验证失败（HTTP {exc.code}）') from None
    except Exception:
        raise ValueError('无法验证 TypeSafe 连接，请稍后重试') from None
    if not isinstance(data, dict) or not isinstance(data.get('models'), list) or not data['models']:
        raise ValueError('TypeSafe 模型列表响应无效，尚未启用 JEV')
    return key
