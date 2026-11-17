"""Initialize a fresh router using a private CLIProxyAPI link-key file."""
from pathlib import Path
import argparse
import json
import os
from aimp_router.server import load_token
from aimp_router.router import Router


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--link-key-file', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = Path(__file__).resolve().parent
    runtime = root / 'runtime'
    if runtime.exists():
        raise SystemExit('runtime already exists; refusing to overwrite existing configuration.')
    key = args.link_key_file.expanduser().read_text().strip()
    if len(key) < 32:
        raise SystemExit('Expected a private link key of at least 32 characters.')
    config = json.loads((root / 'router.example.json').read_text())
    Router(config)
    runtime.mkdir(mode=0o700)
    (runtime / 'config.json').write_text(json.dumps(config, indent=2))
    (runtime / 'credentials.json').write_text(json.dumps({'CLIPROXY_API_KEY': key}))
    load_token(runtime / 'client-token')
    print('Created private router runtime. Client token is separate from upstream link key.')


if __name__ == '__main__':
    main()
