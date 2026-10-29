import importlib.util
import ssl
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
import truststore

from noteford import assets, cdn_download, mac_cdn
from noteford.https_context import download_context
from noteford import https_context
from noteford import build_tls


def test_download_context_uses_native_verification_without_global_patch(monkeypatch):
    monkeypatch.setattr(https_context, 'sys', SimpleNamespace(platform='darwin'))
    original = ssl.SSLContext
    context = download_context()
    assert isinstance(context, truststore.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert ssl.SSLContext is original
    assert download_context() is not context
    # No explicit CA anchors can restore roots distrusted by the native store.
    assert context._ctx.get_ca_certs(binary_form=True) == []


@pytest.mark.skipif(sys.platform != 'darwin', reason='macOS native TLS configuration')
def test_parallel_openers_cannot_restore_disabled_native_verification(monkeypatch):
    monkeypatch.setattr(https_context, 'sys', SimpleNamespace(platform='darwin'))
    # Reproduce overlapping truststore handshakes without sockets. The native
    # configure context temporarily changes these flags; independent openers
    # must never snapshot another connection's temporary state.
    first, second = download_context(), download_context()
    import truststore._macos as native
    a, b = native._configure_context(first._ctx), native._configure_context(second._ctx)
    a.__enter__()
    b.__enter__()
    a.__exit__(None, None, None)
    b.__exit__(None, None, None)
    for context in (first, second):
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True


@pytest.mark.parametrize('platform', ['linux', 'win32'])
def test_other_platforms_load_only_explicit_reviewed_ca(monkeypatch, tmp_path, platform):
    monkeypatch.setattr(https_context, 'sys', SimpleNamespace(platform=platform))
    pem = tmp_path / 'ca.pem'
    pem.write_text('synthetic reviewed CA')
    import sys
    monkeypatch.setitem(sys.modules, 'certifi', SimpleNamespace(where=lambda: str(pem)))
    calls = []
    class Context:
        def __init__(self, protocol):
            assert protocol == ssl.PROTOCOL_TLS_CLIENT
        def load_verify_locations(self, **kwargs):
            calls.append(kwargs)
    monkeypatch.setattr(ssl, 'SSLContext', Context)
    assert isinstance(download_context(), Context)
    assert calls == [{'cafile': str(pem)}]


def test_build_context_uses_reviewed_roots_without_machine_defaults():
    import hashlib
    context = build_tls.build_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    roots = {hashlib.sha256(x).hexdigest() for x in context.get_ca_certs(binary_form=True)}
    assert len(roots) > 100
    assert '86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b' not in roots


def test_build_context_rejects_modified_ca_data(monkeypatch, tmp_path):
    import json
    pem = tmp_path / 'cacert.pem'
    pem.write_bytes(b'modified CA data')
    (tmp_path / 'ca-policy.json').write_text(json.dumps({'pemSha256': '0' * 64}))
    monkeypatch.setattr(build_tls, 'CA_FILE', pem)
    with pytest.raises(ValueError, match='Build CA bundle verification failed'):
        build_tls.build_context()


@pytest.mark.parametrize('module', [assets, cdn_download, mac_cdn])
def test_each_download_handler_keeps_proxy_and_redirect_policy(monkeypatch, module):
    captured = []

    class StopBeforeNetwork(Exception):
        pass

    def opener(*handlers):
        captured.extend(handlers)
        raise StopBeforeNetwork

    monkeypatch.setattr(urllib.request, 'build_opener', opener)
    with pytest.raises(StopBeforeNetwork):
        if module is assets:
            assets.download_avatar('http://wx.qlogo.cn/synthetic')
        elif module is cdn_download:
            cdn_download._request({}, time.monotonic() + 5)
        else:
            mac_cdn.fetch('https://emoji.qpic.cn/synthetic')
    assert next(x for x in captured if isinstance(x, urllib.request.ProxyHandler)).proxies == {}
    context = next(x for x in captured if isinstance(x, urllib.request.HTTPSHandler))._context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    redirect = next(x for x in captured if isinstance(x, urllib.request.HTTPRedirectHandler))
    assert redirect.redirect_request(None, None, None, None, None, None) is None


def bootstrap_module():
    source = Path(__file__).parents[1] / 'Scripts/bootstrap-build-pip.py'
    spec = importlib.util.spec_from_file_location('build_pip_bootstrap', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_rejects_tampered_wheel_before_running_pip(tmp_path, monkeypatch):
    module = bootstrap_module()
    monkeypatch.setattr(module.sys, 'prefix', str(tmp_path))
    monkeypatch.setattr(module, 'fixed_pip', lambda: False)
    calls = []
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: calls.append(a))
    wheel = tmp_path / module.WHEEL
    wheel.write_bytes(b'not the advisory wheel')
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        module.bootstrap(wheel)
    assert calls == []


@pytest.mark.parametrize(('version', 'safe'), [
    ('26.2.1', False), ('26.2.2rc1', False), ('26.2.2', True), ('26.3', True),
])
def test_bootstrap_security_floor(monkeypatch, version, safe):
    module = bootstrap_module()
    monkeypatch.setattr(module.importlib.metadata, 'version', lambda name: version)
    assert module.fixed_pip() is safe


def test_bootstrap_runs_verified_wheel_offline_before_accepting_upgrade(tmp_path, monkeypatch):
    module = bootstrap_module()
    monkeypatch.setattr(module.sys, 'prefix', str(tmp_path))
    states = iter([False, True])
    monkeypatch.setattr(module, 'fixed_pip', lambda: next(states))
    monkeypatch.setattr(module, 'verified_bytes', lambda data: data)
    wheel = tmp_path / module.WHEEL
    wheel.write_bytes(b'synthetic reviewed wheel')
    calls = []

    def run(command, **options):
        assert Path(command[-1]).read_bytes() == wheel.read_bytes()
        calls.append(command)

    monkeypatch.setattr(module.subprocess, 'run', run)
    module.bootstrap(wheel)
    assert len(calls) == 1
    assert {'--no-index', '--no-deps', '--isolated', '--require-virtualenv'} <= set(calls[0])
