"""Minimal stdio MCP model plugin. JSON-RPC only on stdout.

No resources, prompts, logging or server-initiated requests are advertised.
"""
import argparse
import json
import sys
from .client import ModelClient

VERSIONS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
TOOLS = [
    {'name': 'generate', 'description': 'Call a model through the AIMP router; auto selects a model and fails over before output. Does not execute tool calls.',
     'inputSchema': {'type': 'object', 'properties': {
         'messages': {'type': 'array', 'items': {'type': 'object'}},
         'model': {'type': 'string', 'default': 'auto'},
         'routing': {'type': 'object'}, 'tools': {'type': 'array', 'items': {'type': 'object'}},
         'max_tokens': {'type': 'integer', 'minimum': 1}}, 'required': ['messages'], 'additionalProperties': False}},
    {'name': 'list_models', 'description': 'List configured router aliases without calling a model.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'get_router_status', 'description': 'Read routing readiness, capabilities and cooldown; no model inference.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'preview_route', 'description': 'Preview model selection without making an upstream request.',
     'inputSchema': {'type': 'object', 'properties': {'messages': {'type': 'array'}, 'model': {'type': 'string'},
                    'routing': {'type': 'object'}, 'tools': {'type': 'array'}}, 'required': ['messages'], 'additionalProperties': False}},
]


def dispatch(client, request):
    method, args = request.get('method'), request.get('params', {})
    if method == 'initialize':
        return {'protocolVersion': args.get('protocolVersion') if args.get('protocolVersion') in VERSIONS else VERSIONS[0],
                'capabilities': {'tools': {}}, 'serverInfo': {'name': 'aimp-model-router', 'version': '1.0.0'}}
    if method == 'ping':
        return {}
    if method == 'tools/list':
        return {'tools': TOOLS}
    if method != 'tools/call':
        raise LookupError('method not found')
    name, arguments = args.get('name'), args.get('arguments', {})
    schema = next((x['inputSchema'] for x in TOOLS if x['name'] == name), None)
    if schema is None:
        raise ValueError('unknown tool')
    if not isinstance(arguments, dict) or set(arguments) - set(schema['properties']) or any(k not in arguments for k in schema.get('required', [])):
        raise ValueError('invalid tool arguments')
    try:
        if name == 'generate':
            result = client.generate(**arguments)
        elif name == 'list_models':
            result = client.request('/v1/models')
        elif name == 'get_router_status':
            result = client.request('/health')
        else:
            result = client.request('/routing/preview', arguments)
        return {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}]}
    except Exception:
        return {'isError': True, 'content': [{'type': 'text', 'text': 'Model router call failed; check router configuration and status.'}]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--url', default='http://127.0.0.1:8318')
    args = parser.parse_args()
    client = ModelClient(args.token_file, args.url)
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or not isinstance(request.get('method'), str):
                raise ValueError('invalid request')
            if 'id' not in request:
                continue
            response = {'jsonrpc': '2.0', 'id': request['id'], 'result': dispatch(client, request)}
        except Exception as exc:
            code = -32700 if isinstance(exc, json.JSONDecodeError) else -32601 if isinstance(exc, LookupError) else -32602
            response = {'jsonrpc': '2.0', 'id': request.get('id') if isinstance(request, dict) else None,
                        'error': {'code': code, 'message': 'Invalid request or unsupported method'}}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
