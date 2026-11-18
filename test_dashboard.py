"""Balance contracts and the real HTTP administration boundary; no paid APIs."""
import copy
import http.cookiejar
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

from aimp_router.balances import amount, query_balance, read_json
from aimp_router.dashboard import DashboardServer, DashboardStore, network_settings
from setup_dashboard import initialize


def provider(kind='deepseek', **extra):
    return {'id': 'p', 'name': '测试账号', 'kind': kind,
            'models': ['DeepSeek Flash', 'DeepSeek Pro'], 'api_key': 'test-balance-key', **extra}


class BalanceTests(unittest.TestCase):
    def test_deepseek_preserves_currencies_and_shared_account(self):
        result = query_balance(provider(), lambda *args: {'is_available': True, 'balance_infos': [
            {'currency': 'CNY', 'total_balance': '18.50', 'granted_balance': '3.00', 'topped_up_balance': '15.50'},
            {'currency': 'USD', 'total_balance': '0.00', 'granted_balance': '0', 'topped_up_balance': '0'}]})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['balances'][0]['total'], '18.50')
        self.assertEqual(len(result['models']), 2)
        self.assertEqual(result['balances'][1]['total'], '0.00')
        self.assertNotIn('api_key', json.dumps(result))

    def test_openrouter_money_and_key_budget_are_distinct(self):
        result = query_balance(provider('openrouter'), lambda *args: {'data': {'total_credits': '100.50', 'total_usage': '23.19'}})
        self.assertEqual(result['balances'][0], {'currency': 'USD', 'total': '77.31'})
        result = query_balance(provider('openrouter_key'), lambda *args: {'data': {'limit_remaining': 12}})
        self.assertEqual(result['metric'], 'Key 剩余额度')
        self.assertEqual(result['balances'][0]['total'], '12')
        for data in ({}, {'limit_remaining': None}):
            result = query_balance(provider('openrouter_key'), lambda *args: {'data': data})
            self.assertEqual(result['status'], 'no_limit')
            self.assertEqual(result['balances'], [])

    def test_missing_or_unsupported_never_calls_a_provider(self):
        fetch = Mock(side_effect=AssertionError('must not call provider'))
        self.assertEqual(query_balance(provider(api_key=''), fetch)['status'], 'not_configured')
        for kind in ('codex', 'openai', 'anthropic', 'xai'):
            result = query_balance(provider(kind), fetch)
            self.assertEqual(result['status'], 'unsupported')
            self.assertEqual(result['balances'], [])
        fetch.assert_not_called()

    def test_custom_and_malformed_amounts_do_not_become_zero(self):
        p = provider('custom', endpoint='https://balance.example.com/account', amount_path='data.0.balance', currency='CNY')
        result = query_balance(p, lambda *args: {'data': [{'balance': '0.01'}]})
        self.assertEqual(result['balances'][0]['total'], '0.01')
        for bad in (None, True, 'oops', 'NaN', 'Infinity', '1e-9999999', {}):
            result = query_balance(p, lambda *args: {'data': [{'balance': bad}]})
            self.assertEqual(result['status'], 'error', str(bad))
            self.assertEqual(result['balances'], [])
        for bad in ({}, [], {'balance_infos': []}, {'balance_infos': None}):
            self.assertEqual(query_balance(provider(), lambda *args: bad)['status'], 'error')

    def test_upstream_errors_are_redacted(self):
        def fail(*args):
            raise urllib.error.HTTPError('https://api.deepseek.com/user/balance', 403,
                                         'test-balance-key and upstream private text', {}, None)
        result = query_balance(provider(), fail)
        self.assertEqual(result['status'], 'error')
        self.assertNotIn('test-balance-key', json.dumps(result))
        self.assertNotIn('upstream private text', json.dumps(result))


class BalanceHTTPTests(unittest.TestCase):
    def test_bearer_auth_is_never_forwarded_on_redirect(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                self.server.calls.append((self.path, self.headers.get('Authorization')))
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/stolen')
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"data":{"balance":"5.25"}}')
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.calls = []
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = 'http://127.0.0.1:%d' % server.server_port
        try:
            self.assertEqual(read_json(base + '/balance', 'test-private-key')['data']['balance'], '5.25')
            with self.assertRaises(urllib.error.HTTPError): read_json(base + '/redirect', 'test-private-key')
            self.assertEqual(server.calls, [('/balance', 'Bearer test-private-key'), ('/redirect', 'Bearer test-private-key')])
        finally:
            server.shutdown(); server.server_close()


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        initialize(self.runtime, {'host': '127.0.0.1', 'port': 8319, 'public_url': ''}, 't' * 40)
        self.query = Mock(side_effect=lambda p: query_balance(p, Mock()))
        self.store = DashboardStore(self.runtime, self.query)

    def tearDown(self): self.temp.cleanup()

    def test_secrets_are_retained_but_never_returned(self):
        p = provider(id='deepseek')
        self.store.save_provider(p)
        p['api_key'] = ''
        self.store.save_provider(p)
        self.assertEqual(self.store.data['providers'][0]['api_key'], 'test-balance-key')
        self.assertNotIn('test-balance-key', json.dumps(self.store.state()))
        self.assertTrue(self.store.state()['providers'][0]['key_configured'])
        self.assertEqual(DashboardStore(self.runtime).data['providers'][0]['api_key'], 'test-balance-key')
        p['clear_key'] = True
        self.store.save_provider(p)
        self.assertFalse(self.store.state()['providers'][0]['key_configured'])

    def test_key_not_reused_for_different_provider_or_endpoint(self):
        p = provider(id='deepseek')
        self.store.save_provider(p)
        self.store.save_provider({**p, 'kind': 'openrouter', 'api_key': ''})
        self.assertFalse(self.store.state()['providers'][0]['key_configured'])
        custom = provider('custom', id='deepseek', endpoint='https://a.example.com/balance', amount_path='balance', currency='USD')
        self.store.save_provider(custom)
        self.store.save_provider({**custom, 'endpoint': 'https://b.example.com/balance', 'api_key': ''})
        self.assertFalse(self.store.state()['providers'][0]['key_configured'])

    def test_cache_and_deleting_an_account(self):
        one = self.store.balances()
        two = self.store.balances(force=True)
        self.assertEqual(one, two)
        self.assertEqual(self.query.call_count, 4)
        self.store.delete_provider('deepseek')
        self.assertEqual(len(self.store.balances()['providers']), 3)
        self.assertEqual(self.query.call_count, 7)

    def test_configuration_rejects_unknown_fields_and_insecure_remote(self):
        for bad in ('http://192.168.1.1/balance', 'https://user:secret@a.example.com/balance', 'https://a.example.com/balance?key=x'):
            with self.assertRaises(ValueError):
                self.store.save_provider(provider('custom', id='deepseek', endpoint=bad, amount_path='balance', currency='USD'))
        with self.assertRaises(ValueError): self.store.save_provider(provider(id='deepseek', surprise='bad'))
        with self.assertRaises(ValueError): self.store.save_provider(provider(id='missing'))
        with self.assertRaises(ValueError): network_settings({'host': 'a.evil.test', 'port': 8319})
        with self.assertRaises(ValueError): network_settings({'host': '127.0.0.1', 'port': True})
        with self.assertRaises(ValueError): initialize(self.runtime, self.store.data['network'])


class DashboardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        initialize(self.runtime, {'host': '127.0.0.1', 'port': 8319, 'public_url': ''}, 't' * 40)
        self.store = DashboardStore(self.runtime)
        self.server = DashboardServer(self.store, 't' * 40, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = 'http://127.0.0.1:%d' % self.server.server_port
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = ''

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.temp.cleanup()

    def call(self, path, body=None, **headers):
        standard = {'Content-Type': 'application/json', 'X-Dashboard-Client': '1', 'X-CSRF-Token': self.csrf}
        standard.update(headers)
        request = urllib.request.Request(self.base + path, json.dumps(body).encode() if body is not None else None, standard)
        with self.opener.open(request, timeout=12) as response:
            return json.load(response)

    def login(self):
        self.call('/api/login', {'token': 't' * 40})
        self.csrf = self.call('/api/state')['csrf']

    def test_anonymous_cannot_read_write_or_fetch_balances(self):
        for path, body in (('/api/state', None), ('/api/balances', None), ('/api/providers/save', provider())):
            with self.assertRaises(urllib.error.HTTPError) as error: self.call(path, body)
            self.assertEqual(error.exception.code, 401)
        self.assertEqual(len(self.store.data['providers']), 4)

    def test_login_cookie_and_logout(self):
        request = urllib.request.Request(self.base + '/api/login', json.dumps({'token': 't' * 40}).encode(),
                                         {'Content-Type': 'application/json', 'X-Dashboard-Client': '1'})
        with self.opener.open(request) as response:
            cookie = response.headers['Set-Cookie']
            self.assertIn('HttpOnly', cookie)
            self.assertIn('SameSite=Strict', cookie)
            self.assertNotIn('t' * 40, cookie)
        self.csrf = self.call('/api/state')['csrf']
        self.call('/api/logout', {})
        with self.assertRaises(urllib.error.HTTPError) as error: self.call('/api/state')
        self.assertEqual(error.exception.code, 401)

    def test_cross_origin_csrf_and_dns_rebinding_are_rejected(self):
        self.login()
        for headers in ({'Origin': 'https://evil.test'}, {'X-CSRF-Token': ''}, {'Host': 'evil.test'}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.call('/api/providers/delete', {'id': 'deepseek'}, **headers)
            self.assertEqual(error.exception.code, 403)
        self.assertEqual(len(self.store.data['providers']), 4)

    def test_save_query_and_reload_with_no_secret_in_response(self):
        self.login()
        self.call('/api/providers/save', provider(id='deepseek'))
        state = self.call('/api/state')
        self.assertNotIn('test-balance-key', json.dumps(state))
        self.assertTrue(state['providers'][0]['key_configured'])
        # Other defaults are unsupported and do not call upstream. Replace the
        # test balance adapter to avoid contacting the real DeepSeek API.
        self.store.balance_query = lambda p: query_balance(p, lambda *args: {'is_available': True, 'balance_infos': [
            {'currency': 'CNY', 'total_balance': '0.00', 'granted_balance': '0', 'topped_up_balance': '0'}]})
        result = self.call('/api/balances/refresh', {})
        self.assertEqual(result['providers'][0]['status'], 'ok')
        self.assertEqual(result['providers'][0]['balances'][0]['total'], '0.00')
        self.assertNotIn('test-balance-key', json.dumps(result))

    def test_network_settings_are_persisted_and_apply_on_restart(self):
        self.login()
        changed = {'host': '0.0.0.0', 'port': 8320, 'public_url': 'http://192.168.1.20:8320'}
        self.call('/api/network', changed)
        state = self.call('/api/state')
        self.assertEqual(state['network'], changed)
        self.assertEqual(state['active_network']['host'], '127.0.0.1')
        self.assertEqual(DashboardStore(self.runtime).data['network'], changed)

    def test_static_assets_csp_and_path_traversal(self):
        for path in ('/', '/app.js', '/style.css'):
            with self.opener.open(self.base + path) as response:
                self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
                self.assertTrue(response.read())
        with self.assertRaises(urllib.error.HTTPError): self.opener.open(self.base + '/../../runtime/dashboard-token')

    def test_login_rate_limit(self):
        for i in range(11):
            with self.assertRaises(urllib.error.HTTPError) as error: self.call('/api/login', {'token': 'wrong'})
            self.assertEqual(error.exception.code, 401 if i < 10 else 429)


if __name__ == '__main__':
    unittest.main()
