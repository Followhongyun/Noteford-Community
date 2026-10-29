"""Explicit TLS verification for the application's bounded HTTPS downloads."""
from __future__ import annotations

import ssl
import sys

import truststore


def download_context() -> ssl.SSLContext:
    # Do not load OpenSSL's default bundle or certifi as additional anchors:
    # an old bundle could restore trust removed by the operating system.
    # Keep this local to our download handlers; never replace ssl globally.
    # A context belongs to one opener: truststore temporarily changes its
    # underlying verification flags while wrapping a socket. Concurrent
    # openers must not share that mutable state.
    if sys.platform == 'darwin':
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # truststore's Linux backend still loads OpenSSL's default roots. Other
    # targets use the reviewed bundle explicitly instead of assuming that an
    # unknown system's native revocation policy already blocks the incident.
    import certifi
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=certifi.where())
    return context
