"""Loopback-only authenticated model API for AIMP and other agents."""
import argparse
import hmac
import json
import os
from pathlib import Path
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .router import Router, RoutingError


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, router, token, port=8318):
        if len(token) < 32:
            raise ValueError('client token must be at least 32 characters')
        self.router, self.token = router, token
        super().__init__(('127.0.0.1', port), Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, data, extra=None):
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        return hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + self.server.token)

    def do_GET(self):
        if not self.authorized():
            return self.send(401, {'error': {'message': 'authentication required'}})
        state = self.server.router.status()
        if self.path == '/health':
            return self.send(200, {'service': 'aimp-model-router', 'running': True, **state})
        if self.path == '/v1/models':
            models = ['auto'] + [x['id'] for x in state['models'] if x['configured']]
            return self.send(200, {'object': 'list', 'data': [{'id': n, 'object': 'model', 'owned_by': 'aimp'} for n in models]})
        return self.send(404, {'error': {'message': 'unknown endpoint'}})

    def do_POST(self):
        self.connection.settimeout(30)
        try:
            if not self.authorized():
                return self.send(401, {'error': {'message': 'authentication required'}})
            if self.path not in ('/v1/chat/completions', '/routing/preview'):
                return self.send(404, {'error': {'message': 'unknown endpoint'}})
            if self.headers.get('Transfer-Encoding'):
                raise RoutingError('chunked request bodies are unsupported')
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 16 * 1024 * 1024:
                    raise RoutingError('request body missing or too large', 413)
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError()
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise ValueError()
            except (ValueError, UnicodeError):
                raise RoutingError('invalid JSON object') from None
            if self.path == '/routing/preview':
                if not isinstance(payload.get('response_format', {}), dict):
                    raise RoutingError('invalid response_format')
                return self.send(200, self.server.router.plan(payload))
            result, route = self.server.router.complete(payload)
            headers = {'X-AIMP-Model': route['selected'], 'X-AIMP-Attempts': str(len(route['attempts']))}
            if not payload.get('stream'):
                return self.send(200, result, headers)
            upstream, first = result
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Connection', 'close')
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(first)
                self.wfile.flush()
                while True:
                    chunk = upstream.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception:
                # Once output is visible, close the stream. Replaying could
                # duplicate text or tool calls in the agent.
                pass
            finally:
                self.close_connection = True
                upstream.close()
        except RoutingError as exc:
            self.send(exc.status, {'error': {'message': str(exc), 'type': 'routing_error'}})
        except (OSError, TimeoutError):
            self.close_connection = True
        except Exception:
            self.send(500, {'error': {'message': 'router internal error'}})


def load_token(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(secrets.token_urlsafe(36))
    return path.read_text(encoding='utf-8').strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--port', type=int, default=8318)
    parser.add_argument('--credentials-file')
    args = parser.parse_args()
    if args.credentials_file and Path(args.credentials_file).exists():
        credentials = json.loads(Path(args.credentials_file).read_text(encoding='utf-8'))
        for name, value in credentials.items():
            if not isinstance(name, str) or not name.replace('_', '').isalnum() or not isinstance(value, str):
                raise ValueError('invalid credential entry')
            os.environ[name] = value
    router = Router(json.loads(Path(args.config).read_text(encoding='utf-8-sig')))
    server = Server(router, load_token(args.token_file), args.port)
    print('AIMP model router listening on 127.0.0.1:%d; configured=%s' % (server.server_port, router.status()['configured']), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
