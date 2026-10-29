"""Standard-library-only TLS bootstrap for reviewed public build inputs."""
from __future__ import annotations

import hashlib
import json
import ssl
from pathlib import Path

CA_FILE = Path(__file__).resolve().parents[2] / 'desktop/security/cacert.pem'


def build_context() -> ssl.SSLContext:
    # The repository's CA input is independent of a build machine's old pip,
    # OpenSSL defaults or optional truststore installation.
    expected = json.loads(CA_FILE.with_name('ca-policy.json').read_text())['pemSha256']
    if hashlib.sha256(CA_FILE.read_bytes()).hexdigest() != expected:
        raise ValueError('Build CA bundle verification failed')
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=CA_FILE)
    return context
