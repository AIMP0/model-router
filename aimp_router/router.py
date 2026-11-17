"""OpenAI-compatible routing with ordered fallback and circuit cooldown."""
import copy
import http.client
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

RETRYABLE = {408, 429, 500, 502, 503, 504}
MAX_RESPONSE = 32 * 1024 * 1024


class RoutingError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Router:
    def __init__(self, config, clock=time.monotonic):
        self.config = copy.deepcopy(config)
        self.clock = clock
        self.lock = threading.Lock()
        self.circuits = {}
        self.counts = {'requests': 0, 'fallbacks': 0, 'failures': 0}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.validate()

    def validate(self):
        c = self.config
        if not isinstance(c.get('targets'), dict) or not c['targets']:
            raise ValueError('targets must be a non-empty object')
        for name, target in c['targets'].items():
            if not isinstance(name, str) or not isinstance(target, dict):
                raise ValueError('invalid target')
            u = urlsplit(target.get('base_url', ''))
            if u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ValueError('invalid base_url')
            if u.scheme != 'https' and u.hostname not in ('localhost', '127.0.0.1', '::1'):
                raise ValueError('non-local upstream requires HTTPS')
            if not isinstance(target.get('model'), str) or not target['model']:
                raise ValueError('target requires model')
            if not isinstance(target.get('capabilities', []), list):
                raise ValueError('invalid capabilities')
            if target.get('api_key_env') and not isinstance(target['api_key_env'], str):
                raise ValueError('invalid credential environment name')
        if c.get('default') not in c['targets']:
            raise ValueError('default target missing')
        chains = [c.get('fallback', [])]
        for section in ('roles', 'tiers'):
            if not isinstance(c.get(section, {}), dict):
                raise ValueError('invalid routing rules')
            chains.extend(c.get(section, {}).values())
        for chain in chains:
            if not isinstance(chain, list) or any(n not in c['targets'] for n in chain):
                raise ValueError('route references an unknown target')
        if not 1 <= c.get('timeout_seconds', 60) <= 300:
            raise ValueError('timeout must be 1..300')
        if not 1 <= c.get('cooldown_seconds', 30) <= 3600:
            raise ValueError('cooldown must be 1..3600')

    def configured(self, name):
        target = self.config['targets'][name]
        env = target.get('api_key_env')
        return target.get('enabled', True) is True and (not env or bool(os.environ.get(env)))

    def plan(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get('messages'), list) or not payload['messages']:
            raise RoutingError('messages must be a non-empty array')
        if any(not isinstance(m, dict) or m.get('role') not in ('system', 'developer', 'user', 'assistant', 'tool', 'function') for m in payload['messages']):
            raise RoutingError('invalid message')
        model = payload.get('model', 'auto')
        if not isinstance(model, str):
            raise RoutingError('model must be a string')
        route = payload.get('routing', {})
        if not isinstance(route, dict) or set(route) - {'role', 'tier', 'chain'}:
            raise RoutingError('routing accepts role, tier or chain')
        required = set()
        if payload.get('tools') or any(m.get('tool_calls') or m.get('role') == 'tool' for m in payload['messages']):
            required.add('tools')
        for message in payload['messages']:
            content = message.get('content')
            if isinstance(content, list) and any(isinstance(x, dict) and x.get('type') in ('image_url', 'input_image') for x in content):
                required.add('vision')
        if payload.get('response_format', {}).get('type') == 'json_schema':
            required.add('json_schema')
        if model != 'auto':
            if model not in self.config['targets']:
                raise RoutingError('unknown model alias', 404)
            candidates, reason = [model], 'explicit'
        else:
            role, tier = route.get('role'), route.get('tier')
            if role is not None and role not in self.config.get('roles', {}):
                raise RoutingError('unknown role')
            if tier is not None and tier not in self.config.get('tiers', {}):
                raise RoutingError('unknown tier')
            if 'chain' in route:
                chain = route['chain']
                if not isinstance(chain, list) or not chain or len(chain) > 20 or any(not isinstance(n, str) or n not in self.config['targets'] for n in chain):
                    raise RoutingError('chain must contain known model aliases')
                candidates, reason = chain[:], 'agent-policy'
            elif role is not None:
                candidates, reason = self.config['roles'][role][:], 'role:' + role
            elif tier is not None:
                candidates, reason = self.config['tiers'][tier][:], 'tier:' + tier
            else:
                candidates, reason = [self.config['default']], 'default'
            if 'chain' in route:
                pass  # The authenticated agent already supplied its full policy.
            elif self.config.get('fallback_enabled', True):
                candidates += self.config.get('fallback', [])
            else:
                candidates = candidates[:1]
        seen, selected, skipped = set(), [], []
        with self.lock:
            for name in candidates:
                if name in seen:
                    continue
                seen.add(name)
                target = self.config['targets'][name]
                why = ('not_configured' if not self.configured(name) else
                       'capability' if not required.issubset(target.get('capabilities', [])) else
                       'cooldown' if self.circuits.get(name, 0) > self.clock() else '')
                if why:
                    skipped.append({'model': name, 'reason': why})
                else:
                    selected.append(name)
        return {'candidates': selected, 'skipped': skipped, 'reason': reason, 'required': sorted(required)}

    def status(self):
        with self.lock:
            return {'configured': any(self.configured(n) for n in self.config['targets']),
                    'models': [{'id': n, 'configured': self.configured(n),
                                'capabilities': t.get('capabilities', []),
                                'cooldown_seconds': max(0, round(self.circuits.get(n, 0) - self.clock()))}
                               for n, t in self.config['targets'].items()], 'counters': dict(self.counts)}

    def complete(self, payload):
        """Return decoded JSON or an open SSE response; never retry after returning it."""
        if 'stream' in payload and type(payload['stream']) is not bool:
            raise RoutingError('stream must be boolean')
        if not isinstance(payload.get('response_format', {}), dict):
            raise RoutingError('invalid response_format')
        plan = self.plan(payload)
        if not plan['candidates']:
            raise RoutingError('no configured, capable model is currently available', 503)
        with self.lock:
            self.counts['requests'] += 1
        attempts = []
        for name in plan['candidates']:
            t = self.config['targets'][name]
            data = {k: v for k, v in payload.items() if k != 'routing'}
            data['model'] = t['model']
            headers = {'Content-Type': 'application/json', 'Accept': 'text/event-stream' if data.get('stream') else 'application/json'}
            if t.get('api_key_env'):
                headers['Authorization'] = 'Bearer ' + os.environ[t['api_key_env']]
            request = urllib.request.Request(t['base_url'].rstrip('/') + '/chat/completions',
                                             json.dumps(data).encode(), headers)
            response = None
            keep_stream = False
            try:
                response = self.opener.open(request, timeout=self.config.get('timeout_seconds', 60))
                if data.get('stream'):
                    if 'text/event-stream' not in response.headers.get('Content-Type', ''):
                        raise RoutingError('upstream did not return an SSE stream', 502)
                    # Wait for the first complete SSE event before committing headers.
                    first = bytearray()
                    while len(first) <= 1024 * 1024:
                        line = response.readline(65536)
                        if not line:
                            raise RoutingError('upstream stream ended before its first event', 502)
                        first.extend(line)
                        if not line.strip() and b'data:' in first:
                            break
                    else:
                        raise RoutingError('upstream SSE event is too large', 502)
                    for line in bytes(first).splitlines():
                        if line.startswith(b'data:') and line[5:].strip() != b'[DONE]':
                            try:
                                event = json.loads(line[5:])
                            except ValueError:
                                raise RoutingError('invalid upstream SSE event', 502) from None
                            if 'error' in event:
                                # Semantic errors in a 200 stream may be request errors:
                                # do not silently switch provider for these.
                                raise RoutingError('upstream returned an error event', 400)
                    result = (response, bytes(first))
                    keep_stream = True
                else:
                    raw = response.read(MAX_RESPONSE + 1)
                    if len(raw) > MAX_RESPONSE:
                        raise RoutingError('upstream response too large', 502)
                    try:
                        result = json.loads(raw)
                    except ValueError:
                        raise RoutingError('invalid upstream response', 502) from None
                    if not isinstance(result, dict) or not result.get('choices'):
                        raise RoutingError('upstream response has no choices', 502)
                    response.close()
                attempts.append({'model': name, 'status': 200})
                with self.lock:
                    self.circuits.pop(name, None)
                    self.counts['fallbacks'] += int(len(attempts) > 1)
                return result, {'selected': name, 'reason': plan['reason'], 'attempts': attempts}
            except urllib.error.HTTPError as exc:
                status = exc.code
                exc.close()
            except RoutingError as exc:
                status = exc.status
            except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, http.client.HTTPException):
                status = 503
            finally:
                # Successful streams stay open for the HTTP handler to consume.
                if response is not None and not keep_stream:
                    response.close()
            attempts.append({'model': name, 'status': status})
            if status not in RETRYABLE:
                raise RoutingError('upstream rejected request (HTTP %s); no fallback performed' % status, status if status in (400, 401, 403, 404, 413, 422) else 502)
            with self.lock:
                self.circuits[name] = self.clock() + self.config.get('cooldown_seconds', 30)
        with self.lock:
            self.counts['failures'] += 1
        raise RoutingError('all eligible models failed; retry after cooldown', 503)
