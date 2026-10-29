import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import noteford.mac_entry as mac_entry
from noteford.mac_auto import PHASE_DONE


@pytest.fixture
def server(tmp_path, monkeypatch):
    # Never run a real capture from a test: replace the run_fn + config builder.
    monkeypatch.setattr(mac_entry, "build_auto_config", lambda **kw: object())

    def fake_run_fn(config):
        def _run(report, force):
            report.phase = "await_scan"
            report.needs_scan = True
            time.sleep(0.05)
            report.phase = PHASE_DONE
            report.needs_scan = False
            report.ok = True
            report.libs_synced = 3
        return _run

    monkeypatch.setattr(mac_entry, "auto_capture_run_fn", fake_run_fn)

    token_file = tmp_path / "token.json"
    srv = mac_entry.build_server("127.0.0.1", 0, token_file, tmp_path / "state", Path("/Applications/WeChat.app"))
    port = srv.server_address[1]
    token = json.loads(token_file.read_text())["token"]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield port, token
    srv.shutdown()


def _req(port, path, *, method="GET", token=None, body=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("X-Noteford-Token", token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_health_needs_no_auth(server):
    port, _ = server
    status, body = _req(port, "/api/v1/health")
    assert status == 200 and body["ok"]


def test_auto_capture_requires_token(server):
    port, _ = server
    status, body = _req(port, "/api/v1/mac/auto-capture", method="POST", body={})
    assert status == 401 and body["error"] == "unauthorized"
    status, body = _req(port, "/api/v1/mac/auto-capture", method="GET")
    assert status == 401


def test_auto_capture_start_is_non_blocking_then_poll(server):
    port, token = server
    # POST returns immediately (does not block for the whole scan window)
    t0 = time.monotonic()
    status, body = _req(port, "/api/v1/mac/auto-capture", method="POST", token=token, body={"force": True})
    assert status == 200 and body["started"] and body["running"]
    assert time.monotonic() - t0 < 2.0  # returned fast, not blocked
    # poll until the background job finishes
    final = None
    for _ in range(100):
        status, snap = _req(port, "/api/v1/mac/auto-capture", method="GET", token=token)
        assert status == 200
        if not snap["running"]:
            final = snap
            break
        time.sleep(0.05)
    assert final is not None
    assert final["phase"] == PHASE_DONE and final["ok"] and final["libs_synced"] == 3
    # no secret material leaks: every value is a count/flag/str, never key hex
    import re
    def _no_hex(v):
        if isinstance(v, str):
            assert not re.fullmatch(r"[0-9a-fA-F]{32,}", v)
        elif isinstance(v, (list, dict)):
            for x in (v.values() if isinstance(v, dict) else v):
                _no_hex(x)
    for v in final.values():
        _no_hex(v)
    assert "libs_key_verified" in final  # counts are fine


def test_second_post_does_not_start_duplicate(server):
    port, token = server
    s1, b1 = _req(port, "/api/v1/mac/auto-capture", method="POST", token=token, body={})
    assert b1["started"]
    s2, b2 = _req(port, "/api/v1/mac/auto-capture", method="POST", token=token, body={})
    # while the first is still running, the second must not start a duplicate
    if b2.get("running"):
        assert b2["started"] is False


def test_sync_scheduler_route_reports_disabled_by_default(tmp_path):
    # build a server with the scheduler disabled (interval 0): the route must
    # report it without ever running a real sync.
    token_file = tmp_path / "t.json"
    srv = mac_entry.build_server(
        "127.0.0.1", 0, token_file, tmp_path / "state", Path("/Applications/WeChat.app"),
        sync_interval=0,
    )
    port = srv.server_address[1]
    token = json.loads(token_file.read_text())["token"]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _req(port, "/api/v1/mac/sync-scheduler", method="GET", token=token)
        assert status == 200 and body["ok"]
        assert body["enabled"] is False and body["running"] is False
        assert body["runs"] == 0
        # needs auth
        s2, _ = _req(port, "/api/v1/mac/sync-scheduler", method="GET")
        assert s2 == 401
    finally:
        srv.shutdown()


def test_sync_scheduler_route_reports_unified_scheduler_shape(tmp_path):
    token_file = tmp_path / "t.json"
    srv = mac_entry.build_server(
        "127.0.0.1", 0, token_file, tmp_path / "state", Path("/Applications/WeChat.app"),
        sync_interval=0,
    )
    port = srv.server_address[1]
    token = json.loads(token_file.read_text())["token"]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _req(port, "/api/v1/mac/sync-scheduler", method="GET", token=token)
        assert status == 200 and body["ok"]
        # single login-gated scheduler shape, no separate boot_sync path
        assert "boot_sync" not in body
        for field in ("enabled", "phase", "runs", "errors", "last_account", "next_run_at", "seconds_to_next"):
            assert field in body
        # disabled by default in this test (interval 0)
        assert body["enabled"] is False and body["phase"] in ("idle", "disabled")
    finally:
        srv.shutdown()
