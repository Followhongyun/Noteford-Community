"""Bootstrap build pip from the PSF advisory's independently pinned wheel.

The old pip's HTTPS verifier is not used to resolve or install dependencies.
Only these reviewed wheel bytes may be installed, offline, before the build.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from noteford.build_tls import build_context

# https://discuss.python.org/t/109576
VERSION = '26.2.2'
SHA256 = 'fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70'
URL = ('https://files.pythonhosted.org/packages/3f/06/'
       '3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/'
       'pip-26.2.2-py3-none-any.whl')
WHEEL = 'pip-26.2.2-py3-none-any.whl'


def fixed_pip() -> bool:
    try:
        version = importlib.metadata.version('pip')
    except importlib.metadata.PackageNotFoundError:
        return False
    # A prerelease of the fix must not satisfy the security floor.
    if not re.fullmatch(r'\d+(?:\.\d+){1,2}', version):
        return False
    parts = tuple(map(int, version.split('.')))
    return parts + (0,) * (3 - len(parts)) >= (26, 2, 2)


def verified_bytes(data: bytes) -> bytes:
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError('Build pip wheel SHA-256 mismatch; refusing installation')
    return data


def bootstrap(wheel: Path | None = None) -> None:
    if sys.prefix == sys.base_prefix:
        raise RuntimeError('Run bootstrap with the target virtual environment Python')
    if fixed_pip():
        return
    if wheel is None:
        # No secrets are sent. A valid TLS connection is still required, and
        # the advisory's hash is checked before any downloaded code executes.
        with urllib.request.urlopen(URL, timeout=60, context=build_context()) as response:
            data = verified_bytes(response.read(4 * 1024 * 1024 + 1))
    else:
        data = verified_bytes(wheel.read_bytes())
    with tempfile.TemporaryDirectory(prefix='noteford-pip-bootstrap-') as directory:
        target = Path(directory) / WHEEL
        target.write_bytes(data)
        subprocess.run([sys.executable, '-I', '-m', 'pip', '--isolated',
                        '--disable-pip-version-check', 'install', '--no-index',
                        '--no-deps', '--require-virtualenv', '--upgrade', str(target)],
                       check=True)
    if not fixed_pip():
        raise RuntimeError('Build pip did not reach the required security version')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path, help='Use an already downloaded, hash-verified wheel')
    bootstrap(parser.parse_args().wheel)
