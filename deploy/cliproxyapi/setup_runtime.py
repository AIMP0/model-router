"""Initialize a fresh private config without putting keys on the command line."""
from pathlib import Path
import getpass
import json
import os
import secrets


def main():
    os.umask(0o077)
    root = Path(__file__).resolve().parent
    runtime = root / 'runtime'
    if runtime.exists():
        raise SystemExit('runtime already exists; refusing to overwrite existing configuration.')
    config = json.loads((root / 'config.example.json').read_text(encoding='utf-8'))
    deepseek = getpass.getpass('DeepSeek API key (empty to omit DeepSeek): ').strip()
    link_key = secrets.token_urlsafe(48)
    config['access']['api-keys'] = [link_key]
    config['oauth']['auth-dir'] = str(runtime / 'auths')
    providers = config['api-keys']['openai-compatibility']
    if deepseek:
        providers[0]['keys'][0]['api-key'] = deepseek
    else:
        providers.clear()
    runtime.mkdir(mode=0o700)
    (runtime / 'auths').mkdir(mode=0o700)
    (runtime / 'config.json').write_text(json.dumps(config, indent=2))
    (runtime / 'link-key').write_text(link_key)
    print('Created runtime/config.json and runtime/link-key. Keep runtime private.')


if __name__ == '__main__':
    main()
