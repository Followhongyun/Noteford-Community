"""Account-bound local-session evidence, without credentials or network access.

An open session+contact database pair proves local account use, not server
authentication or connectivity. Absence of handles is deliberately inconclusive:
Windows can omit files during handle enumeration.
"""
import ntpath
import os
import posixpath
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil


def _path(value):
    return ntpath.normcase(ntpath.normpath(str(value)))


def _inventory():
    processes = []
    incomplete = False
    for process in psutil.process_iter(['name', 'cmdline'], ad_value=None):
        name = process.info.get('name')
        if name is None:
            incomplete = True
            continue
        if name.lower() not in {'weixin.exe', 'wechat.exe'}:
            continue
        if any(arg.startswith('--type=') for arg in process.info.get('cmdline') or []):
            continue
        try:
            processes.append({_path(item.path) for item in process.open_files()})
        except (psutil.Error, OSError):
            processes.append(set())
            incomplete = True
    return processes, incomplete


class LoginDetector:
    def __init__(self, inventory=None, clock=None):
        self.inventory = inventory or _inventory
        self.clock = clock or time.monotonic
        self.lock = threading.Lock()
        self.cached = None
        self.expires = 0

    def check(self, wechat_root, account):
        with self.lock:
            if self.cached is None or self.clock() >= self.expires:
                try:
                    processes, incomplete = self.inventory()
                except (psutil.Error, OSError):
                    processes, incomplete = [], True
                self.cached = processes, incomplete, datetime.now(timezone.utc).isoformat()
                self.expires = self.clock() + 10
            processes, incomplete, checked = self.cached
        root = Path(wechat_root) / account / 'db_storage'
        required = {_path(root / 'session/session.db'), _path(root / 'contact/contact.db')}
        if any(required <= files for files in processes):
            state, reason = 'active', 'account_databases_open'
        elif not processes and not incomplete:
            state, reason = 'not_running', 'wechat_not_running'
        else:
            state, reason = 'unknown', 'inspection_unavailable' if incomplete else 'account_session_not_observed'
        return {'state': state, 'reason': reason, 'checked_at': checked, 'network_online': None}


_detector = LoginDetector()


def account_login_status(wechat_root, account):
    if sys.platform == "darwin":
        return _mac_detector.check(wechat_root, account)
    if os.name != 'nt':
        return {'state': 'unknown', 'reason': 'unsupported_platform', 'checked_at': None, 'network_online': None}
    return _detector.check(wechat_root, account)


class LoginReconnect:
    """Observe registered accounts; debounce noisy handle enumeration, no new grants."""
    def __init__(self, check=account_login_status, clock=time.monotonic):
        self.check, self.clock = check, clock
        self.states = {}
        self.last_trigger = {}

    def poll(self, records):
        now = self.clock()
        connected = False
        live = set()
        for record in records:
            key = (str(record.wechat_root), record.account)
            live.add(key)
            state = self.check(record.wechat_root, record.account)['state']
            previous = self.states.get(key)
            self.states[key] = state
            if previous is not None and previous != 'active' and state == 'active':
                if now - self.last_trigger.get(key, float('-inf')) >= 60:
                    self.last_trigger[key] = now
                    connected = True
        self.states = {k: v for k, v in self.states.items() if k in live}
        self.last_trigger = {k: v for k, v in self.last_trigger.items() if k in live}
        return connected


# --------------------------------------------------------------------------
# macOS login detection
#
# The official Mac client is the Tencent Developer ID app
# 5A4RE8SF68.com.tencent.xinWeChat. The main "WeChat" process opens the active
# account's session.db + contact.db inside the sandbox container
#   ~/Library/Containers/com.tencent.xinWeChat/Data/Documents/.../xwechat_files/<wxid>/db_storage
# so an open (session.db, contact.db) pair proves which wxid is logged in -- the
# same account-bound local-session evidence the Windows detector uses, with no
# credentials or network. Read-only; never touches WeChat.
# --------------------------------------------------------------------------

_MAC_BUNDLE_ID = "com.tencent.xinWeChat"
_MAC_TEAM_ID = "5A4RE8SF68"


def _mac_posix(value) -> str:
    return posixpath.normcase(posixpath.normpath(str(value)))


def _is_mac_wechat_process(info: dict) -> bool:
    name = (info.get("name") or "")
    exe = info.get("exe") or ""
    cmdline = info.get("cmdline") or []
    # Skip Chromium-style helper subprocesses (renderer/gpu/utility, mini-program
    # runtime helpers): they do not open the account databases.
    if any(isinstance(a, str) and a.startswith("--type=") for a in cmdline):
        return False
    if name not in {"WeChat", "WeChatAppEx"}:
        # Some psutil builds report the bundle executable path but a truncated
        # name; fall back to the exe path of the official app bundle.
        if "/WeChat.app/Contents/MacOS/WeChat" not in exe:
            return False
    # Must be the official WeChat bundle (by exe path); the copy lives elsewhere.
    blob = exe + " " + " ".join(a for a in cmdline if isinstance(a, str))
    if _MAC_BUNDLE_ID not in blob and "/WeChat.app/" not in blob:
        return False
    return True


def _mac_inventory():
    """Open-file sets for each running official WeChat process + incomplete flag."""
    processes = []
    incomplete = False
    for process in psutil.process_iter(["name", "exe", "cmdline"], ad_value=None):
        info = process.info
        if info.get("name") is None and info.get("exe") is None:
            incomplete = True
            continue
        try:
            if not _is_mac_wechat_process(info):
                continue
        except (psutil.Error, OSError):
            incomplete = True
            continue
        try:
            processes.append({_mac_posix(item.path) for item in process.open_files()})
        except (psutil.Error, OSError):
            processes.append(set())
            incomplete = True
    return processes, incomplete


def _mac_account_dirs(wechat_root):
    # Reuse the capture module's account discovery (session.db + contact.db).
    from .mac_capture import _account_dirs
    return [child.name for child in _account_dirs(Path(wechat_root))]


def _mac_default_root():
    from .mac_capture import default_db_roots, select_active_root
    return select_active_root(default_db_roots())


class MacLoginDetector:
    def __init__(self, inventory=None, account_dirs=None, default_root=None, clock=None):
        self.inventory = inventory or _mac_inventory
        self.account_dirs = account_dirs or _mac_account_dirs
        self.default_root = default_root or _mac_default_root
        self.clock = clock or time.monotonic
        self.lock = threading.Lock()
        self.cached = None
        self.expires = 0

    def check(self, wechat_root=None, account=None):
        root = Path(wechat_root) if wechat_root else self.default_root()
        checked = datetime.now(timezone.utc).isoformat()
        if root is None:
            return {"state": "unknown", "reason": "wechat_root_not_found",
                    "account": None, "checked_at": checked, "network_online": None}
        with self.lock:
            if self.cached is None or self.clock() >= self.expires:
                try:
                    processes, incomplete = self.inventory()
                except (psutil.Error, OSError):
                    processes, incomplete = [], True
                self.cached = (processes, incomplete, datetime.now(timezone.utc).isoformat())
                self.expires = self.clock() + 2
            processes, incomplete, checked = self.cached

        accounts = [account] if account else self.account_dirs(root)
        active = []
        for acct in accounts:
            base = Path(root) / acct / "db_storage"
            required = {_mac_posix(base / "session/session.db"),
                        _mac_posix(base / "contact/contact.db")}
            if any(required <= files for files in processes):
                active.append(acct)
        if len(active) == 1:
            return {"state": "active", "reason": "account_databases_open",
                    "account": active[0], "checked_at": checked, "network_online": None}
        if len(active) > 1:
            return {"state": "unknown", "reason": "multiple_account_sessions",
                    "account": None, "checked_at": checked, "network_online": None}

        if not processes and not incomplete:
            return {"state": "not_running", "reason": "wechat_not_running",
                    "account": None, "checked_at": checked, "network_online": None}
        reason = "inspection_unavailable" if incomplete else "account_session_not_observed"
        return {"state": "unknown", "reason": reason,
                "account": None, "checked_at": checked, "network_online": None}


_mac_detector = MacLoginDetector()


def mac_login_status(wechat_root=None, account=None):
    return _mac_detector.check(wechat_root, account)
