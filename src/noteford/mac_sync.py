"""Read-only archive of resigned-WeChat databases.

Live files are copied with the existing snapshot helper, decrypted with the
stored per-database key, then the encrypted WAL is merged into that copy.
A failed page HMAC means the database was rebuilt or the key is stale, and
the caller should capture keys again on the next WeChat login. This module
does not import the Windows memory scanner.
"""

from __future__ import annotations

import threading
import time as _time

import json
from pathlib import Path

from .crypto import (
    database_readable,
    decrypt_database,
    find_matching_key,
    merge_encrypted_wal,
    quick_check,
)
from .mac_capture import DB_KEY_ACCOUNT, DatabaseFile, load_databases
from .snapshot import copy_database_snapshot


def stored_key_records(store) -> list[dict]:
    raw = store.get(DB_KEY_ACCOUNT)
    if not raw:
        return []
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return []
    keys = payload.get("keys") if isinstance(payload, dict) else None
    return list(keys) if isinstance(keys, list) else []


def key_for_database(database: DatabaseFile, keys: list[dict]) -> dict | None:
    for item in keys:
        if item.get("relative_path") == database.relative_path and item.get("key_hex"):
            return {"key_hex": item["key_hex"], "salt_hex": item.get("salt_hex")}
    for item in keys:
        if item.get("salt_hex") == database.salt.hex() and item.get("key_hex"):
            return {"key_hex": item["key_hex"], "salt_hex": item.get("salt_hex")}
    return None


def sync_databases(
    db_root: Path,
    project_root: Path,
    store,
    *,
    copy_snapshot=copy_database_snapshot,
    decrypt=decrypt_database,
    merge_wal=merge_encrypted_wal,
    check=quick_check,
    readable=database_readable,
    account_prefix: str | None = None,
    on_database_synced=None,
    retries: int = 3,
    settle_seconds: float = 0.0,
) -> list[dict]:
    """Copy each database read-only, verify its key, and merge the WAL copy."""

    results = []
    keys = stored_key_records(store)
    # Make the catalog available first; message/attachment databases follow.
    databases = sorted(load_databases(db_root), key=lambda db: (0 if db.path.name in ('session.db', 'contact.db') else 1 if db.path.name.startswith(('message_', 'biz_message_')) else 2, db.relative_path))
    for database in databases:
        if account_prefix and database.relative_path.split("/", 1)[0] != account_prefix:
            continue
        before = _stamp(database.path)
        account = database.relative_path.split("/", 1)[0]
        category = database.path.parent.name
        encrypted_root = project_root / "data" / "encrypted" / account / category
        snapshot_dir = copy_snapshot(
            database.path,
            encrypted_root,
            retries=retries,
            settle_seconds=settle_seconds,
        )
        snapshot_db = snapshot_dir / database.path.name
        candidate = key_for_database(database, keys)
        row = {
            "relative_path": database.relative_path,
            "status": "recapture",
            "wal_frames": 0,
            "snapshot": snapshot_dir.name,
        }
        if candidate is not None and find_matching_key(snapshot_db, [candidate]):
            decrypted_dir = project_root / "data" / "decrypted" / account / category / snapshot_dir.name
            decrypted_dir.mkdir(parents=True, exist_ok=True)
            decrypted_db = decrypted_dir / snapshot_db.name
            key = bytes.fromhex(candidate["key_hex"])
            decrypt(snapshot_db, decrypted_db, key)
            wal = Path(str(snapshot_db) + "-wal")
            row["wal_frames"] = int(merge_wal(decrypted_db, wal, key) if wal.is_file() else 0)
            if check(decrypted_db):
                row["status"] = "synced"
                row["integrity"] = "quick_check"
            elif readable(decrypted_db):
                # fts5 shadow DBs: quick_check cannot validate them, but
                # the pages decrypt and every row reads. Treat as synced.
                row["status"] = "synced"
                row["integrity"] = "readable"
            else:
                row["status"] = "quick_check_failed"
                row["integrity"] = "failed"
        if _stamp(database.path) != before:
            row["status"] = "source_mutated"
        results.append(row)
        if row['status'] == 'synced' and on_database_synced:
            on_database_synced(row)
    return results


def needs_recapture(results: list[dict]) -> bool:
    return any(item["status"] == "recapture" for item in results)


def _stamp(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


class LoginGatedSync:
    """Single, login-gated incremental sync scheduler.

    One loop, not two: after start it waits until Mac login detection reports an
    ACTIVE WeChat login (polling, never syncing while logged out), then runs one
    incremental sync, and from that point re-runs every ``interval`` seconds. The
    schedule is anchored on the previous sync finishing (no separate boot sync,
    no t=0 countdown). If WeChat logs out mid-run the next cycle sees "not
    active" and falls back to polling until a login returns, then syncs again
    (re-anchoring). interval <= 0 disables the whole flow.

    Idempotent (``sync_once`` is itself idempotent), non-fatal (a login-check or
    sync error -- including a stale key_unavailable after a re-login as a
    different account -- is recorded and swallowed, never crashing the service).
    Stats carry no secrets.
    """

    def __init__(self, login_check, sync_once, interval_seconds, *, poll_seconds=None, clock=None, before_sync=None):
        self._login_check = login_check
        self._sync_once = sync_once
        self._before_sync = before_sync
        self.interval = float(interval_seconds)
        # While waiting for a login, re-check at most this often (bounded by the
        # interval so a tiny interval still polls quickly).
        if poll_seconds is not None:
            self.poll = float(poll_seconds)
        else:
            self.poll = min(self.interval, 30.0) if self.interval > 0 else 0.0
        self._clock = clock or _time.time
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stats = {
            "enabled": self.interval > 0,
            "interval_seconds": self.interval,
            "poll_seconds": self.poll,
            "phase": "idle",          # idle|waiting_login|synced|error|disabled|needs_rescan
            "runs": 0,
            "errors": 0,
            "last_status": None,      # "ok" | "error" | None
            "last_error": None,       # error code string, never secrets
            "last_account": None,     # wxid of the login we synced, never secrets
            "last_run_at": None,      # epoch seconds of last successful sync
            "next_run_at": None,      # epoch seconds of the next planned sync
            "waiting_reason": None,   # why we are waiting (login state/reason)
        }

    # -- internal, all non-fatal ------------------------------------------
    def _safe_login(self) -> dict:
        try:
            return self._login_check() or {}
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._stats["phase"] = "waiting_login"
                self._stats["waiting_reason"] = "login_check_error:" + getattr(exc, "code", type(exc).__name__)
            return {"state": "unknown", "reason": "login_check_error"}

    def _set_waiting(self, status: dict) -> None:
        with self._lock:
            self._stats["phase"] = "waiting_login"
            self._stats["waiting_reason"] = status.get("reason") or status.get("state") or "not_active"
            self._stats["next_run_at"] = None

    def _gate(self, account) -> dict | None:
        if self._before_sync is None:
            return None
        try:
            gate = self._before_sync(account)
        except Exception as exc:  # noqa: BLE001
            return {"stale": False, "error": getattr(exc, "code", type(exc).__name__)}
        return gate if isinstance(gate, dict) else None

    def _do_sync(self, account) -> None:
        gate = self._gate(account)
        if gate and gate.get("needs_scan"):
            # Keys are stale. The scan card calls run(force=True); do not sync
            # with a key that no longer opens the core libraries.
            with self._lock:
                self._stats["phase"] = "needs_rescan"
                self._stats["last_status"] = "needs_rescan"
                self._stats["last_error"] = gate.get("reason") or "keys_stale"
                self._stats["waiting_reason"] = "keys_stale"
                self._stats["last_account"] = account
                self._stats["last_run_at"] = self._clock()
                self._stats["next_run_at"] = self._clock() + self.interval
            return
        try:
            self._sync_once()
        except Exception as exc:  # noqa: BLE001 - never crash the scheduler
            with self._lock:
                self._stats["errors"] += 1
                self._stats["last_status"] = "error"
                self._stats["last_error"] = getattr(exc, "code", type(exc).__name__)
                self._stats["last_account"] = account
                self._stats["phase"] = "error"
                self._stats["last_run_at"] = self._clock()
                self._stats["next_run_at"] = self._clock() + self.interval
            return
        with self._lock:
            self._stats["runs"] += 1
            self._stats["last_status"] = "ok"
            self._stats["last_error"] = None
            self._stats["last_account"] = account
            self._stats["phase"] = "synced"
            self._stats["waiting_reason"] = None
            self._stats["last_run_at"] = self._clock()
            self._stats["next_run_at"] = self._clock() + self.interval

    def _loop(self) -> None:
        while not self._stop.is_set():
            status = self._safe_login()
            if status.get("state") == "active":
                self._do_sync(status.get("account"))
                # Anchor the next run on this sync finishing.
                if self._stop.wait(self.interval):
                    break
            else:
                self._set_waiting(status)
                if self._stop.wait(self.poll):
                    break

    # -- lifecycle --------------------------------------------------------
    def start(self) -> bool:
        if self.interval <= 0:
            with self._lock:
                self._stats["phase"] = "disabled"
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="login-gated-sync", daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def snapshot(self) -> dict:
        with self._lock:
            snap = dict(self._stats)
        snap["running"] = self.is_running()
        now = self._clock()
        nxt = snap.get("next_run_at")
        snap["seconds_to_next"] = max(0.0, nxt - now) if isinstance(nxt, (int, float)) else None
        return snap
