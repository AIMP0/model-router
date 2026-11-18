"""A separately authenticated, IP-configurable balance dashboard."""
import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import ipaddress
import json
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urlsplit
import uuid

from .balances import KINDS, query_balance, validate_endpoint
from .configure import write_private
from .server import load_token


ASSETS = Path(__file__).with_name('web')
COOKIE = 'model_router_session'


def network_settings(value):
    if not isinstance(value, dict) or set(value) - {'host', 'port', 'public_url'}:
        raise ValueError('访问配置无效')
    host, port = value.get('host', ''), value.get('port')
    if not isinstance(host, str) or not (host == 'localhost' or isinstance(host, str) and _ipv4(host)):
        raise ValueError('监听 IP 请填写 IPv4 地址或 localhost；所有网卡使用 0.0.0.0')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('端口必须为 1..65535')
    public_url = value.get('public_url', '')
    if not isinstance(public_url, str):
        raise ValueError('访问地址无效')
    public_url = public_url.strip()
    if public_url:
        try:
            u = urlsplit(public_url)
            valid = (u.scheme in ('http', 'https') and u.hostname and not u.username
                     and not u.password and not u.query and not u.fragment
                     and u.path in ('', '/') and (u.port is None or 1 <= u.port <= 65535)
                     and not any(c.isspace() for c in public_url))
        except ValueError:
            valid = False
        if not valid or len(public_url) > 2048:
            raise ValueError('访问地址应类似 http://192.168.1.20:8319 或 https://models.example.com')
    return {'host': host, 'port': port, 'public_url': public_url.rstrip('/')}


def _ipv4(value):
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False


def validate_provider(value, previous=None):
    if not isinstance(value, dict) or set(value) - {
            'id', 'name', 'kind', 'models', 'api_key', 'clear_key',
            'endpoint', 'currency', 'amount_path'}:
        raise ValueError('提供商配置无效')
    name, kind = value.get('name'), value.get('kind')
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or not isinstance(kind, str) or kind not in KINDS:
        raise ValueError('请填写提供商名称并选择接口类型')
    models = value.get('models', [])
    if (not isinstance(models, list) or len(models) > 100 or
            any(not isinstance(x, str) or not 1 <= len(x) <= 120 for x in models)):
        raise ValueError('关联模型列表无效')
    key = value.get('api_key', '')
    if not isinstance(key, str) or len(key) > 4096 or any(c.isspace() for c in key):
        raise ValueError('密钥不能包含空白字符')
    # Retain only when the destination and adapter are unchanged. A key must not
    # silently travel to a newly selected provider or custom endpoint.
    if not key and not value.get('clear_key') and previous:
        if kind == previous['kind'] and (kind != 'custom' or value.get('endpoint') == previous.get('endpoint')):
            key = previous.get('api_key', '')
    result = {'id': previous['id'] if previous else uuid.uuid4().hex,
              'name': name.strip(), 'kind': kind, 'models': list(dict.fromkeys(models)),
              'api_key': key}
    if kind not in ('deepseek', 'openrouter', 'openrouter_key', 'custom'):
        result['api_key'] = ''
    if kind == 'custom':
        result['endpoint'] = validate_endpoint(value.get('endpoint', ''))
        path = value.get('amount_path', '')
        currency = value.get('currency', '')
        if not isinstance(path, str) or not re.fullmatch(r'[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*', path) or len(path) > 200:
            raise ValueError('金额字段使用点分隔路径，例如 data.balance')
        if currency not in ('CNY', 'USD', 'credits'):
            raise ValueError('币种应为 CNY、USD 或 credits')
        result.update(amount_path=path, currency=currency)
    return result


class DashboardStore:
    def __init__(self, runtime, balance_query=query_balance):
        self.runtime = Path(runtime).resolve()
        self.path = self.runtime / 'dashboard.json'
        self.lock = threading.RLock()
        self.refresh_lock = threading.Lock()
        self.balance_query = balance_query
        self.cache = None
        self.version = 0
        self.data = json.loads(self.path.read_text(encoding='utf-8'))
        self.data['network'] = network_settings(self.data['network'])
        providers = self.data.get('providers', [])
        if not isinstance(providers, list) or len(providers) > 32:
            raise ValueError('提供商配置无效')
        # Validate saved configuration without resetting private keys or IDs.
        self.data['providers'] = [validate_provider(p, p) for p in providers]

    def _save(self, data):
        write_private(self.path, data)
        self.data = data
        self.version += 1
        self.cache = None

    def state(self):
        with self.lock:
            providers = [{k: v for k, v in p.items() if k != 'api_key'} |
                         {'key_configured': bool(p.get('api_key'))} for p in self.data['providers']]
            try:
                config = json.loads((self.runtime / 'config.json').read_text(encoding='utf-8-sig'))
                models = [{'alias': alias, 'model': t.get('model', ''), 'enabled': t.get('enabled', True)}
                          for alias, t in config.get('targets', {}).items()]
            except (OSError, ValueError, AttributeError):
                models = []
            return {'network': copy.deepcopy(self.data['network']), 'providers': providers,
                    'models': models, 'kinds': KINDS}

    def save_provider(self, value):
        with self.lock:
            provider_id = value.get('id') if isinstance(value, dict) else None
            old = next((p for p in self.data['providers'] if p['id'] == provider_id), None)
            if provider_id and old is None:
                raise ValueError('提供商不存在，请刷新页面')
            if old is None and len(self.data['providers']) >= 32:
                raise ValueError('最多配置 32 个提供商账号')
            new = validate_provider(value, old)
            data = copy.deepcopy(self.data)
            data['providers'] = [new if p['id'] == new['id'] else p for p in data['providers']]
            if old is None:
                data['providers'].append(new)
            self._save(data)

    def delete_provider(self, provider_id):
        with self.lock:
            data = copy.deepcopy(self.data)
            if not any(p['id'] == provider_id for p in data['providers']):
                raise ValueError('提供商不存在')
            data['providers'] = [p for p in data['providers'] if p['id'] != provider_id]
            self._save(data)

    def save_network(self, value):
        network = network_settings(value)
        with self.lock:
            data = copy.deepcopy(self.data)
            data['network'] = network
            self._save(data)

    def balances(self, force=False):
        with self.refresh_lock:
            with self.lock:
                if self.cache and time.monotonic() - self.cache[0] < (5 if force else 60):
                    return self.cache[1]
                version = self.version
                providers = copy.deepcopy(self.data['providers'])
            with ThreadPoolExecutor(max_workers=4) as executor:
                rows = list(executor.map(self.balance_query, providers))
            result = {'providers': rows, 'updated_at': datetime.now(timezone.utc).isoformat()}
            with self.lock:
                if version == self.version:
                    self.cache = (time.monotonic(), result)
                    return result
            # A key or provider changed while querying. Never return stale data
            # for a deleted account; the caller can refresh the new state.
            return {'providers': [], 'updated_at': result['updated_at'], 'changed': True}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, store, token, host=None, port=None, secure_cookies=False):
        if not isinstance(token, str) or len(token) < 32 or not token.isascii():
            raise ValueError('管理令牌至少需要 32 个 ASCII 字符')
        self.store, self.token = store, token
        self.sessions, self.attempts = {}, {}
        self.auth_lock = threading.Lock()
        self.secure_cookies = secure_cookies
        self.network = copy.deepcopy(store.data['network'])
        super().__init__((host if host is not None else self.network['host'],
                          port if port is not None else self.network['port']), DashboardHandler)

    def host_allowed(self, host):
        try:
            u = urlsplit('http://' + host)
            if not host or u.username or u.password or u.path or u.query or u.fragment:
                return False
            public = urlsplit(self.network.get('public_url', ''))
            if public.netloc and host.lower() == public.netloc.lower():
                return True
            return (u.hostname in ('localhost', '127.0.0.1') or _ipv4(u.hostname)) and (u.port or 80) == self.server_port
        except (ValueError, TypeError):
            return False


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, data, content_type='application/json; charset=utf-8', cookie=None):
        raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(raw)

    def session(self):
        try:
            cookies = SimpleCookie(self.headers.get('Cookie', ''))
            session_id = cookies[COOKIE].value
        except (KeyError, ValueError, CookieError):
            return None
        with self.server.auth_lock:
            current = self.server.sessions.get(session_id)
            if current and current['expires'] > time.monotonic():
                return current
            self.server.sessions.pop(session_id, None)
        return None

    def do_GET(self):
        if not self.server.host_allowed(self.headers.get('Host', '')):
            return self.send(403, {'error': '访问地址不允许'})
        assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                  '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                  '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if self.path in assets:
            file, content_type = assets[self.path]
            return self.send(200, (ASSETS / file).read_bytes(), content_type)
        session = self.session()
        if not session:
            return self.send(401, {'error': '请先登录'})
        if self.path == '/api/state':
            return self.send(200, self.server.store.state() | {'csrf': session['csrf'],
                             'active_network': self.server.network,
                             'active_port': self.server.server_port})
        if self.path == '/api/balances':
            return self.send(200, self.server.store.balances())
        return self.send(404, {'error': '接口不存在'})

    def do_POST(self):
        self.connection.settimeout(15)
        try:
            host = self.headers.get('Host', '')
            if not self.server.host_allowed(host) or self.headers.get('X-Dashboard-Client') != '1':
                return self.send(403, {'error': '请求来源不允许'})
            origin = self.headers.get('Origin')
            expected_origin = ('https://' if self.server.secure_cookies else 'http://') + host
            if origin and origin != expected_origin:
                return self.send(403, {'error': '请求来源不允许'})
            if self.headers.get('Transfer-Encoding') or self.headers.get_content_type() != 'application/json':
                return self.send(400, {'error': '需要 JSON 请求'})
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 65536:
                return self.send(413, {'error': '请求过大或缺少内容'})
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError('请求应为 JSON 对象')
            if self.path == '/api/login':
                return self.login(payload)
            session = self.session()
            if not session:
                return self.send(401, {'error': '请先登录'})
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf']):
                return self.send(403, {'error': '页面已过期，请刷新后重试'})
            if self.path == '/api/logout':
                with self.server.auth_lock:
                    self.server.sessions = {k: v for k, v in self.server.sessions.items() if v is not session}
                return self.send(200, {'ok': True}, cookie=COOKIE + '=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0')
            if self.path == '/api/providers/save':
                self.server.store.save_provider(payload)
            elif self.path == '/api/providers/delete':
                self.server.store.delete_provider(payload.get('id'))
            elif self.path == '/api/network':
                self.server.store.save_network(payload)
            elif self.path == '/api/balances/refresh':
                return self.send(200, self.server.store.balances(force=True))
            else:
                return self.send(404, {'error': '接口不存在'})
            return self.send(200, {'ok': True})
        except (ValueError, UnicodeError) as exc:
            message = str(exc) if type(exc) is ValueError else '请求内容无效'
            return self.send(400, {'error': message or '请求内容无效'})
        except (OSError, TimeoutError):
            self.close_connection = True
        except Exception:
            return self.send(500, {'error': '操作未完成，请检查本地配置和文件权限'})

    def login(self, payload):
        now = time.monotonic()
        address = self.client_address[0]
        with self.server.auth_lock:
            self.server.attempts = {k: v for k, v in self.server.attempts.items() if v[0] > now - 60}
            since, count = self.server.attempts.get(address, (now, 0))
            if count >= 10:
                return self.send(429, {'error': '尝试次数较多，请一分钟后重试'})
            token = payload.get('token', '')
            if not isinstance(token, str) or not hmac.compare_digest(token.encode(), self.server.token.encode()):
                self.server.attempts[address] = (since, count + 1)
                return self.send(401, {'error': '管理令牌不正确'})
            self.server.attempts.pop(address, None)
            self.server.sessions = {k: v for k, v in self.server.sessions.items() if v['expires'] > now}
            if len(self.server.sessions) >= 128:
                self.server.sessions.pop(next(iter(self.server.sessions)))
            session_id = secrets.token_urlsafe(32)
            self.server.sessions[session_id] = {'expires': now + 12 * 3600, 'csrf': secrets.token_urlsafe(32)}
        cookie = COOKIE + '=' + session_id + '; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200'
        if self.server.secure_cookies:
            cookie += '; Secure'
        return self.send(200, {'ok': True}, cookie=cookie)


def main():
    parser = argparse.ArgumentParser(description='Model Router balance dashboard')
    parser.add_argument('--runtime-dir', type=Path, default=Path('runtime'))
    parser.add_argument('--secure-cookies', action='store_true', help='use behind an HTTPS reverse proxy')
    args = parser.parse_args()
    runtime = args.runtime_dir.expanduser().resolve()
    store = DashboardStore(runtime)
    token_path = runtime / 'dashboard-token'
    if not token_path.exists():
        parser.error('Run python3 setup_dashboard.py first.')
    server = DashboardServer(store, load_token(token_path), secure_cookies=args.secure_cookies)
    print('Dashboard listening on %s:%d' % server.server_address, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
