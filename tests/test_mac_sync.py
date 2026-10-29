import sqlite3
from pathlib import Path

from noteford.crypto import database_readable, quick_check


def test_initial_decryption_publishes_catalog_before_messages_and_failed_auxiliary(tmp_path, monkeypatch):
    import json
    import shutil
    from types import SimpleNamespace
    from noteford import mac_sync
    from noteford.mac_capture import DatabaseFile
    sources = []
    for name in ('emoticon.db', 'message_0.db', 'session.db', 'contact.db'):
        path = tmp_path / name
        path.write_bytes(b'fixture')
        sources.append(DatabaseFile(f'acct/db_storage/test/{name}', path, b'', b''))
    monkeypatch.setattr(mac_sync, 'load_databases', lambda root: sources)
    monkeypatch.setattr(mac_sync, 'find_matching_key', lambda *args: True)
    store = SimpleNamespace(get=lambda _: json.dumps({'schema': 1, 'keys': [
        {'relative_path': db.relative_path, 'key_hex': 'aa' * 32, 'salt_hex': 'bb' * 16} for db in sources
    ]}).encode())
    def snapshot(path, destination, **kwargs):
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination / path.name)
        return destination
    published = []
    def publish(row):
        published.append(Path(row['relative_path']).name)
        if published == ['contact.db', 'session.db']:
            assert not list((tmp_path / 'project/data/decrypted').rglob('message_0.db'))
    rows = mac_sync.sync_databases(tmp_path, tmp_path / 'project', store,
        copy_snapshot=snapshot, decrypt=lambda src, dst, key: shutil.copyfile(src, dst),
        check=lambda path: path.name != 'emoticon.db', readable=lambda path: False,
        on_database_synced=publish)
    assert published == ['contact.db', 'session.db', 'message_0.db']
    assert rows[-1]['status'] == 'quick_check_failed'


def _make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE t(x)")
    con.execute("INSERT INTO t VALUES (1)")
    con.commit()
    con.close()


def test_database_readable_true_for_valid_db(tmp_path):
    db = tmp_path / "ok.db"
    _make_db(db)
    assert quick_check(db) is True
    assert database_readable(db) is True


def test_database_readable_false_for_garbage(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a database" * 500)
    assert quick_check(bad) is False
    assert database_readable(bad) is False


def test_database_readable_accepts_fts5_vtable_db(tmp_path):
    # A DB whose only content is an fts5 virtual table: PRAGMA quick_check runs
    # the fts5 integrity verifier (the exact path that errors on WeChat's custom
    # fts configs). database_readable must still accept it because the schema is
    # present and the file opens cleanly.
    db = tmp_path / "fts.db"
    con = sqlite3.connect(db)
    con.execute("CREATE VIRTUAL TABLE docs USING fts5(body)")
    con.execute("INSERT INTO docs(body) VALUES ('hello world')")
    con.commit()
    con.close()
    assert database_readable(db) is True


def test_sync_marks_unverifiable_but_readable_as_synced(tmp_path, monkeypatch):
    import noteford.mac_sync as mac_sync
    from noteford.mac_capture import DatabaseFile

    db = tmp_path / "source.db"
    db.write_bytes(b"\x00" * 4096)

    fake = DatabaseFile(
        relative_path="acct/db_storage/contact/contact_fts.db",
        path=db,
        page1=b"\x00" * 4096,
        salt=b"\x00" * 16,
    )
    monkeypatch.setattr(mac_sync, "load_databases", lambda root: [fake])

    class Store:
        def get(self, account):
            import json
            return json.dumps({"schema": 1, "keys": [
                {"relative_path": fake.relative_path, "key_hex": "aa" * 32, "salt_hex": "bb" * 16}
            ]}).encode()

    def copy_snapshot(database, dest, **kw):
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / database.name
        out.write_bytes(database.read_bytes())
        return dest

    rows = mac_sync.sync_databases(
        tmp_path, tmp_path / "proj", Store(),
        copy_snapshot=copy_snapshot,
        decrypt=lambda src, dst, key: dst.write_bytes(b"x"),
        merge_wal=lambda db, wal, key: 0,
        check=lambda p: False,          # quick_check fails (simulate fts5)
        readable=lambda p: True,        # but the DB is readable
    )
    # patch find_matching_key so the key is accepted
    assert rows[0]["status"] in {"synced", "recapture"}


def test_sync_readable_fallback_sets_integrity(tmp_path, monkeypatch):
    import noteford.mac_sync as mac_sync
    from noteford.mac_capture import DatabaseFile

    db = tmp_path / "source.db"
    db.write_bytes(b"\x00" * 4096)
    fake = DatabaseFile(
        relative_path="acct/db_storage/contact/contact_fts.db",
        path=db, page1=b"\x00" * 4096, salt=b"\x00" * 16,
    )
    monkeypatch.setattr(mac_sync, "load_databases", lambda root: [fake])
    monkeypatch.setattr(mac_sync, "find_matching_key", lambda snap, cands: (b"k", "fp"))

    import json

    class Store:
        def get(self, account):
            return json.dumps({"schema": 1, "keys": [
                {"relative_path": fake.relative_path, "key_hex": "aa" * 32, "salt_hex": "bb" * 16}
            ]}).encode()

    def copy_snapshot(database, dest, **kw):
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / database.name
        out.write_bytes(database.read_bytes())
        return dest

    rows = mac_sync.sync_databases(
        tmp_path, tmp_path / "proj", Store(),
        copy_snapshot=copy_snapshot,
        decrypt=lambda src, dst, key: dst.write_bytes(b"x"),
        merge_wal=lambda d, w, k: 0,
        check=lambda p: False,
        readable=lambda p: True,
    )
    assert rows[0]["status"] == "synced"
    assert rows[0]["integrity"] == "readable"


# ---------------------------------------------------------------------------
# LoginGatedSync: single login-gated scheduler (replaces PeriodicSync + boot sync)
# ---------------------------------------------------------------------------

def test_login_gated_sync_disabled_when_interval_non_positive():
    from noteford.mac_sync import LoginGatedSync
    calls = []
    sched = LoginGatedSync(lambda: {"state": "active"}, lambda: calls.append(1), 0)
    assert sched.start() is False
    assert sched.is_running() is False
    snap = sched.snapshot()
    assert snap["enabled"] is False and snap["running"] is False
    assert snap["phase"] == "disabled" and snap["runs"] == 0
    assert calls == []


def test_login_gated_sync_waits_for_login_then_syncs_then_schedules():
    import threading
    import time
    from noteford.mac_sync import LoginGatedSync

    state = {"logged_in": False}
    synced = {"n": 0}
    first = threading.Event()

    def login_check():
        return {"state": "active", "account": "wxid_abc"} if state["logged_in"] else {
            "state": "not_running", "reason": "wechat_not_running"}

    def sync_once():
        synced["n"] += 1
        first.set()

    # tiny interval + poll so the test runs fast
    sched = LoginGatedSync(login_check, sync_once, 0.05, poll_seconds=0.01)
    assert sched.start() is True
    # while logged out: it polls, never syncs
    time.sleep(0.08)
    assert synced["n"] == 0
    snap = sched.snapshot()
    assert snap["phase"] == "waiting_login" and snap["waiting_reason"] == "wechat_not_running"
    # now log in -> it syncs, records account + next_run_at, keeps cycling
    state["logged_in"] = True
    assert first.wait(2.0)
    time.sleep(0.12)  # let at least one more interval tick happen
    sched.stop()
    assert synced["n"] >= 2  # anchored cadence after first sync
    snap = sched.snapshot()
    assert snap["last_account"] == "wxid_abc"
    assert snap["last_status"] == "ok" and snap["runs"] >= 2
    assert snap["next_run_at"] is not None


def test_login_gated_sync_skips_when_keys_need_rescan():
    import time
    from noteford.mac_sync import LoginGatedSync

    calls = []

    def before_sync(_account):
        return {"needs_scan": True, "reason": "core_hmac_failed"}

    sched = LoginGatedSync(
        lambda: {"state": "active", "account": "wxid_x"},
        lambda: calls.append(1),
        0.05,
        poll_seconds=0.01,
        before_sync=before_sync,
    )
    assert sched.start() is True
    time.sleep(0.12)
    sched.stop()
    assert calls == []
    snap = sched.snapshot()
    assert snap["phase"] == "needs_rescan"
    assert snap["last_error"] == "core_hmac_failed"
    assert snap["errors"] == 0


def test_login_gated_sync_logout_falls_back_to_waiting():
    import time
    from noteford.mac_sync import LoginGatedSync
    state = {"logged_in": True}
    synced = {"n": 0}

    def login_check():
        return {"state": "active", "account": "wxid_abc"} if state["logged_in"] else {
            "state": "not_running", "reason": "wechat_not_running"}

    sched = LoginGatedSync(login_check, lambda: synced.__setitem__("n", synced["n"] + 1), 0.05, poll_seconds=0.01)
    sched.start()
    time.sleep(0.08)
    assert synced["n"] >= 1
    # log out: syncing must stop, scheduler returns to waiting_login
    state["logged_in"] = False
    time.sleep(0.12)
    before = synced["n"]
    time.sleep(0.1)
    sched.stop()
    assert synced["n"] == before  # no syncs while logged out
    assert sched.snapshot()["phase"] == "waiting_login"


def test_login_gated_sync_sync_error_is_non_fatal():
    import time
    from noteford.mac_sync import LoginGatedSync

    class Boom(Exception):
        code = "key_unavailable"

    def boom():
        raise Boom()

    sched = LoginGatedSync(lambda: {"state": "active", "account": "wxid_x"}, boom, 0.05, poll_seconds=0.01)
    sched.start()
    time.sleep(0.12)
    sched.stop()
    snap = sched.snapshot()
    assert snap["errors"] >= 1 and snap["last_status"] == "error"
    assert snap["last_error"] == "key_unavailable" and snap["phase"] == "error"


def test_login_gated_sync_login_check_error_is_non_fatal():
    import time
    from noteford.mac_sync import LoginGatedSync

    def boom():
        raise RuntimeError("bad")

    synced = {"n": 0}
    sched = LoginGatedSync(boom, lambda: synced.__setitem__("n", synced["n"] + 1), 0.05, poll_seconds=0.01)
    sched.start()
    time.sleep(0.08)
    sched.stop()
    assert synced["n"] == 0  # never synced because login never confirmed
    assert sched.snapshot()["phase"] == "waiting_login"
