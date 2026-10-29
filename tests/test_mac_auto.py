import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from noteford.mac_auto import (
    AutoCapture,
    AutoCaptureConfig,
    PHASE_DONE,
    PHASE_FAILED,
    PHASE_SATISFIED,
    verify_keys_against_libs,
)


def _cfg(tmp_path, account="acct"):
    return AutoCaptureConfig(
        official_app=Path("/Applications/WeChat.app"),
        state_dir=tmp_path / "state",
        db_root=tmp_path / "dbroot",
        project_root=tmp_path / "proj",
        account_prefix=account,
        scan_timeout_seconds=5.0,
    )


def _db(rel, path):
    return SimpleNamespace(relative_path=rel, path=path)


class FakeResign:
    def __init__(self):
        self.restored = 0
    def signature_text(self, app):
        return "official"


class FakeInterpose:
    pass


class FakeStore:
    def __init__(self):
        self.items = {}
    def get(self, account):
        return self.items.get(account)
    def put(self, account, payload):
        self.items[account] = payload


def _write_keys(state_dir, keys):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "keys.json").write_text(json.dumps({"schema": 1, "keys": keys}))


def test_verify_keys_against_libs_counts_only_valid(tmp_path, monkeypatch):
    import noteford.mac_auto as m
    db = _db("acct/x.db", tmp_path / "x.db")
    (tmp_path / "x.db").write_bytes(b"\x00" * 4096)
    monkeypatch.setattr(m, "verify_key", lambda key, page1, explicit_salt=None: True)
    n, verified = verify_keys_against_libs(
        [{"relative_path": "acct/x.db", "key_hex": "aa" * 32, "salt_hex": "bb" * 16}], [db]
    )
    assert n == 1 and verified == {"acct/x.db"}


def test_idempotent_skips_scan_when_keys_valid(tmp_path, monkeypatch):
    import noteford.mac_auto as m
    cfg = _cfg(tmp_path)
    _write_keys(cfg.state_dir, [{"relative_path": "acct/x.db", "key_hex": "aa" * 32, "salt_hex": "bb" * 16}])
    (tmp_path / "x.db").write_bytes(b"\x00" * 4096)
    monkeypatch.setattr(m, "verify_key", lambda *a, **k: True)
    called = {"prepare": 0, "build": 0, "capture": 0, "sync": 0, "restore": 0}

    pipe = AutoCapture(
        cfg,
        resign_actions=FakeResign(),
        interpose_actions=FakeInterpose(),
        store=FakeStore(),
        load_databases=lambda root: [_db("acct/x.db", tmp_path / "x.db")],
        prepare_copy=lambda *a, **k: called.__setitem__("prepare", called["prepare"] + 1),
        build_hook=lambda *a, **k: called.__setitem__("build", called["build"] + 1),
        capture=lambda *a, **k: called.__setitem__("capture", called["capture"] + 1),
        sync_databases=lambda *a, **k: (called.__setitem__("sync", called["sync"] + 1) or
                                        [{"relative_path": "acct/x.db", "status": "synced"}]),
        restore_original=lambda *a, **k: called.__setitem__("restore", called["restore"] + 1),
    )
    report = pipe.run()
    assert report.phase == PHASE_DONE and report.ok
    assert report.keys_already_valid and not report.needs_scan
    assert called["prepare"] == 0 and called["build"] == 0 and called["capture"] == 0
    assert called["restore"] == 0  # no copy touched -> no restore needed
    assert report.libs_synced == 1


def test_full_scan_path_calls_restore_and_syncs(tmp_path, monkeypatch):
    import noteford.mac_auto as m
    cfg = _cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    # no keys yet -> scan required
    monkeypatch.setattr(m, "verify_key", lambda *a, **k: False)
    monkeypatch.setattr(m, "shadow_app", lambda sd: tmp_path / "noexist.app")
    order = []
    result = SimpleNamespace(matched=[{"relative_path": "acct/x.db"}], error=None)
    cfg.on_account_captured = lambda account: order.append(f'account:{account}') or 'library-id'
    cfg.on_database_synced = lambda row: order.append('readable')
    def sync(*args, **kwargs):
        order.append('sync')
        row = {"relative_path": "acct/x.db", "status": "synced"}
        kwargs['on_database_synced'](row)
        return [row]

    pipe = AutoCapture(
        cfg,
        resign_actions=FakeResign(),
        interpose_actions=FakeInterpose(),
        store=FakeStore(),
        load_databases=lambda root: [_db("acct/x.db", tmp_path / "x.db")],
        prepare_copy=lambda *a, **k: order.append("prepare"),
        build_hook=lambda *a, **k: order.append("build"),
        capture=lambda *a, **k: (order.append("capture") or result),
        sync_databases=sync,
        restore_original=lambda *a, **k: (order.append("restore") or {"ok": True, "trial_removed": True}),
    )
    (tmp_path / "x.db").write_bytes(b"\x00" * 4096)
    report = pipe.run()
    assert report.ok and report.phase == PHASE_DONE
    assert order == ["prepare", "build", "capture", "account:acct", "restore", "sync", "readable"]
    assert report.library_id == 'library-id'
    assert report.official_reopened and report.copy_cleaned
    assert report.libs_synced == 1


def test_no_passphrase_reports_failure_but_still_restores(tmp_path, monkeypatch):
    import noteford.mac_auto as m
    cfg = _cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "verify_key", lambda *a, **k: False)
    monkeypatch.setattr(m, "shadow_app", lambda sd: tmp_path / "noexist.app")
    restored = []
    result = SimpleNamespace(matched=[], error="timeout")
    pipe = AutoCapture(
        cfg,
        resign_actions=FakeResign(),
        interpose_actions=FakeInterpose(),
        store=FakeStore(),
        load_databases=lambda root: [_db("acct/x.db", tmp_path / "x.db")],
        prepare_copy=lambda *a, **k: None,
        build_hook=lambda *a, **k: None,
        capture=lambda *a, **k: result,
        sync_databases=lambda *a, **k: [],
        restore_original=lambda *a, **k: (restored.append(1) or {"ok": True, "trial_removed": True}),
    )
    (tmp_path / "x.db").write_bytes(b"\x00" * 4096)
    report = pipe.run()
    assert not report.ok and report.phase == PHASE_FAILED
    assert report.needs_scan  # still needs a scan
    assert restored == [1]  # copy cleaned + official reopened despite failure
    assert report.official_reopened


def test_exception_mid_run_still_restores(tmp_path, monkeypatch):
    import noteford.mac_auto as m
    cfg = _cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "verify_key", lambda *a, **k: False)
    monkeypatch.setattr(m, "shadow_app", lambda sd: tmp_path / "noexist.app")
    restored = []

    class Boom(Exception):
        code = "hook_build_failed"

    def boom(*a, **k):
        raise Boom()

    pipe = AutoCapture(
        cfg,
        resign_actions=FakeResign(),
        interpose_actions=FakeInterpose(),
        store=FakeStore(),
        load_databases=lambda root: [_db("acct/x.db", tmp_path / "x.db")],
        prepare_copy=lambda *a, **k: None,
        build_hook=boom,
        capture=lambda *a, **k: None,
        sync_databases=lambda *a, **k: [],
        restore_original=lambda *a, **k: (restored.append(1) or {"ok": True, "trial_removed": True}),
    )
    (tmp_path / "x.db").write_bytes(b"\x00" * 4096)
    report = pipe.run()
    assert report.phase == PHASE_FAILED and report.error == "hook_build_failed"
    assert restored == [1]


def test_force_bypasses_idempotency(tmp_path, monkeypatch):
    import noteford.mac_auto as m
    cfg = _cfg(tmp_path)
    _write_keys(cfg.state_dir, [{"relative_path": "acct/x.db", "key_hex": "aa" * 32, "salt_hex": "bb" * 16}])
    (tmp_path / "x.db").write_bytes(b"\x00" * 4096)
    monkeypatch.setattr(m, "verify_key", lambda *a, **k: True)  # keys ARE valid
    monkeypatch.setattr(m, "shadow_app", lambda sd: tmp_path / "noexist.app")
    order = []
    result = SimpleNamespace(matched=[{"relative_path": "acct/x.db"}], error=None)
    pipe = AutoCapture(
        cfg,
        resign_actions=FakeResign(),
        interpose_actions=FakeInterpose(),
        store=FakeStore(),
        load_databases=lambda root: [_db("acct/x.db", tmp_path / "x.db")],
        prepare_copy=lambda *a, **k: order.append(("prepare", k.get("force"))),
        build_hook=lambda *a, **k: order.append("build"),
        capture=lambda *a, **k: (order.append("capture") or result),
        sync_databases=lambda *a, **k: (order.append("sync") or
                                        [{"relative_path": "acct/x.db", "status": "synced"}]),
        restore_original=lambda *a, **k: (order.append("restore") or {"ok": True, "trial_removed": True}),
    )
    report = pipe.run(force=True)
    # even though keys are valid, force runs the real capture chain
    assert ("prepare", True) in order and "capture" in order
    assert report.ok and report.phase == PHASE_DONE


def test_safe_sync_failure_does_not_fail_capture(tmp_path, monkeypatch):
    import noteford.mac_auto as m
    cfg = _cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "verify_key", lambda *a, **k: False)
    monkeypatch.setattr(m, "shadow_app", lambda sd: tmp_path / "noexist.app")
    result = SimpleNamespace(matched=[{"relative_path": "acct/x.db"}], error=None)

    def boom_sync(*a, **k):
        raise RuntimeError("sync blew up")

    pipe = AutoCapture(
        cfg,
        resign_actions=FakeResign(),
        interpose_actions=FakeInterpose(),
        store=FakeStore(),
        load_databases=lambda root: [_db("acct/x.db", tmp_path / "x.db")],
        prepare_copy=lambda *a, **k: None,
        build_hook=lambda *a, **k: None,
        capture=lambda *a, **k: result,
        sync_databases=boom_sync,
        restore_original=lambda *a, **k: {"ok": True, "trial_removed": True},
    )
    (tmp_path / "x.db").write_bytes(b"\x00" * 4096)
    report = pipe.run()
    assert report.ok  # capture success stands despite sync failure
    assert any(n.startswith("sync_failed:") for n in report.notes)


def test_autocapturejob_non_blocking_snapshot_and_single_run():
    import threading
    import time

    from noteford.mac_auto import AutoCaptureJob, PHASE_DONE

    job = AutoCaptureJob()
    gate = threading.Event()

    def run_fn(report, force):
        report.phase = "await_scan"
        report.needs_scan = True
        gate.wait(5)
        report.phase = PHASE_DONE
        report.needs_scan = False
        report.ok = True

    snap = job.start(run_fn)
    assert snap["started"] and snap["running"]
    # live snapshot while the worker is blocked waiting for the "scan"
    for _ in range(50):
        if job.snapshot().get("needs_scan"):
            break
        time.sleep(0.02)
    s = job.snapshot()
    assert s["running"] and s["needs_scan"] and s["phase"] == "await_scan"
    # a second start while running does not launch a duplicate capture
    s2 = job.start(run_fn)
    assert s2["started"] is False and s2["running"] is True
    gate.set()
    for _ in range(100):
        if not job.is_running():
            break
        time.sleep(0.02)
    final = job.snapshot()
    assert final["phase"] == PHASE_DONE and final["ok"] and not final["running"]


def test_autocapturejob_snapshot_before_start():
    from noteford.mac_auto import AutoCaptureJob
    assert AutoCaptureJob().snapshot() == {"running": False, "report": None}


def test_core_hmac_and_new_account_mark_keys_stale(tmp_path, monkeypatch):
    import noteford.mac_auto as m
    from noteford.mac_auto import assess_key_freshness

    session = _db("old/db_storage/session/session.db", tmp_path / "s.db")
    contact = _db("old/db_storage/contact/contact.db", tmp_path / "c.db")
    (tmp_path / "s.db").write_bytes(b"\x00" * 4096)
    (tmp_path / "c.db").write_bytes(b"\x00" * 4096)
    keys = [
        {"relative_path": "old/db_storage/session/session.db", "key_hex": "aa" * 32, "salt_hex": "bb" * 16},
        {"relative_path": "old/db_storage/contact/contact.db", "key_hex": "aa" * 32, "salt_hex": "bb" * 16},
    ]
    monkeypatch.setattr(m, "verify_key", lambda *a, **k: False)
    stale = assess_key_freshness(keys, [session, contact])
    assert stale["reason"] == "core_hmac_failed" and stale["needs_scan"] and "key_hex" not in stale
    monkeypatch.setattr(m, "verify_key", lambda *a, **k: True)
    fresh = assess_key_freshness(keys, [session, contact])
    assert fresh["fresh"] and not fresh["needs_scan"]
    new_session = _db("new/db_storage/session/session.db", tmp_path / "ns.db")
    (tmp_path / "ns.db").write_bytes(b"\x00" * 4096)
    changed = assess_key_freshness(keys, [session, contact, new_session])
    assert changed["reason"] == "new_account" and changed["needs_scan"]
    historical = assess_key_freshness(keys, [session, contact, new_session], observed_accounts=["old", "new"])
    assert historical["fresh"] and not historical["needs_scan"]
    from noteford import mac_entry
    from noteford.mac_capture import write_keys_file
    state = tmp_path / "state"
    write_keys_file(state / "keys.json", keys, observed_accounts=["old", "new"])
    monkeypatch.setattr(mac_entry, "load_core_databases", lambda _root: [session, contact, new_session])
    assert mac_entry._freshness(state, tmp_path)["fresh"]
    truly_new = _db("later/db_storage/session/session.db", tmp_path / "later.db")
    assert assess_key_freshness(keys, [session, contact, new_session, truly_new], observed_accounts=["old", "new"])["reason"] == "new_account"
    monkeypatch.setattr(m, "verify_key", lambda *a, **k: False)
    assert assess_key_freshness(keys, [session, contact, new_session], observed_accounts=["old", "new"])["reason"] == "core_hmac_failed"
    assert assess_key_freshness([], [session])["reason"] == "no_keys"
    assert assess_key_freshness([], [])["reason"] == "databases_not_found"
    assert assess_key_freshness([], [])["needs_scan"] is False


def test_arm_scan_does_not_start_capture_or_leak_secrets():
    from noteford.mac_auto import AutoCaptureJob

    job = AutoCaptureJob()
    armed = job.arm_scan("core_hmac_failed")
    assert armed["armed"] and armed["needs_scan"] and armed["phase"] == "scan_ready"
    assert armed["scan_status"] == "ready" and not job.is_running()
    blob = json.dumps(job.snapshot())
    assert "key_hex" not in blob and "passphrase" not in blob
    again = job.arm_scan("core_hmac_failed")
    assert again["armed"] is False and again["phase"] == "scan_ready"


def test_transient_retry_uses_force_and_user_errors_do_not(tmp_path, monkeypatch):
    import noteford.mac_auto as m
    from noteford.mac_auto import run_with_retries
    from noteford.mac_capture import CaptureError

    cfg = _cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "verify_key", lambda *a, **k: False)
    monkeypatch.setattr(m, "shadow_app", lambda sd: tmp_path / "missing.app")
    forces = []
    sleeps = []

    def prepare(*_a, **k):
        forces.append(bool(k.get("force")))
        if len(forces) == 1:
            raise CaptureError("copy_failed")

    result = SimpleNamespace(matched=[{"relative_path": "acct/x.db"}], error=None)
    pipe = AutoCapture(
        cfg,
        resign_actions=FakeResign(),
        interpose_actions=FakeInterpose(),
        store=FakeStore(),
        load_databases=lambda root: [_db("acct/x.db", tmp_path / "x.db")],
        prepare_copy=prepare,
        build_hook=lambda *_a, **_k: None,
        capture=lambda *_a, **_k: result,
        sync_databases=lambda *_a, **_k: [{"relative_path": "acct/x.db", "status": "synced"}],
        restore_original=lambda *_a, **_k: {"ok": True, "trial_removed": True},
    )
    (tmp_path / "x.db").write_bytes(b"\x00" * 4096)
    report = run_with_retries(pipe, force=False, sleep=lambda delay: sleeps.append(delay), max_retries=2)
    assert forces == [False, True]
    assert sleeps == [0.5]
    assert report.ok and report.phase == PHASE_DONE
    assert any(note.startswith("retry:copy_failed:") for note in report.notes)

    calls = []

    def no_scan(*_a, **_k):
        calls.append(1)
        return SimpleNamespace(matched=[], error="no_passphrase_captured")

    user = AutoCapture(
        cfg,
        resign_actions=FakeResign(),
        interpose_actions=FakeInterpose(),
        store=FakeStore(),
        load_databases=lambda root: [_db("acct/x.db", tmp_path / "x.db")],
        prepare_copy=lambda *_a, **_k: None,
        build_hook=lambda *_a, **_k: None,
        capture=no_scan,
        sync_databases=lambda *_a, **_k: [],
        restore_original=lambda *_a, **_k: {"ok": True, "trial_removed": True},
    )
    failed = run_with_retries(user, force=False, sleep=lambda delay: sleeps.append(delay), max_retries=2)
    assert calls == [1]
    assert failed.error == "no_passphrase_captured" and not failed.ok
    assert any("勿反复重试" in note for note in failed.notes)


def test_incomplete_coverage_is_not_treated_as_success(tmp_path, monkeypatch):
    import noteford.mac_auto as m

    cfg = _cfg(tmp_path)
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(m, "verify_key", lambda *a, **k: False)
    monkeypatch.setattr(m, "shadow_app", lambda sd: tmp_path / "missing.app")
    pipe = AutoCapture(
        cfg,
        resign_actions=FakeResign(),
        interpose_actions=FakeInterpose(),
        store=FakeStore(),
        load_databases=lambda root: [_db("acct/x.db", tmp_path / "x.db")],
        prepare_copy=lambda *_a, **_k: None,
        build_hook=lambda *_a, **_k: None,
        capture=lambda *_a, **_k: SimpleNamespace(matched=[{"relative_path": "acct/x.db"}], error="coverage_incomplete"),
        sync_databases=lambda *_a, **_k: [{"relative_path": "acct/x.db", "status": "synced"}],
        restore_original=lambda *_a, **_k: {"ok": True, "trial_removed": True},
    )
    (tmp_path / "x.db").write_bytes(b"\x00" * 4096)
    report = pipe.run()
    assert not report.ok and report.error == "coverage_incomplete"
    assert report.needs_scan and report.scan_status == "failed"
    assert report.libs_synced == 0


def test_restore_fallback_deletes_when_trial_removed_is_false(tmp_path, monkeypatch):
    import noteford.mac_auto as m
    import shutil

    cfg = _cfg(tmp_path)
    shadow = cfg.state_dir / "WeChat-trial.app"
    shadow.mkdir(parents=True)
    (shadow / "payload").write_text("copy", encoding="utf-8")
    monkeypatch.setattr(m, "verify_key", lambda *a, **k: False)
    monkeypatch.setattr(m, "shadow_app", lambda _sd: shadow)
    removed = []

    class Actions(FakeResign):
        def remove_tree(self, path):
            removed.append(path)
            shutil.rmtree(path, ignore_errors=True)

    pipe = AutoCapture(
        cfg,
        resign_actions=Actions(),
        interpose_actions=FakeInterpose(),
        store=FakeStore(),
        load_databases=lambda root: [_db("acct/x.db", tmp_path / "x.db")],
        prepare_copy=lambda *_a, **_k: None,
        build_hook=lambda *_a, **_k: None,
        capture=lambda *_a, **_k: SimpleNamespace(matched=[{"relative_path": "acct/x.db"}], error=None),
        sync_databases=lambda *_a, **_k: [{"relative_path": "acct/x.db", "status": "synced"}],
        restore_original=lambda *_a, **_k: {"ok": True, "trial_removed": False},
    )
    (tmp_path / "x.db").write_bytes(b"\x00" * 4096)
    report = pipe.run()
    assert removed == [shadow]
    assert report.copy_cleaned and not shadow.exists()
    assert report.scan_status == "done"


def test_auto_capture_routes_leave_windows_unchanged():
    from noteford.mac_auto import AutoCaptureJob
    from noteford.mac_entry import present_auto_capture, route_auto_capture_snapshot, route_auto_capture_start

    assert route_auto_capture_snapshot(platform="win32") == {"ok": False, "error": "windows_key_path_unchanged"}
    assert route_auto_capture_start({}, platform="win32") == {"ok": False, "error": "windows_key_path_unchanged"}
    assert route_auto_capture_snapshot(platform="linux")["error"] == "darwin_required"
    job = AutoCaptureJob()
    view = present_auto_capture(job, {"stale": True, "fresh": False, "needs_scan": True, "reason": "no_keys"})
    blob = json.dumps(view)
    assert view["phase"] == "scan_ready" and view["needs_scan"]
    assert "key_hex" not in blob and "passphrase" not in blob
    fresh = present_auto_capture(job, {"stale": False, "fresh": True, "needs_scan": False, "reason": None})
    assert fresh["keys_already_valid"] and fresh["scan_status"] == "done"


def test_restore_failure_blocks_success_and_does_not_unlink_live_copy(tmp_path):
    cfg = _cfg(tmp_path)
    shadow = cfg.state_dir / "WeChat-trial.app"
    shadow.mkdir(parents=True)
    class Running(FakeResign):
        def shadow_still_running(self, path):
            return True
        def remove_tree(self, path):
            pytest.fail("must not delete running temporary application")
    def failed_restore(*args, **kwargs):
        from noteford.mac_capture import CaptureError
        raise CaptureError("quit_trial_copy_first")
    pipe = AutoCapture(
        cfg, resign_actions=Running(), interpose_actions=FakeInterpose(), store=FakeStore(),
        load_databases=lambda root: [_db("acct/x.db", tmp_path / "x.db")],
        prepare_copy=lambda *a, **k: None, build_hook=lambda *a, **k: None,
        capture=lambda *a, **k: SimpleNamespace(matched=[{"relative_path": "acct/x.db"}], error=None),
        sync_databases=lambda *a, **k: pytest.fail("must restore before publishing readable data"),
        restore_original=failed_restore,
    )
    (tmp_path / "x.db").write_bytes(b"\0" * 4096)
    report = pipe.run()
    assert not report.ok and report.error == "wechat_restore_failed"
    assert shadow.exists() and not report.copy_cleaned
    from noteford.mac_entry import present_auto_capture
    class Job:
        def snapshot(self):
            return {"running": False, **report.public()}
    assert present_auto_capture(Job(), {"fresh": True})["error"] == "wechat_restore_failed"
