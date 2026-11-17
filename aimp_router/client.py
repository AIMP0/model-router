"""Small synchronous adapter usable by AIMP agents without a model SDK."""
import json
from pathlib import Path
import urllib.request
import urllib.error
from urllib.parse import urlsplit


class ModelClient:
    def __init__(self, token_file, base_url='http://127.0.0.1:8318', timeout=300):
        u = urlsplit(base_url)
        if u.hostname not in ('127.0.0.1', 'localhost', '::1') or u.scheme != 'http' or u.username or u.password:
            raise ValueError('router client requires a loopback HTTP URL; use SSH forwarding for remote access')
        self.token_file, self.base_url, self.timeout = Path(token_file), base_url.rstrip('/'), timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(self.base_url + path, data,
            {'Authorization': 'Bearer ' + self.token_file.read_text(encoding='utf-8').strip(),
             'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                message = json.load(exc).get('error', {}).get('message', 'model router request failed')
            except (ValueError, AttributeError):
                message = 'model router request failed'
            finally:
                exc.close()
            raise RuntimeError(message) from None
        except urllib.error.URLError:
            raise RuntimeError('model router is unreachable') from None

    def generate(self, messages, model='auto', routing=None, tools=None, **options):
        if options.get('stream'):
            raise ValueError('use the HTTP SSE endpoint for streaming')
        payload = {**options, 'model': model, 'messages': messages, 'stream': False}
        if routing is not None:
            payload['routing'] = routing
        if tools is not None:
            payload['tools'] = tools
        return self.request('/v1/chat/completions', payload)
