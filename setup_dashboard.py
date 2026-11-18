"""Initialize dashboard access independently of the model API token."""
import argparse
import getpass
import json
import os
from pathlib import Path

from aimp_router.configure import write_private
from aimp_router.dashboard import network_settings
from aimp_router.server import load_token


def initialize(runtime, network, token=None):
    runtime = Path(runtime)
    path = runtime / 'dashboard.json'
    if path.exists() or (runtime / 'dashboard-token').exists():
        raise ValueError('管理页面已初始化；请登录修改配置，不会覆盖现有令牌。')
    network = network_settings(network)
    if token is not None and (len(token) < 32 or not token.isascii() or any(c.isspace() for c in token)):
        raise ValueError('管理令牌至少需要 32 个 ASCII 字符，不能包含空白字符。')
    models = []
    if (runtime / 'config.json').exists():
        models = list(json.loads((runtime / 'config.json').read_text(encoding='utf-8-sig')).get('targets', {}))
    providers = [
        {'id': 'deepseek', 'name': 'DeepSeek', 'kind': 'deepseek',
         'models': [m for m in models if 'deepseek' in m.lower()], 'api_key': ''},
        {'id': 'codex', 'name': 'GPT / Codex', 'kind': 'codex',
         'models': [m for m in models if 'gpt' in m.lower()], 'api_key': ''},
        {'id': 'claude', 'name': 'Claude', 'kind': 'anthropic',
         'models': [m for m in models if 'claude' in m.lower()], 'api_key': ''},
        {'id': 'grok', 'name': 'Grok', 'kind': 'xai',
         'models': [m for m in models if 'grok' in m.lower()], 'api_key': ''},
    ]
    os.umask(0o077)
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    if token:
        # Exclusive creation prevents accidental reuse of a model API token.
        fd = os.open(runtime / 'dashboard-token', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(token)
    else:
        load_token(runtime / 'dashboard-token')
    write_private(path, {'network': network, 'providers': providers})


def main():
    parser = argparse.ArgumentParser(description='配置余额页面的监听 IP、端口和管理令牌')
    parser.add_argument('--runtime-dir', type=Path, default=Path(__file__).resolve().parent / 'runtime')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8319)
    parser.add_argument('--public-url', default='')
    parser.add_argument('--generate-token', action='store_true', help='generate a token into a private file without prompting')
    args = parser.parse_args()
    token = None if args.generate_token else getpass.getpass('管理令牌（至少 32 个字符；回车自动生成到本地文件）：').strip() or None
    network = {'host': args.host, 'port': args.port, 'public_url': args.public_url}
    try:
        initialize(args.runtime_dir.expanduser().resolve(), network, token)
    except ValueError as exc:
        parser.error(str(exc))
    print('管理页面已初始化；令牌位于 runtime/dashboard-token，不会显示在日志中。')
    if args.public_url:
        print('访问地址：' + args.public_url)
    print('启动：python3 -m aimp_router.dashboard --runtime-dir ' + str(args.runtime_dir))


if __name__ == '__main__':
    main()
