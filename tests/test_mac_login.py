import posixpath
from pathlib import Path

import noteford.login_status as ls
from noteford.login_status import MacLoginDetector, account_login_status


def _open_set(root, acct):
    # Use host-native paths so the synthetic handles match pathlib on Windows too.
    base = Path(root) / acct / "db_storage"
    return {
        posixpath.normcase(posixpath.normpath(str(base / "session/session.db"))),
        posixpath.normcase(posixpath.normpath(str(base / "contact/contact.db"))),
        posixpath.normcase(posixpath.normpath(str(base / "message/message_0.db"))),
    }


ROOT = "/Users/u/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"


def test_active_detects_logged_in_account():
    det = MacLoginDetector(
        inventory=lambda: ([_open_set(ROOT, "wxid_abc")], False),
        account_dirs=lambda root: ["wxid_abc", "wxid_other"],
        default_root=lambda: Path(ROOT),
        clock=lambda: 0,
    )
    r = det.check()
    assert r["state"] == "active" and r["account"] == "wxid_abc"
    assert r["reason"] == "account_databases_open" and r["network_online"] is None


def test_picks_the_account_whose_dbs_are_open():
    det = MacLoginDetector(
        inventory=lambda: ([_open_set(ROOT, "wxid_second")], False),
        account_dirs=lambda root: ["wxid_first", "wxid_second"],
        default_root=lambda: Path(ROOT),
        clock=lambda: 0,
    )
    assert det.check()["account"] == "wxid_second"


def test_not_running_when_no_processes():
    det = MacLoginDetector(
        inventory=lambda: ([], False),
        account_dirs=lambda root: ["wxid_abc"],
        default_root=lambda: Path(ROOT),
        clock=lambda: 0,
    )
    r = det.check()
    assert r["state"] == "not_running" and r["account"] is None


def test_unknown_when_inspection_incomplete():
    det = MacLoginDetector(
        inventory=lambda: ([set()], True),  # a process seen but files unreadable
        account_dirs=lambda root: ["wxid_abc"],
        default_root=lambda: Path(ROOT),
        clock=lambda: 0,
    )
    r = det.check()
    assert r["state"] == "unknown" and r["reason"] == "inspection_unavailable"


def test_unknown_when_running_but_account_not_observed():
    det = MacLoginDetector(
        inventory=lambda: ([{"/some/other/file"}], False),
        account_dirs=lambda root: ["wxid_abc"],
        default_root=lambda: Path(ROOT),
        clock=lambda: 0,
    )
    r = det.check()
    assert r["state"] == "unknown" and r["reason"] == "account_session_not_observed"


def test_wechat_root_not_found():
    det = MacLoginDetector(
        inventory=lambda: ([], False),
        account_dirs=lambda root: [],
        default_root=lambda: None,
        clock=lambda: 0,
    )
    r = det.check()
    assert r["state"] == "unknown" and r["reason"] == "wechat_root_not_found"


def test_explicit_account_param_checked():
    det = MacLoginDetector(
        inventory=lambda: ([_open_set(ROOT, "wxid_abc")], False),
        account_dirs=lambda root: ["wxid_abc", "wxid_zzz"],
        default_root=lambda: Path(ROOT),
        clock=lambda: 0,
    )
    assert det.check(account="wxid_zzz")["state"] != "active"
    assert det.check(account="wxid_abc")["state"] == "active"


def test_is_mac_wechat_process_filters():
    # helper subprocess (renderer) is excluded
    assert ls._is_mac_wechat_process(
        {"name": "WeChat", "exe": "/Applications/WeChat.app/Contents/MacOS/WeChat",
         "cmdline": ["WeChat", "--type=renderer"]}
    ) is False
    # non-wechat process excluded
    assert ls._is_mac_wechat_process({"name": "Finder", "exe": "/x", "cmdline": []}) is False
    # official main process accepted
    assert ls._is_mac_wechat_process(
        {"name": "WeChat", "exe": "/Applications/WeChat.app/Contents/MacOS/WeChat", "cmdline": ["WeChat"]}
    ) is True


def test_account_login_status_dispatches_to_mac(monkeypatch):
    monkeypatch.setattr(ls.sys, "platform", "darwin")
    seen = {}

    def fake_check(root, acct):
        seen["args"] = (root, acct)
        return {"state": "active", "account": "wxid_abc", "reason": "account_databases_open",
                "checked_at": "t", "network_online": None}

    monkeypatch.setattr(ls._mac_detector, "check", fake_check)
    r = account_login_status(ROOT, "wxid_abc")
    assert r["state"] == "active" and seen["args"] == (ROOT, "wxid_abc")


def test_ambiguous_sessions_do_not_choose_an_account():
    det = MacLoginDetector(inventory=lambda: ([_open_set(ROOT, 'a'), _open_set(ROOT, 'b')], False),
                           account_dirs=lambda root: ['a', 'b'], default_root=lambda: Path(ROOT))
    assert det.check()['reason'] == 'multiple_account_sessions'
    assert det.check()['account'] is None


def test_login_change_is_detected_within_two_seconds():
    now = [0]
    active = ['a']
    det = MacLoginDetector(inventory=lambda: ([_open_set(ROOT, active[0])], False),
                           account_dirs=lambda root: ['a', 'b'], default_root=lambda: Path(ROOT), clock=lambda: now[0])
    assert det.check()['account'] == 'a'
    active[0] = 'b'
    now[0] = 2
    assert det.check()['account'] == 'b'
