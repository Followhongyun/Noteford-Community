import asyncio
import json
import sys

import pytest
from mcp import Client

from noteford.keychain_secret import add_command, marker
from noteford.libraries import LibraryRegistry
from noteford.local_pairing import build_paired_server, open_pairing
from noteford.platform_seam import MacKeychainSecretProtector, protected_data_key, secret_backend
from noteford.remote_mcp import open_provider
from test_mcp_server import _indexed_account


class Completed:
    def __init__(self, code, stdout=""):
        self.returncode = code
        self.stdout = stdout
        self.stderr = ""


class MemoryKeychain:
    def __init__(self):
        self.items = {}
        self.commands = []

    def __call__(self, command, **kwargs):
        command = list(command)
        self.commands.append(command)
        if command[1] == "add-generic-password":
            service = command[command.index("-s") + 1]
            account = command[command.index("-a") + 1]
            self.items[(service, account)] = command[command.index("-w") + 1]
            return Completed(0)
        if command[1] == "find-generic-password":
            service = command[command.index("-s") + 1]
            account = command[command.index("-a") + 1]
            password = self.items.get((service, account))
            if password is None:
                return Completed(44)
            return Completed(0, password + "\n")
        return Completed(1)


class Clock:
    now = 1_800_000_000.0

    def __call__(self):
        return self.now


def _app(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    wechat = tmp_path / "wechat"
    for name in ("session", "contact"):
        folder = wechat / "acct_a" / "db_storage" / name
        folder.mkdir(parents=True)
        (folder / f"{name}.db").write_bytes(b"fixture")
    cfg = {"project_root": str(app), "wechat_roots": [str(wechat)]}
    config = app / "config.local.json"
    config.write_text(json.dumps(cfg))
    registry = LibraryRegistry(app, cfg)
    registry.discover(wechat)
    lib = _indexed_account(app, registry, "acct_a", "LOCAL-SECRET-TEXT")
    access = app / "data" / "mcp_access.json"
    access.write_text(json.dumps({"version": 1, "clients": {
        "g1": {"enabled": True, "library_id": lib.id, "scope": "selected", "conversations": ["group@chatroom"], "label": "Codex"},
    }}))
    return config, app, lib


def _provider(chain):
    protector = MacKeychainSecretProtector(runner=chain)

    def provide(path):
        return protected_data_key(path, platform="darwin", protector=protector)

    return provide


def test_keychain_marker_roundtrip_keeps_the_key_off_disk(tmp_path):
    chain = MemoryKeychain()
    protector = MacKeychainSecretProtector(runner=chain)
    secret = b"k" * 32
    blob = protector.wrap(secret)
    assert blob == marker(protector.service, protector.account)
    assert secret not in blob and secret.hex().encode() not in blob
    assert protector.accessible == "kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly"
    assert protector.unwrap(blob) == secret
    command = chain.commands[0]
    assert command[0] == "/usr/bin/security"
    assert secret not in b"".join(part.encode() for part in command)
    assert secret.hex() in command
    key = protected_data_key(tmp_path / "mcp_local.key", platform="darwin", protector=protector)
    stored = (tmp_path / "mcp_local.key").read_bytes()
    assert key not in stored and key.hex().encode() not in stored
    assert protected_data_key(tmp_path / "mcp_local.key", platform="darwin", protector=protector) == key
    with pytest.raises(ValueError):
        protector.unwrap(b"DPAPI")


def test_windows_data_key_still_delegates_to_dpapi(tmp_path, monkeypatch):
    seen = {}

    def fake(path):
        seen["path"] = path
        return b"w" * 32

    monkeypatch.setattr("noteford.remote_mcp.dpapi_key_provider", fake)
    assert secret_backend("win32") == "dpapi"
    assert protected_data_key(tmp_path / "mcp_local.key", platform="win32") == b"w" * 32
    assert seen["path"] == tmp_path / "mcp_local.key"


def test_macos_open_pairing_stdio_handshake(tmp_path):
    config, app, lib = _app(tmp_path)
    chain = MemoryKeychain()
    provide = _provider(chain)
    clock = Clock()
    first = open_pairing(config, provide, clock)
    token = first.issue_token("g1")
    second = open_pairing(config, provide, clock)
    server, _ = build_paired_server(config, second, token, lambda: "/Applications/Codex.app/Contents/MacOS/Codex")

    async def call():
        async with Client(server) as client:
            result = await client.call_tool("read_messages", {"library_id": lib.id, "conversation_id": "group@chatroom"})
            text = json.dumps(result.structured_content or [item.text for item in result.content if hasattr(item, "text")], ensure_ascii=False)
            return result.is_error, text

    denied, text = asyncio.run(call())
    assert denied and "LOCAL-SECRET-TEXT" not in text
    request_id = second.requests()[0]["request_id"]
    second.review(request_id)
    second.approve(request_id)
    allowed, text = asyncio.run(call())
    assert allowed is False and "LOCAL-SECRET-TEXT" in text
    raw = (app / "data" / "mcp_local.store").read_bytes()
    marker_bytes = (app / "data" / "mcp_local.key").read_bytes()
    assert token.encode() not in raw and token.encode() not in marker_bytes
    assert b"remote MCP storage requires Windows DPAPI" not in marker_bytes


def test_macos_remote_store_reopens_without_dpapi(tmp_path):
    config, app, _lib = _app(tmp_path)
    chain = MemoryKeychain()
    provide = _provider(chain)
    first = open_provider(config, "https://nf.test", provide)
    code = first.new_pair_code()["code"]
    second = open_provider(config, "https://nf.test", provide)
    assert second.store.data["pair"]["digest"] == first.store.digest(code)
    raw = (app / "data" / "mcp_remote.store").read_bytes()
    marker_bytes = (app / "data" / "mcp_remote.key").read_bytes()
    assert code.encode() not in raw and code.encode() not in marker_bytes


def test_add_command_passes_hex_to_security_not_a_shell():
    secret = bytes(range(32))
    command = add_command("com.followhongyun.noteford.community", "noteford-data-key", secret)
    assert command[:2] == ["/usr/bin/security", "add-generic-password"]
    assert "sh" not in command and "bash" not in command
    assert secret.hex() in command
    assert not any(part.encode("utf-8", "surrogateescape") == secret for part in command)


def test_keychain_denial_does_not_retry_with_another_identity(monkeypatch):
    from types import SimpleNamespace
    from noteford import platform_seam, keychain_secret
    monkeypatch.setattr(platform_seam, 'sys', SimpleNamespace(platform='darwin'))
    seen = []
    def denied(*args, **kwargs):
        seen.append(kwargs['allow_ui'])
        raise OSError('keychain_failed')
    monkeypatch.setattr(keychain_secret, 'native_get', denied)
    monkeypatch.setattr(keychain_secret, 'cli_get', lambda *a: pytest.fail('must not retry via security'))
    with pytest.raises(OSError):
        MacKeychainSecretProtector(allow_interaction=False)._get()
    assert seen == [False]


def test_background_admin_denial_is_structured_and_never_interactive(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from noteford import local_pairing, platform_seam
    monkeypatch.setattr(local_pairing, 'sys', SimpleNamespace(platform='darwin', stdout=sys.stdout))
    (tmp_path/'data').mkdir()
    (tmp_path/'data/mcp_local.store').write_bytes(b'existing encrypted store')
    monkeypatch.setattr(local_pairing, 'load_root_config', lambda _: {'project_root': str(tmp_path)})
    def key(path, *, protector):
        assert protector.allow_interaction is False
        raise OSError('private diagnostic must not escape')
    monkeypatch.setattr(platform_seam, 'protected_data_key', key)
    monkeypatch.setattr(local_pairing, 'open_pairing', lambda config, key_provider: key_provider(tmp_path/'key'))
    assert local_pairing.run_admin(['--config',str(tmp_path/'config'), '--noninteractive-keychain','requests']) == 2
    assert json.loads(capsys.readouterr().out) == {'ok': False, 'error': 'keychain_access_required'}


def test_background_check_without_pairings_does_not_open_keychain(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from noteford import local_pairing
    monkeypatch.setattr(local_pairing, 'sys', SimpleNamespace(platform='darwin', stdout=sys.stdout))
    monkeypatch.setattr(local_pairing, 'load_root_config', lambda _: {'project_root': str(tmp_path)})
    monkeypatch.setattr(local_pairing, 'open_pairing', lambda *a, **kw: pytest.fail('no credentials needed'))
    assert local_pairing.run_admin(['--config',str(tmp_path/'config'), '--noninteractive-keychain','requests']) == 0
    assert json.loads(capsys.readouterr().out) == {'ok': True, 'result': []}


def test_native_interaction_flag_restored_after_error():
    from types import SimpleNamespace
    from noteford.keychain_secret import _user_interaction
    class Call:
        def __init__(self, run): self.run = run
        def __call__(self, *args): return self.run(*args)
    seen = []
    def get(pointer): pointer._obj.value = 1; return 0
    security = SimpleNamespace(SecKeychainGetUserInteractionAllowed=Call(get),
        SecKeychainSetUserInteractionAllowed=Call(lambda value: seen.append(int(value)) or 0))
    with pytest.raises(ValueError):
        with _user_interaction(security, False):
            assert seen == [0]
            raise ValueError('denied')
    assert seen == [0, 1]


def test_linux_host_does_not_pretend_to_have_dpapi():
    if sys.platform in {"win32", "darwin"}:
        pytest.skip("host dispatch is covered on that platform")
    with pytest.raises(NotImplementedError):
        protected_data_key("/tmp/noteford-not-used.key")
