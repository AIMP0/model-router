"""Download the exact deployed upstream release; verify its pinned SHA256."""
from pathlib import Path
import hashlib
import io
import platform
import tarfile
import urllib.request

VERSION = '8.0.73'
ASSET = 'CLIProxyAPI_8.0.73_linux_amd64.tar.gz'
SHA256 = '4c81e8c33c93b153d8013e4f631fc6cdd27325f950acb65a90156a4543bd74af'


def main():
    if platform.system() != 'Linux' or platform.machine() not in ('x86_64', 'amd64'):
        raise SystemExit('This deployment is pinned to Linux x86_64.')
    root = Path(__file__).resolve().parent
    url = f'https://github.com/router-for-me/CLIProxyAPI/releases/download/v{VERSION}/{ASSET}'
    with urllib.request.urlopen(url, timeout=120) as response:
        raw = response.read()
    if hashlib.sha256(raw).hexdigest() != SHA256:
        raise RuntimeError('Release checksum mismatch')
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        matches = [m for m in archive if m.isfile() and Path(m.name).name == 'cli-proxy-api']
        if len(matches) != 1:
            raise RuntimeError('Unexpected release archive')
        binary = root / 'bin/cli-proxy-api'
        binary.parent.mkdir(exist_ok=True)
        temporary = binary.with_suffix('.new')
        temporary.write_bytes(archive.extractfile(matches[0]).read())
        temporary.chmod(0o700)
        temporary.replace(binary)
    print('CLIProxyAPI installed; SHA256 verified. Services were not started.')


if __name__ == '__main__':
    main()
