import copy
import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from aimp_router.router import Router, RoutingError
from aimp_router.server import Server
from aimp_router.client import ModelClient
try:
    from aimp_preview.storage import Store, Problem
    from aimp_preview.server import PreviewServer
    HAS_WORKBENCH = True
except ModuleNotFoundError as exc:
    if exc.name != 'aimp_preview':
        raise
    HAS_WORKBENCH = False


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.calls.append(payload)
        code = self.server.codes.get(payload['model'], 200)
        self.send_response(code)
        if payload.get('stream') and code == 200:
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            if payload['model'] == 'empty': return
            self.wfile.write(b'data: {"choices":[{"delta":{"tool_calls":[{"id":"call_1"}]}}]}\n\n')
            if payload['model'] != 'partial':
                self.wfile.write(b'data: [DONE]\n\n')
        else:
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'choices': [{'message': {'role': 'assistant', 'content': 'ok',
                'tool_calls': [{'id': 'call_1', 'type': 'function', 'function': {'name': 'read_memory', 'arguments': '{}'}}]}}]}).encode())


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        self.upstream.calls, self.upstream.codes = [], {}
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        target = {'base_url': 'http://127.0.0.1:%d/v1' % self.upstream.server_port, 'model': 'a', 'capabilities': ['tools', 'vision']}
        self.config = {'targets': {'GPT': target, 'Claude': {**target, 'model': 'b'},
                       'DeepSeek Flash': {**target, 'model': 'fast', 'capabilities': []}},
                       'default': 'GPT', 'fallback': ['Claude'], 'cooldown_seconds': 5,
                       'roles': {'design': ['Claude']}, 'tiers': {'simple': ['DeepSeek Flash']}}
        self.router = Router(self.config)
        self.server = Server(self.router, 'x' * 40, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.temp = tempfile.TemporaryDirectory()
        self.token = Path(self.temp.name) / 'token'
        self.token.write_text('x' * 40)
        self.client = ModelClient(self.token, 'http://127.0.0.1:%d' % self.server.server_port)
        self.messages = [{'role': 'user', 'content': 'hello'}]

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()
        self.upstream.shutdown(); self.upstream.server_close()
        self.temp.cleanup()

    def test_default_role_tier_and_explicit_model(self):
        for model, routing, expected in [('auto', None, 'a'), ('auto', {'role': 'design'}, 'b'),
            ('auto', {'tier': 'simple'}, 'fast'), ('Claude', None, 'b')]:
            self.client.generate(self.messages, model=model, routing=routing)
            self.assertEqual(self.upstream.calls[-1]['model'], expected)
            self.assertNotIn('routing', self.upstream.calls[-1])

    def test_retryable_failure_switches_and_circuit_recovers(self):
        self.upstream.codes['a'] = 429
        now = [0]
        self.router.clock = lambda: now[0]
        self.client.generate(self.messages)
        self.assertEqual([p['model'] for p in self.upstream.calls], ['a', 'b'])
        self.client.generate(self.messages)
        self.assertEqual(self.upstream.calls[-1]['model'], 'b')
        self.upstream.codes['a'] = 200
        now[0] = 6
        self.client.generate(self.messages)
        self.assertEqual(self.upstream.calls[-1]['model'], 'a')

    def test_request_and_auth_errors_never_switch(self):
        for status in (400, 401, 403, 404, 422):
            self.upstream.calls.clear()
            self.upstream.codes['a'] = status
            with self.assertRaises(RuntimeError): self.client.generate(self.messages)
            self.assertEqual(len(self.upstream.calls), 1)

    def test_explicit_pin_and_disabled_fallback(self):
        self.upstream.codes['a'] = 503
        with self.assertRaises(RuntimeError): self.client.generate(self.messages, model='GPT')
        self.assertEqual(len(self.upstream.calls), 1)
        self.router.circuits.clear()
        self.router.config['fallback_enabled'] = False
        with self.assertRaises(RuntimeError): self.client.generate(self.messages)
        self.assertEqual(len(self.upstream.calls), 2)

    def test_capabilities_filter_images_and_tool_history(self):
        messages = [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA=='}}]}]
        self.client.generate(messages, routing={'tier': 'simple'})
        self.assertEqual(self.upstream.calls[-1]['model'], 'b')
        messages = self.messages + [{'role': 'assistant', 'tool_calls': [{'id': 't'}]}, {'role': 'tool', 'tool_call_id': 't', 'content': '{}'}]
        self.client.generate(messages, routing={'tier': 'simple'})
        self.assertEqual(self.upstream.calls[-1]['messages'], messages)
        self.assertEqual(self.upstream.calls[-1]['model'], 'b')

    def test_tools_pass_through_without_execution(self):
        tools = [{'type': 'function', 'function': {'name': 'read_memory', 'parameters': {'type': 'object'}}}]
        response = self.client.generate(self.messages, tools=tools)
        self.assertEqual(self.upstream.calls[-1]['tools'], tools)
        self.assertEqual(response['choices'][0]['message']['tool_calls'][0]['id'], 'call_1')

    def test_streaming_fallback_before_first_event(self):
        self.router.config['targets']['GPT']['model'] = 'empty'
        payload = {'model': 'auto', 'messages': self.messages, 'stream': True}
        request = urllib.request.Request(self.client.base_url + '/v1/chat/completions', json.dumps(payload).encode(),
            {'Authorization': 'Bearer ' + 'x' * 40, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.headers['X-AIMP-Model'], 'Claude')
            self.assertEqual(response.headers['X-AIMP-Attempts'], '2')
            self.assertIn(b'call_1', response.read())

    def test_authentication_and_malformed_requests(self):
        with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(self.client.base_url + '/health')
        self.assertEqual(error.exception.code, 401)
        for payload in ({'messages': []}, {'messages': self.messages, 'response_format': None},
                        {'messages': self.messages, 'stream': 'yes'}, {'messages': self.messages, 'routing': {'role': 'missing'}}):
            with self.assertRaises(RuntimeError): self.client.request('/v1/chat/completions', payload)
        self.assertFalse(self.upstream.calls)

    def test_partial_stream_is_not_replayed(self):
        self.router.config['targets']['GPT']['model'] = 'partial'
        request = urllib.request.Request(self.client.base_url + '/v1/chat/completions',
            json.dumps({'model': 'auto', 'messages': self.messages, 'stream': True}).encode(),
            {'Authorization': 'Bearer ' + 'x' * 40})
        with urllib.request.urlopen(request) as response:
            data = response.read()
            self.assertIn(b'call_1', data)
            self.assertNotIn(b'[DONE]', data)
        self.assertEqual(len(self.upstream.calls), 1)

    def test_network_failure_and_all_models_unavailable(self):
        self.router.config['targets']['GPT']['base_url'] = 'http://127.0.0.1:1/v1'
        self.client.generate(self.messages)
        self.assertEqual(self.upstream.calls[-1]['model'], 'b')
        self.upstream.codes['b'] = 503
        with self.assertRaises(RuntimeError): self.client.generate(self.messages)
        calls = len(self.upstream.calls)
        with self.assertRaises(RuntimeError): self.client.generate(self.messages)
        self.assertEqual(len(self.upstream.calls), calls)

    def test_redirect_does_not_forward_credentials(self):
        self.upstream.codes['a'] = 302
        with self.assertRaises(RuntimeError): self.client.generate(self.messages)
        self.assertEqual(len(self.upstream.calls), 1)

    def test_missing_credentials_and_preview_do_not_call_upstream(self):
        for t in self.router.config['targets'].values(): t['api_key_env'] = 'AIMP_TEST_MISSING_SECRET'
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(self.client.request('/health')['configured'])
            self.assertFalse(self.client.request('/routing/preview', {'messages': self.messages})['candidates'])
            with self.assertRaises(RuntimeError): self.client.generate(self.messages)
        self.assertFalse(self.upstream.calls)

    @unittest.skipUnless(HAS_WORKBENCH, 'optional AIMP workbench integration')
    def test_aimp_uses_saved_assignments_and_fallback_setting(self):
        with patch.dict(os.environ, {'AIMP_ROUTER_TOKEN_FILE': str(self.token), 'AIMP_ROUTER_URL': self.client.base_url}):
            store = Store(Path(self.temp.name) / 'data')
            state = store.gateway.state()
            state['config']['roles']['design'] = ['Claude', 'GPT']
            state['config']['fallback_enabled'] = False
            store.gateway.update(state)
            store.gateway.generate({'messages': self.messages, 'role': 'design'})
            self.assertEqual(self.upstream.calls[-1]['model'], 'b')
            with self.assertRaises(Problem): store.gateway.generate({'messages': self.messages, 'sensitive': True})

    def test_mcp_handshake_list_and_generate(self):
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'generate', 'arguments': {'messages': self.messages}}}]
        result = subprocess.run([sys.executable, '-m', 'aimp_router.mcp', '--token-file', str(self.token), '--url', self.client.base_url],
            input='\n'.join(json.dumps(r) for r in requests) + '\n', capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(replies), 3)
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-11-25')
        self.assertEqual(len(replies[1]['result']['tools']), 4)
        self.assertNotIn('isError', replies[2]['result'])

    @unittest.skipUnless(HAS_WORKBENCH, 'optional AIMP workbench integration')
    def test_authenticated_workbench_endpoint_reaches_router(self):
        preview = PreviewServer(Path(self.temp.name) / 'workbench', 0)
        threading.Thread(target=preview.serve_forever, daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        base = 'http://127.0.0.1:%d' % preview.server_port
        def call(path, data):
            request = urllib.request.Request(base + path, json.dumps(data).encode(),
                {'Content-Type': 'application/json', 'X-Lisa-Client': '1'})
            with opener.open(request) as result: return json.load(result)
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                call('/api/gateway/generate', {'messages': self.messages})
            self.assertEqual(error.exception.code, 401)
            call('/api/login', {'token': preview.token})
            with patch.dict(os.environ, {'AIMP_ROUTER_TOKEN_FILE': str(self.token), 'AIMP_ROUTER_URL': self.client.base_url}):
                result = call('/api/gateway/generate', {'messages': self.messages})
                self.assertEqual(result['choices'][0]['message']['content'], 'ok')
                self.assertEqual(self.upstream.calls[-1]['model'], 'fast')
        finally:
            preview.shutdown(); preview.server_close()


if __name__ == '__main__': unittest.main()
