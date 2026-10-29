import asyncio
import json
import os
import threading
import psutil
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest
from mcp import Client

from noteford.libraries import LibraryRegistry, load_root_config
from noteford.mcp_server import create_server
from noteford.mac_ai_access import direct_server
from noteford.product_workspace import WorkspaceAccess, require_active, require_source
from noteford.service import MultiLibraryApi, make_handler
from test_mcp_server import _indexed_account


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def activate(installation, root, workspace, epoch):
    write(root / '.product-workspace.json', {'version': 1, 'installation': str(installation), 'workspace': workspace})
    write(installation / 'data/runtime/product-account/active-workspace.json',
          {'version': 1, 'active': True, 'root': str(root), 'workspace': workspace, 'epoch': epoch, 'pid': os.getpid(),
           'appExecutable': psutil.Process().exe(), 'processStartedAtMs': psutil.Process().create_time() * 1000})


def owner_fixture(tmp_path):
    root = tmp_path / 'installation'
    config = root / 'config.local.json'
    source = root / 'data/decrypted'
    for name in ('session', 'contact'):
        folder = source / 'acct_owner/db_storage' / name
        folder.mkdir(parents=True)
        (folder / f'{name}.db').write_bytes(b'fixture')
    cfg = {'project_root': str(root), 'wechat_roots': []}
    write(config, cfg)
    registry = LibraryRegistry(root, cfg)
    registry.discover(source)
    library = _indexed_account(root, registry, 'acct_owner', 'OWNER-ONLY-MESSAGE')
    write(root / 'data/mcp_access.json', {'version': 1, 'clients': {'owner': {
        'enabled': True, 'library_id': library.id, 'scope': 'all_synced', 'conversations': [],
    }}})
    write(root / 'data/mac_ai_access.json', {'version': 1, 'enabled': True, 'library_id': library.id, 'scope': 'following'})
    index = registry.context(library.id).index_path
    activate(root, root, 'legacy', 'first')
    return root, config, library, index


def test_http_old_token_denied_after_logout_switch_and_relogin(tmp_path):
    root, config, library, index = owner_fixture(tmp_path)
    api = MultiLibraryApi(config)
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(api, 'old-http-token'))
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    def read(route='/api/v1/libraries'):
        req = Request(f'http://127.0.0.1:{server.server_port}{route}', headers={'X-Noteford-Token': 'old-http-token'})
        try:
            with urlopen(req) as result: return result.status, json.load(result)
        except HTTPError as error: return error.code, json.load(error)
    try:
        assert read()[1]['data'][0]['id'] == library.id
        write(root / 'data/runtime/product-account/active-workspace.json', {'version': 1, 'active': False})
        assert read()[0] == 401
        other = root / 'product-accounts/other'
        write(other / 'config.local.json', {'project_root': str(other), 'wechat_roots': []})
        activate(root, other, 'other', 'second')
        assert MultiLibraryApi(other / 'config.local.json').libraries() == []
        assert read()[0] == 401
        activate(root, root, 'legacy', 'third')
        assert read()[0] == 401  # old connection cannot inherit a renewed session
        assert MultiLibraryApi(config).libraries()[0]['id'] == library.id
        assert index.is_file()
    finally:
        server.shutdown(); server.server_close(); worker.join()


@pytest.mark.parametrize('direct', [False, True])
def test_live_mcp_session_stops_reading_on_account_switch_and_preserves_data(tmp_path, direct):
    root, config, library, index = owner_fixture(tmp_path)
    before = index.read_bytes()
    server = direct_server(config) if direct else create_server(config, 'owner')
    async def exercise():
        async with Client(server) as client:
            async def read():
                result = await client.call_tool('read_messages', {'library_id': library.id, 'conversation_id': 'group@chatroom'})
                return result.is_error, str(result)
            error, text = await read()
            assert not error and 'OWNER-ONLY-MESSAGE' in text
            other = root / 'product-accounts/other'; other.mkdir(parents=True)
            activate(root, other, 'other', 'second')
            error, text = await read()
            assert error and 'OWNER-ONLY-MESSAGE' not in text
            activate(root, root, 'legacy', 'third')
            error, text = await read()
            assert error and 'OWNER-ONLY-MESSAGE' not in text
        fresh = direct_server(config) if direct else create_server(config, 'owner')
        async with Client(fresh) as client:
            result = await client.call_tool('read_messages', {'library_id': library.id, 'conversation_id': 'group@chatroom'})
            assert not result.is_error and 'OWNER-ONLY-MESSAGE' in str(result)
    asyncio.run(exercise())
    assert index.read_bytes() == before


def test_mac_archives_capture_and_manual_import_stay_in_active_workspace(tmp_path, monkeypatch):
    from noteford.mac_resign import default_state_dir
    from noteford.mac_live_sync import managed_archive
    from noteford.platform_seam import discover_library_candidates
    root, _, _, _ = owner_fixture(tmp_path)
    other = root / 'product-accounts/other'
    write(other / 'config.local.json', {'project_root': str(other), 'wechat_roots': []})
    activate(root, other, 'other', 'new')
    monkeypatch.setenv('NOTEFORD_WORKSPACE_ROOT', str(other))
    monkeypatch.setenv('NOTEFORD_WORKSPACE_EPOCH', 'new')
    assert default_state_dir() == other / 'keycap'
    assert managed_archive() == other / 'data/decrypted'
    assert discover_library_candidates(platform='darwin') == []
    alias = tmp_path / 'alias'; alias.symlink_to(root / 'data/decrypted', target_is_directory=True)
    api = MultiLibraryApi(other / 'config.local.json')
    for source in (root / 'data/decrypted', alias, root, tmp_path):
        with pytest.raises(PermissionError): api.discover_candidates(str(source))
        with pytest.raises(PermissionError): api.discover_libraries(str(source))
    require_source(other, other / 'data/decrypted')
    activate(root, root, 'legacy', 'owner')
    with pytest.raises(PermissionError): require_source(root, other / 'data/decrypted')
    with pytest.raises(PermissionError): require_source(root, root / 'product-accounts')
    with pytest.raises(PermissionError): default_state_dir()


def test_dead_desktop_corrupt_lease_and_unmanaged_reader_fail_closed(tmp_path):
    root = tmp_path / 'root'; root.mkdir()
    old_reader = WorkspaceAccess(root)
    activate(root, root, 'legacy', 'new')
    with pytest.raises(PermissionError): old_reader.check()
    lease = root / 'data/runtime/product-account/active-workspace.json'
    value = json.loads(lease.read_text()); value['pid'] = 99999999; write(lease, value)
    with pytest.raises(PermissionError): require_active(root)
    lease.write_text('{')
    with pytest.raises(PermissionError): require_active(root)
    activate(root, root, 'legacy', 'new')
    value = json.loads(lease.read_text()); value['processStartedAtMs'] -= 60000; write(lease, value)
    with pytest.raises(PermissionError): require_active(root)


def test_separate_keychain_items_preserve_legacy_local_remote_and_account_keys(tmp_path):
    from noteford.platform_seam import MacKeychainSecretProtector, protected_data_key
    from test_keychain_pairing import MemoryKeychain
    chain = MemoryKeychain()
    protector = MacKeychainSecretProtector(runner=chain)
    legacy = tmp_path / 'legacy.key'; legacy.write_bytes(protector.wrap(b'l' * 32))
    paths = [tmp_path / account / store for account in ('owner', 'other') for store in ('mcp_local.key', 'mcp_remote.key')]
    keys = [protected_data_key(path, platform='darwin', protector=protector) for path in paths]
    assert len(set(keys)) == 4 and len(chain.items) == 5
    assert protected_data_key(legacy, platform='darwin', protector=protector) == b'l' * 32
    assert [protected_data_key(path, platform='darwin', protector=protector) for path in paths] == keys


def test_mac_http_rejects_caller_paths_before_capture_or_key_access(tmp_path, monkeypatch):
    root, _, _, _ = owner_fixture(tmp_path)
    other = root / 'product-accounts/other'
    config = other / 'config.local.json'
    write(config, {'project_root': str(other), 'wechat_roots': []})
    activate(root, other, 'other', 'second')
    def forbidden(*args, **kwargs):
        pytest.fail('capture dispatch must not receive arbitrary paths')
    monkeypatch.setattr('noteford.mac_entry.route_action', forbidden)
    monkeypatch.setattr('noteford.mac_entry.route_auto_capture_start', forbidden)
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(MultiLibraryApi(config), 'B-token'))
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    try:
        for route in ('wechat', 'auto-capture'):
            for field, value in [('project_root', root), ('state_dir', root / 'keycap'), ('db_root', root / 'data/decrypted')]:
                request = Request(f'http://127.0.0.1:{server.server_port}/api/v1/mac/{route}',
                                  data=json.dumps({'action': 'sync', field: str(value)}).encode(),
                                  headers={'X-Noteford-Token': 'B-token'}, method='POST')
                with pytest.raises(HTTPError) as error: urlopen(request)
                assert error.value.code == 403
    finally:
        server.shutdown(); server.server_close(); worker.join()


def test_mac_capture_keys_do_not_reuse_owner_keychain_item(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from noteford.mac_capture import SecurityKeychainStore, DB_KEY_ACCOUNT
    root, _, _, _ = owner_fixture(tmp_path)
    items = {DB_KEY_ACCOUNT: 'owner-secret'}
    def run(command, **kwargs):
        account = command[command.index('-a') + 1]
        if command[1] == 'find-generic-password':
            return SimpleNamespace(returncode=0 if account in items else 44, stdout=items.get(account, ''))
        items[account] = bytes.fromhex(command[command.index('-X') + 1]).decode()
        return SimpleNamespace(returncode=0)
    monkeypatch.setenv('NOTEFORD_WORKSPACE_ROOT', str(root))
    monkeypatch.setenv('NOTEFORD_WORKSPACE_EPOCH', 'first')
    owner = SecurityKeychainStore(run)
    assert owner.get(DB_KEY_ACCOUNT) == b'owner-secret'
    other = root / 'product-accounts/other'; other.mkdir(parents=True)
    activate(root, other, 'other', 'second')
    monkeypatch.setenv('NOTEFORD_WORKSPACE_ROOT', str(other))
    monkeypatch.setenv('NOTEFORD_WORKSPACE_EPOCH', 'second')
    stranger = SecurityKeychainStore(run)
    assert stranger.get(DB_KEY_ACCOUNT) is None
    stranger.put(DB_KEY_ACCOUNT, b'new-key')
    assert items[DB_KEY_ACCOUNT] == 'owner-secret'
    with pytest.raises(PermissionError): owner.get(DB_KEY_ACCOUNT)
