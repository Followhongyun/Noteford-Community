import http.client
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from noteford.mac_accounts import MacAccounts, MacLibraryRegistry, library_scope, request_library
from noteford.service import MultiLibraryApi, make_handler


def setup_accounts(tmp_path, monkeypatch):
    source = tmp_path / 'wechat'
    archive = tmp_path / 'archive'
    registry = MacLibraryRegistry(tmp_path / 'app', {})
    records = []
    for name in ('account_a', 'account_b'):
        for category in ('session', 'contact'):
            db = source / name / 'db_storage' / category / f'{category}.db'
            db.parent.mkdir(parents=True, exist_ok=True)
            db.write_bytes(b'test')
        (archive / name).mkdir(parents=True)
        records.append(registry.register_archive_account(archive, name))
    monkeypatch.setattr('noteford.mac_live_sync.managed_archive', lambda: archive)
    return source, archive, registry, records


def test_follow_switch_logout_manual_selection_and_restart(tmp_path, monkeypatch):
    source, archive, registry, (a, b) = setup_accounts(tmp_path, monkeypatch)
    active = {'state': 'active', 'account': 'account_a'}
    api = SimpleNamespace(registry=registry, root_config={'project_root': str(tmp_path / 'app')})
    def manager():
        return MacAccounts(api, roots=lambda: [source], login=lambda root: dict(active))
    accounts = manager()
    assert accounts.snapshot()['suggested_library_id'] is None
    assert accounts.snapshot()['suggested_library_id'] == a.id
    active['account'] = 'account_b'
    assert accounts.snapshot()['suggested_library_id'] is None
    assert accounts.snapshot()['suggested_library_id'] == b.id
    # Discovery proposes a change; the UI clears its state before selecting it.
    assert registry.current().id == a.id
    registry.switch(b.id)
    active.update(state='not_running', account=None)
    assert accounts.snapshot()['suggested_library_id'] is None
    assert registry.current().id == b.id
    accounts.configure(False)
    registry.switch(a.id)
    active.update(state='active', account='account_b')
    accounts = manager()
    assert accounts.snapshot()['follow_wechat'] is False
    assert accounts.snapshot()['suggested_library_id'] is None
    accounts.configure(True)
    assert accounts.snapshot()['suggested_library_id'] == b.id
    with pytest.raises(ValueError):
        accounts.configure('true')


def pending_connection(tmp_path, monkeypatch):
    from noteford.mac_auto import AutoCaptureReport
    source, archive, registry, records = setup_accounts(tmp_path, monkeypatch)
    registry.remove(records[1].id)
    api = SimpleNamespace(registry=registry, root_config={'project_root': str(tmp_path / 'app')},
                          _lock=threading.RLock(), _cores={}, _automatic_wake=threading.Event())
    accounts = MacAccounts(api, roots=lambda: [source], login=lambda root: {'state': 'not_running'}, state_dir=tmp_path / 'capture')
    job = SimpleNamespace(snapshot=lambda: {'running': False})
    def start(run, *, force):
        job.run, job.force = run, force
        return {'running': True}
    job.start = start
    monkeypatch.setattr('noteford.mac_auto.shared_auto_job', lambda: job)
    def capture(config):
        job.config = config
        def run(report, force):
            report.ok, report.connected_account = True, 'account_b'
        return run
    monkeypatch.setattr('noteford.mac_entry.auto_capture_run_fn', capture)
    return accounts, job, AutoCaptureReport, archive, records


def test_single_account_lists_and_syncs_only_saved_selection_without_deleting_others(tmp_path, monkeypatch):
    from noteford import service
    _, _, registry, (a, b) = setup_accounts(tmp_path, monkeypatch)
    registry.switch(b.id)
    api = object.__new__(MultiLibraryApi)
    api.root_config = {'project_root': str(tmp_path)}
    api.registry = registry
    api._enrich_library = lambda record: {'id': record.id}
    monkeypatch.setattr(service, 'sys', SimpleNamespace(platform='darwin'))
    assert api.libraries() == [{'id': b.id}]
    assert [r.id for r in api._active_records()] == [b.id]
    assert registry.get(a.id).id == a.id
    monkeypatch.setattr(service, 'sys', SimpleNamespace(platform='win32'))
    assert {r['id'] for r in api.libraries()} == {a.id, b.id}


def test_single_reconnect_cannot_change_account(tmp_path, monkeypatch):
    accounts, job, _, archive, (a, b) = pending_connection(tmp_path, monkeypatch)
    accounts.api.registry.register_archive_account(archive, b.account)
    accounts.connect(single=True)
    assert job.config.account_prefix == a.account
    with pytest.raises(ValueError, match='当前版本仅连接当前微信账号'):
        accounts.connect(b.id, single=True)


def test_first_login_opens_catalog_before_final_archive_finishes(tmp_path, monkeypatch):
    accounts, job, report_type, archive, (a, b) = pending_connection(tmp_path, monkeypatch)
    accounts.api.registry.remove(a.id)
    (archive / a.account).rmdir()
    (archive / b.account).rmdir()
    archive.rmdir()
    assert not archive.exists()
    core = SimpleNamespace(account=b.account, project_root=tmp_path/'core',
        repository=SimpleNamespace(index_path=tmp_path/'core/index.db'),
        refresh_lock=threading.Lock(), status=lambda: {'self_display_name': 'Connected'})
    accounts.api._core = lambda _: core
    indexed = []
    monkeypatch.setattr('noteford.decrypted_archive.refresh_decrypted_library', lambda **kw: indexed.append(kw))
    def capture(config):
        def run(report, force):
            assert accounts.api.registry.list() == []
            report.connected_account = b.account
            report.library_id = config.on_account_captured(b.account)
            assert (archive / b.account).is_dir()
            assert not report.readable
            config.on_database_synced({'relative_path': 'account_b/db_storage/contact/contact.db'})
            assert report.readable and accounts.api.registry.current().initialized_at
            assert accounts.api.libraries()[0]['id'] == b.id
            # A later auxiliary failure must not erase already readable data.
            report.ok, report.error = False, 'sync_failed'
        return run
    accounts.api.libraries = lambda: [r.public_dict(current=True) for r in accounts.api.registry.list()]
    monkeypatch.setattr('noteford.mac_entry.auto_capture_run_fn', capture)
    accounts.connect(single=True)
    report = report_type()
    job.run(report, True)
    assert report.readable and not report.ok
    assert indexed[0]['message_database_names'] == {'contact.db'}
    assert indexed[0]['write_archive_status'] is False


def test_add_starts_login_inside_noteford_without_publishing_discovered_accounts(tmp_path, monkeypatch):
    accounts, job, report_type, _, (a, b) = pending_connection(tmp_path, monkeypatch)
    assert [row['library_id'] for row in accounts.snapshot()['accounts']] == [a.id]
    before = accounts.api.registry.registry_path.read_bytes()
    assert accounts.connect() == {'running': True}
    assert job.force is True
    assert job.config.require_live_login is True
    assert job.config.account_prefix is None
    assert accounts.api.registry.registry_path.read_bytes() == before
    finished = []
    def finish(account):
        finished.append(account)
        return b.id, False
    monkeypatch.setattr(accounts, '_finish_connection', finish)
    report = report_type()
    job.run(report, True)
    assert finished == ['account_b']
    assert report.ok and report.library_id == b.id
    assert report.phase == 'done'
    assert 'connected_account' not in report.public()


@pytest.mark.parametrize('outcome', ['cancel', 'failed', 'unconfirmed'])
def test_failed_or_cancelled_add_does_not_publish_account(tmp_path, monkeypatch, outcome):
    accounts, job, report_type, _, _ = pending_connection(tmp_path, monkeypatch)
    if outcome != 'cancel':
        def capture(config):
            def run(report, force):
                report.ok = outcome == 'unconfirmed'
                report.error = None if report.ok else 'no_passphrase_captured'
            return run
        monkeypatch.setattr('noteford.mac_entry.auto_capture_run_fn', capture)
    before = accounts.api.registry.registry_path.read_bytes()
    accounts.connect()
    if outcome == 'cancel':
        accounts.cancel()
        assert job.config.is_cancelled()
    report = report_type()
    job.run(report, True)
    assert not report.ok and report.library_id is None
    assert accounts.api.registry.registry_path.read_bytes() == before


def test_reconnect_binds_registered_account_without_requiring_official_login(tmp_path, monkeypatch):
    accounts, job, _, _, (a, _) = pending_connection(tmp_path, monkeypatch)
    accounts.connect(a.id)
    assert job.config.account_prefix == a.account
    with pytest.raises(KeyError):
        accounts.connect('unregistered')


@pytest.mark.parametrize('outcome', ['ok', 'failed', 'partial', 'cancelled'])
def test_only_publish_account_after_first_usable_read(tmp_path, monkeypatch, outcome):
    accounts, _, _, archive, (a, b) = pending_connection(tmp_path, monkeypatch)
    refresh = {'ok': outcome != 'failed', 'complete': outcome != 'partial'}
    def read():
        if outcome == 'cancelled':
            accounts.cancel()
        return refresh
    core = SimpleNamespace(refresh=read, status=lambda: {'self_display_name': 'Second account'})
    monkeypatch.setattr('noteford.service.LocalApi', lambda config: core)
    before = accounts.api.registry.registry_path.read_bytes()
    if outcome == 'ok':
        assert accounts._finish_connection('account_b') == (b.id, False)
        added = accounts.api.registry.get(b.id)
        assert added.display_name == 'Second account'
        assert added.initialized_at and added.state == 'ready'
        assert accounts.api.registry.current().id == a.id
        assert accounts.api._cores[b.id] is core
        assert accounts.api._automatic_wake.is_set()
        accounts.api._core = lambda id: core
        assert accounts._finish_connection('account_b') == (b.id, True)
        assert len(accounts.api.registry.list()) == 2
    else:
        with pytest.raises(ValueError):
            accounts._finish_connection('account_b')
        assert accounts.api.registry.registry_path.read_bytes() == before


def test_request_binding_isolated_from_global_selection_and_other_threads(tmp_path, monkeypatch):
    monkeypatch.setattr('noteford.service.sys.platform', 'darwin')
    _, _, registry, (a, b) = setup_accounts(tmp_path, monkeypatch)
    api = object.__new__(MultiLibraryApi)
    api.root_config = {'project_root': str(tmp_path)}
    api.registry, api._lock = registry, threading.RLock()
    effects = []
    api._cores = {r.id: SimpleNamespace(
        conversations=lambda account=r.account, **kwargs: [{'username': account}],
        follow=lambda username, account=r.account: effects.append((account, username)) or {'ok': True},
        messages=lambda account=r.account, **kwargs: {'messages': [account]},
    ) for r in (a, b)}
    with library_scope(registry, a.id):
        registry.switch(b.id)
        assert api.messages(conversation='same')['messages'] == ['account_a']
        api.follow('same')
    assert effects == [('account_a', 'same')]
    assert api.messages(conversation='same')['messages'] == ['account_b']
    assert request_library.get() is None
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(api, 'test-token'))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(id, token='test-token'):
        con = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=5)
        con.request('GET', '/api/v1/conversations', headers={'X-Noteford-Library': id, 'X-Noteford-Token': token})
        response = con.getresponse()
        result = response.status, json.loads(response.read())
        con.close()
        return result
    try:
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(request, [a.id, b.id] * 10))
        assert all(code == 200 for code, _ in results)
        assert [body['data'][0]['username'] for _, body in results] == ['account_a', 'account_b'] * 10
        assert request('invalid')[0] == 404
        assert request(a.id, 'wrong')[0] == 401
    finally:
        server.shutdown()
        server.server_close()


def test_registry_updates_preserve_both_accounts_during_switches(tmp_path, monkeypatch):
    _, _, registry, (a, b) = setup_accounts(tmp_path, monkeypatch)
    def update(i):
        record = (a, b)[i % 2]
        registry.update(record.id, display_name=record.account, state='ready', initialized=True)
        registry.switch(record.id)
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(update, range(40)))
    restored = MacLibraryRegistry(registry.app_root, registry.root_config)
    assert {r.id for r in restored.list()} == {a.id, b.id}
    assert all(r.initialized_at for r in restored.list())


def test_legacy_config_does_not_reset_saved_selection_on_restart(tmp_path, monkeypatch):
    import noteford.libraries as libraries
    monkeypatch.setattr('sys.platform', 'darwin')
    source, _, registry, (a, b) = setup_accounts(tmp_path, monkeypatch)
    config = tmp_path / 'app' / 'config.local.json'
    config.write_text(json.dumps({'project_root': str(registry.app_root), 'wechat_root': a.wechat_root, 'account': a.account}))
    registry.switch(b.id)
    before = registry.registry_path.read_bytes()
    restored = libraries.registry_from_config(config)
    assert restored.current().id == b.id
    assert registry.registry_path.read_bytes() == before

@pytest.mark.parametrize('account', ['../escape', '/tmp/escape', '..', ''])
def test_first_login_rejects_unsafe_archive_account(tmp_path, monkeypatch, account):
    from noteford.mac_capture import CaptureError
    accounts, job, report_type, archive, (a, b) = pending_connection(tmp_path, monkeypatch)
    accounts.api.registry.remove(a.id)
    def capture(config):
        def run(report, force):
            config.on_account_captured(account)
        return run
    monkeypatch.setattr('noteford.mac_entry.auto_capture_run_fn', capture)
    accounts.connect(single=True)
    with pytest.raises(CaptureError, match='invalid_account'):
        job.run(report_type(), True)
    assert accounts.api.registry.list() == []
