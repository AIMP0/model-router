"""Read provider balances without guessing amounts or following redirects."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .router import NoRedirect


KINDS = {
    'deepseek': {
        'label': 'DeepSeek', 'metric': '账户余额',
        'endpoint': 'https://api.deepseek.com/user/balance',
        'console': 'https://platform.deepseek.com/',
        'docs': 'https://api-docs.deepseek.com/api/get-user-balance/',
    },
    'openrouter': {
        'label': 'OpenRouter 账户', 'metric': '账户余额',
        'endpoint': 'https://openrouter.ai/api/v1/credits',
        'console': 'https://openrouter.ai/settings/credits',
        'docs': 'https://openrouter.ai/docs/api/api-reference/credits/get-remaining-credits',
        'note': '账户余额接口可能需要 Management API Key；普通 Key 可选择「OpenRouter Key 额度」。',
    },
    'openrouter_key': {
        'label': 'OpenRouter Key 额度', 'metric': 'Key 剩余额度',
        'endpoint': 'https://openrouter.ai/api/v1/key',
        'console': 'https://openrouter.ai/settings/keys',
        'docs': 'https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key',
        'note': '显示 Key 的预算余额，不代表账户总余额。未设置预算时不会显示为无限余额。',
    },
    'codex': {
        'label': 'GPT / Codex 登录', 'metric': '订阅额度',
        'console': 'https://chatgpt.com/codex/settings/usage',
        'note': '当前接入方式没有受支持的金额余额接口，请在官方用量页面查看订阅额度。',
    },
    'openai': {
        'label': 'OpenAI API', 'metric': '账户余额',
        'console': 'https://platform.openai.com/settings/organization/billing/overview',
        'note': '本面板暂未接入可用余额接口；用量和费用不等于账户剩余金额。',
    },
    'anthropic': {
        'label': 'Claude / Anthropic', 'metric': '账户余额',
        'console': 'https://console.anthropic.com/settings/billing',
        'note': '本面板暂未接入余额接口，请到官方控制台查询。',
    },
    'xai': {
        'label': 'Grok / xAI', 'metric': '账户余额',
        'console': 'https://console.x.ai/',
        'note': '本面板暂未接入余额接口，请到官方控制台查询。',
    },
    'custom': {
        'label': '自定义 JSON 余额接口', 'metric': '自定义余额',
        'note': '填写你信任的 GET 接口与金额字段路径，例如 data.balance；密钥只发送到这个地址。',
    },
}


def validate_endpoint(value):
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
        raise ValueError('余额接口地址无效')
    try:
        u = urlsplit(value)
        port = u.port
    except ValueError:
        raise ValueError('余额接口地址无效') from None
    if (u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password
            or u.query or u.fragment or (port is not None and not 1 <= port <= 65535)):
        raise ValueError('余额接口应为不含凭据、查询参数和片段的 HTTP(S) 地址')
    if u.scheme == 'http' and u.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('远程余额接口必须使用 HTTPS')
    return value


def amount(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('missing amount')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('invalid amount') from None
    if (not result.is_finite() or abs(result) > Decimal('1e20')
            or result.as_tuple().exponent < -12 or len(result.as_tuple().digits) > 40):
        raise ValueError('invalid amount')
    return format(result, 'f')


def dotted(data, path):
    for part in path.split('.'):
        data = data[int(part)] if isinstance(data, list) else data[part]
    return data


def read_json(endpoint, key):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    headers = {'Accept': 'application/json', 'User-Agent': 'model-router-balance/0.2'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    request = urllib.request.Request(endpoint, headers=headers)
    with opener.open(request, timeout=8) as response:
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('response too large')
        return json.loads(raw)


def query_balance(provider, fetch=read_json):
    kind = KINDS[provider['kind']]
    result = {
        'id': provider['id'], 'name': provider['name'], 'kind': provider['kind'],
        'models': provider.get('models', []), 'metric': kind['metric'],
        'status': 'unsupported', 'balances': [],
        'console': kind.get('console', ''), 'message': kind.get('note', ''),
        'checked_at': datetime.now(timezone.utc).isoformat(),
    }
    if 'endpoint' not in kind and provider['kind'] != 'custom':
        return result
    if not provider.get('api_key') and provider['kind'] != 'custom':
        result.update(status='not_configured', message='请先配置余额查询密钥。')
        return result
    endpoint = provider.get('endpoint', '') if provider['kind'] == 'custom' else kind['endpoint']
    try:
        validate_endpoint(endpoint)
        data = fetch(endpoint, provider.get('api_key', ''))
        if provider['kind'] == 'deepseek':
            infos = data['balance_infos']
            if not isinstance(infos, list) or not infos or len(infos) > 10:
                raise ValueError('missing balance')
            rows = []
            for row in infos:
                if row['currency'] not in ('CNY', 'USD'):
                    raise ValueError('unknown currency')
                rows.append({'currency': row['currency'], 'total': amount(row['total_balance']),
                             'granted': amount(row['granted_balance']),
                             'topped_up': amount(row['topped_up_balance'])})
            result.update(balances=rows, available=data.get('is_available'))
        elif provider['kind'] == 'openrouter':
            info = data['data']
            total = Decimal(amount(info['total_credits'])) - Decimal(amount(info['total_usage']))
            result['balances'] = [{'currency': 'USD', 'total': amount(total)}]
        elif provider['kind'] == 'openrouter_key':
            remaining = data['data'].get('limit_remaining')
            if remaining is None:
                result.update(status='no_limit', message='此 Key 未返回预算余额；不能据此判断账户余额。')
                return result
            result['balances'] = [{'currency': 'USD', 'total': amount(remaining)}]
        else:
            result['balances'] = [{'currency': provider['currency'],
                                   'total': amount(dotted(data, provider['amount_path']))}]
        result.update(status='ok', message=kind.get('note', ''))
    except urllib.error.HTTPError as exc:
        exc.close()
        code = exc.code
        message = ('密钥无效或无查询权限。' if code in (401, 403) else
                   '查询频率受限，请稍后刷新。' if code == 429 else
                   '接口发生重定向，已停止查询以保护密钥。' if 300 <= code < 400 else
                   '余额接口返回 HTTP %s。' % code)
        result.update(status='error', message=message)
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError):
        result.update(status='error', message='余额接口无法连接或查询超时。')
    except (ValueError, KeyError, TypeError, IndexError, InvalidOperation):
        result.update(status='error', message='接口未返回有效余额，请检查接口类型或金额字段。')
    return result
