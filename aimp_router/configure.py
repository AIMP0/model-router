"""Configure an upstream alias. Credentials are prompted, never CLI arguments."""
import argparse
import getpass
import json
import os
from pathlib import Path
import tempfile
from .router import Router


def write_private(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--credentials-file', required=True)
    parser.add_argument('--alias', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:8317/v1')
    parser.add_argument('--capabilities', nargs='*', choices=['tools', 'vision', 'json_schema'], default=[])
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    if args.alias not in config['targets']:
        parser.error('alias must be an existing target in the configuration')
    target = config['targets'][args.alias]
    target.update(base_url=args.base_url, model=args.model, enabled=True, capabilities=args.capabilities)
    # A separate entry per target allows direct providers as well as CLIProxyAPI.
    target['api_key_env'] = 'AIMP_MODEL_KEY_' + str(list(config['targets']).index(args.alias))
    Router(config)
    credential_path = Path(args.credentials_file)
    credentials = json.loads(credential_path.read_text(encoding='utf-8')) if credential_path.exists() else {}
    key = getpass.getpass('Upstream API key (hidden): ').strip()
    if not key:
        parser.error('API key is required')
    credentials[target['api_key_env']] = key
    write_private(credential_path, credentials)
    write_private(args.config, config)
    print('Saved. Restart aimp-model-router.service to apply.')


if __name__ == '__main__':
    main()
