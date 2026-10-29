import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from noteford import mac_live_sync as live
from noteford.decrypted_archive import _message_databases
from noteford.refresh import KeyUnavailable


@pytest.fixture
def setup_live(tmp_path, monkeypatch):
    state = tmp_path / "project" / "keycap"
    state.mkdir(parents=True)
    root = tmp_path / "wechat"
    account = "wxid_fixture_ab12"
    storage = root / account / "db_storage"
    keys = []
    for category, name in [("session", "session.db"), ("contact", "contact.db"), ("message", "message_0.db")]:
        file = storage / category / name
        file.parent.mkdir(parents=True)
        file.write_bytes(b"encrypted fixture" * 300)
        keys.append({"relative_path": f"{account}/db_storage/{category}/{name}", "key_hex": "ab" * 32})
    (state / "keys.json").write_text(json.dumps({"keys": keys}))
    monkeypatch.setattr(live, "default_state_dir", lambda: state)
    monkeypatch.setattr(live, "default_db_roots", lambda: [root])
    monkeypatch.setattr(live, "verify_key", lambda key, page: True)
    decrypted = []

    def decrypt(source, output, key):
        decrypted.append(source.name)
        con = sqlite3.connect(output)
        con.execute("CREATE TABLE messages(text)")
        con.execute("INSERT INTO messages VALUES ('initial')")
        con.commit()
        con.close()

    def merge(output, wal, key):
        con = sqlite3.connect(output)
        con.execute("INSERT INTO messages VALUES (?)", (wal.read_text(),))
        con.commit()
        con.close()
        return 1

    monkeypatch.setattr(live, "decrypt_database", decrypt)
    monkeypatch.setattr(live, "merge_encrypted_wal", merge)
    return live.managed_archive(), account, storage, decrypted


def test_wal_only_change_is_imported_and_unchanged_sources_survive_restart(setup_live):
    archive, account, storage, decrypted = setup_live
    before = {p: p.read_bytes() for p in storage.rglob("*.db")}
    assert live.refresh_managed_archive(archive, account)["changed"] == 3
    assert live.refresh_managed_archive(archive, account)["changed"] == 0
    wal = storage / "message" / "message_0.db-wal"
    wal.write_text("received after first sync")
    result = live.refresh_managed_archive(archive, account)
    assert result["changed"] == 1 and result["wal_frames"] == 1
    newest = _message_databases(archive / account)
    assert len(newest) == 1
    with sqlite3.connect(newest[0]) as con:
        assert con.execute("SELECT text FROM messages").fetchall() == [("initial",), ("received after first sync",)]
    # Cache is on disk, independent of a scheduler/process instance.
    assert live.refresh_managed_archive(archive, account)["changed"] == 0
    assert decrypted == ["contact.db", "message_0.db", "session.db"]
    assert {p: p.read_bytes() for p in storage.rglob("*.db")} == before
    assert wal.read_text() == "received after first sync"
    wal.unlink()  # Simulate WeChat checkpointing its own WAL, only in this fixture.
    assert live.refresh_managed_archive(archive, account)["changed"] == 1


def test_failed_snapshot_keeps_previous_generation_and_retries(setup_live, monkeypatch):
    archive, account, storage, _ = setup_live
    live.refresh_managed_archive(archive, account)
    old = _message_databases(archive / account)
    (storage / "message" / "message_0.db-wal").write_text("new")
    monkeypatch.setattr(live, "quick_check", lambda p: False)
    with pytest.raises(RuntimeError, match="snapshot_invalid"):
        live.refresh_managed_archive(archive, account)
    assert _message_databases(archive / account) == old
    assert not list(archive.rglob(".live-*"))
    monkeypatch.setattr(live, "quick_check", lambda p: True)
    assert live.refresh_managed_archive(archive, account)["changed"] == 1


def test_missing_key_does_not_report_success_or_modify_sources(setup_live):
    archive, account, storage, _ = setup_live
    source = storage / "message" / "message_1.db"
    source.write_bytes(b"new encrypted shard")
    with pytest.raises(KeyUnavailable):
        live.refresh_managed_archive(archive, account)
    assert source.read_bytes() == b"new encrypted shard"
    assert not list(archive.rglob("message_1.db"))


def test_live_watcher_detects_wal_changes_and_stays_quiet_when_unchanged(setup_live):
    archive, account, storage, _ = setup_live
    records = [SimpleNamespace(id="fixture", wechat_root=str(archive), account=account)]
    now = [0.0]
    watcher = live.MacSourceChanges(clock=lambda: now[0])
    assert not watcher.poll(records)
    wal = storage / "message" / "message_0.db-wal"
    wal.write_text("new")
    assert not watcher.poll(records)
    now[0] = 2.0
    assert watcher.poll(records)
    now[0] = 4.0
    assert not watcher.poll(records)
    restarted = live.MacSourceChanges(clock=lambda: now[0])
    assert not restarted.poll(records)
    wal.write_text("after restart")
    now[0] = 6.0
    assert restarted.poll(records)


def test_imported_archive_never_reads_live_wechat(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "_storage", lambda *_: pytest.fail("live source accessed"))
    assert live.refresh_managed_archive(tmp_path, "fixture") is None


def test_revoked_directory_permission_is_explicit_and_existing_archive_survives(setup_live, monkeypatch):
    archive, account, storage, _ = setup_live
    live.refresh_managed_archive(archive, account)
    before = {p: p.read_bytes() for p in archive.rglob('*.db')}
    scandir = live.os.scandir
    blocked = [True]

    def guarded_scandir(path):
        if blocked[0] and Path(path) == storage:
            raise PermissionError(1, 'Operation not permitted', str(path))
        return scandir(path)

    monkeypatch.setattr(live.os, 'scandir', guarded_scandir)
    with pytest.raises(live.SourcePermissionRequired) as error:
        live.refresh_managed_archive(archive, account)
    assert error.value.code == 'source_permission_required'
    assert {p: p.read_bytes() for p in archive.rglob('*.db')} == before
    blocked[0] = False
    (storage / 'message/message_0.db-wal').write_text('received after permission restored')
    assert live.refresh_managed_archive(archive, account)['changed'] == 1


def test_missing_source_remains_distinct_from_permission_denied(setup_live, monkeypatch):
    archive, account, _, _ = setup_live
    monkeypatch.setattr(live, 'default_db_roots', lambda: [archive.parent / 'absent'])
    with pytest.raises(FileNotFoundError):
        live.refresh_managed_archive(archive, account)


def test_desktop_scheduler_waits_for_deadline_despite_source_changes(setup_live, monkeypatch):
    from noteford import service, mac_entry
    archive, account, storage, _ = setup_live
    record = SimpleNamespace(id="fixture", wechat_root=str(archive), account=account)
    now = [0.0]
    monkeypatch.setattr(service, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(service, "time", SimpleNamespace(time=lambda: now[0], monotonic=lambda: now[0]))
    monkeypatch.setattr(mac_entry, "arm_macos_key_check", lambda: None)
    calls = []

    class Stop:
        stopped = False
        def clear(self): self.stopped = False
        def is_set(self): return self.stopped
        def wait(self, seconds):
            now[0] += seconds
            if now[0] == 1:
                (storage / "message" / "message_0.db-wal").write_text("new arrival")
            if now[0] > 300:
                self.stopped = True
            return self.stopped

    api = object.__new__(service.MultiLibraryApi)
    api.registry = SimpleNamespace(list=lambda: [record], current=lambda: record)
    api.settings = lambda: {"sync_interval_minutes": 5, "sync_interval_seconds": 300}
    api._periodic_stop = Stop()
    api._periodic_thread = None
    def refresh(**kwargs):
        calls.append((kwargs["trigger"], now[0]))
        if len(calls) == 2: api._periodic_stop.stopped = True
        return {"ok": False}
    api._core = lambda _: SimpleNamespace(refresh=refresh)
    class Thread:
        def __init__(self, target, **kwargs): self.target = target
        def start(self): self.target()
        def is_alive(self): return False
    monkeypatch.setattr(service, "threading", SimpleNamespace(Thread=Thread))
    api.start_periodic_refresh()
    assert calls == [("startup", 0), ("interval", 300)]


def test_desktop_refresh_pulls_live_source_before_indexing(monkeypatch):
    from noteford.platform_seam import refresh_host_library
    calls = []
    monkeypatch.setattr(live, "refresh_managed_archive", lambda *args: calls.append("source") or {"changed": 1})
    monkeypatch.setattr("noteford.decrypted_archive.refresh_decrypted_library", lambda **kwargs: calls.append("index") or {})
    result = refresh_host_library(platform="darwin", wechat_root=Path("fixture"), account="fixture")
    assert calls == ["source", "index"]
    assert result["live_source"] == {"changed": 1}


def test_head_image_changes_join_live_refresh_without_writing_wechat(setup_live):
    archive, account, storage, _ = setup_live
    head = storage / 'head_image/head_image.db'
    head.parent.mkdir()
    head.write_bytes(b'head image encrypted fixture' * 200)
    keyfile = live.keys_path(live.default_state_dir())
    payload = json.loads(keyfile.read_text())
    payload['keys'].append({'relative_path': f'{account}/db_storage/head_image/head_image.db', 'key_hex': 'ab' * 32})
    keyfile.write_text(json.dumps(payload))
    before = head.read_bytes()
    assert live.refresh_managed_archive(archive, account)['changed'] == 4
    wal = head.with_name('head_image.db-wal')
    wal.write_text('updated avatar')
    assert live.refresh_managed_archive(archive, account)['changed'] == 1
    assert head.read_bytes() == before
    assert wal.read_text() == 'updated avatar'


def test_unavailable_auxiliary_key_does_not_block_messages(setup_live):
    archive, account, storage, _ = setup_live
    media=storage/'message/media_0.db'
    media.write_bytes(b'encrypted voice database')
    result=live.refresh_managed_archive(archive,account)
    assert result['changed']==3
    assert result['media_errors']==[{'database':'media_0.db','error':'key_unavailable'}]
    assert media.read_bytes()==b'encrypted voice database'
