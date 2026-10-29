"""Fully-automatic, app-callable WeChat key capture for Noteford on macOS.

Goal: after installing Noteford the user scans the WeChat QR exactly once and
everything else happens in the background, with the official WeChat never
touched. This module wraps the manual capture steps behind one idempotent,
retry-safe, self-cleaning entry point:

    copy the app -> merge-resign the copy -> build + ad-hoc sign the interpose
    hook -> launch the copy via subprocess.Popen -> user scans once -> hook
    captures the PBKDF passphrase -> derive every DB key offline -> page-1 HMAC
    verify -> commit verified keys to Keychain -> decrypt/snapshot the libraries ->
    delete the copy -> reopen the official app.

Hard guarantees (enforced by the layers this orchestrates):
  * The official /Applications/WeChat.app signature is NEVER modified. The
    official app is only quit (menu quit) and relaunched by exact path.
  * The copy is launched with subprocess.Popen directly. NEVER nohup or any
    other wrapper that strips DYLD_* across exec (that silently kills the hook).
  * On every outcome -- success, timeout, or exception -- the copy is deleted
    and the official app is reopened, in a finally block.
  * Idempotent: if Keychain keys already verify against the current libraries the
    run returns immediately, with no scan and no copy.
  * Secrets (passphrase, derived keys) never enter logs or return values. Only
    counts and phase names are reported.

This module is the single seam an app front-end (or the `serve` HTTP route)
calls. It takes injectable collaborators so it is unit-testable without a Mac
GUI or a real QR scan.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .crypto import verify_key
from .mac_capture import CaptureConfig, CaptureError, load_databases
from .mac_interpose import build_hook as _build_hook
from .mac_interpose import capture_login_interpose as _capture
from .mac_resign import copy_is_agent
from .mac_resign import prepare_copy as _prepare_copy
from .mac_resign import restore_original as _restore_original
from .mac_resign import shadow_app
from .mac_sync import sync_databases as _sync_databases

# Phase names (also the resume points for a retry).
PHASE_IDLE = "idle"
PHASE_PREFLIGHT = "preflight"
PHASE_SATISFIED = "already_satisfied"
PHASE_PREPARE = "prepare_copy"
PHASE_BUILD_HOOK = "build_hook"
PHASE_AWAIT_SCAN = "await_scan"
PHASE_SCAN_READY = "scan_ready"
PHASE_CAPTURED = "captured"
PHASE_DERIVE = "derive_verify"
PHASE_SYNC = "sync"
PHASE_DONE = "done"
PHASE_FAILED = "failed"

# Infrastructure failures: retry a bounded number of times, then give up.
# User-action failures are not in this set; retrying those would re-prompt
# and kick the official WeChat offline again.
TRANSIENT_ERRORS = frozenset({
    "copy_failed",
    "codesign_failed",
    "codesign_verify_failed",
    "entitlements_export_failed",
    "timeout",
})
USER_ACTION_ERRORS = frozenset({
    "no_passphrase_captured",
    "coverage_incomplete",
    "wrong_account",
    "cancelled",
})
MAX_TRANSIENT_RETRIES = 2
CORE_DB_SUFFIXES = ("session/session.db", "contact/contact.db")


@dataclass
class AutoCaptureConfig:
    official_app: Path
    state_dir: Path
    db_root: Path
    project_root: Path
    account_prefix: str | None = None  # bind capture and sync to one login
    scan_timeout_seconds: float = 600.0
    require_live_login: bool = False
    is_cancelled: Callable[[], bool] | None = None
    on_account_captured: Callable[[str], str] | None = None
    on_database_synced: Callable[[dict], None] | None = None


@dataclass
class AutoCaptureReport:
    ok: bool = False
    connected_account: str | None = None
    library_id: str | None = None
    already_added: bool = False
    readable: bool = False
    phase: str = PHASE_IDLE
    error: str | None = None
    needs_scan: bool = False
    keys_already_valid: bool = False
    libs_total: int = 0
    libs_key_verified: int = 0
    libs_synced: int = 0
    official_reopened: bool = False
    copy_cleaned: bool = False
    scan_status: str = ""  # ready|waiting|captured|verifying|done|failed|blocked
    notes: list[str] = field(default_factory=list)

    def public(self) -> dict:
        """Secret-free dict safe to log or return to the app."""
        return {
            "ok": self.ok,
            "library_id": self.library_id,
            "already_added": self.already_added,
            "readable": self.readable,
            "phase": self.phase,
            "error": self.error,
            "needs_scan": self.needs_scan,
            "keys_already_valid": self.keys_already_valid,
            "libs_total": self.libs_total,
            "libs_key_verified": self.libs_key_verified,
            "libs_synced": self.libs_synced,
            "official_reopened": self.official_reopened,
            "copy_cleaned": self.copy_cleaned,
            "scan_status": self.scan_status,
            "notes": list(self.notes),
        }


def keys_file_path(state_dir: Path) -> Path:
    return state_dir / "keys.json"


def load_keys(state_dir: Path, store=None) -> list[dict]:
    from .mac_key_storage import database_payload, migrate_workspace_caches, cleanup_stale_capture_files
    result = database_payload(keys_file_path(state_dir), store)['keys']
    migrate_workspace_caches(state_dir.parent, store=store)
    cleanup_stale_capture_files(state_dir)
    return result


def verify_keys_against_libs(keys: list[dict], databases) -> tuple[int, set[str]]:
    """Count how many live databases a stored key currently verifies (page-1
    HMAC). Returns (verified_count, set_of_verified_relative_paths)."""
    by_rel = {k.get("relative_path"): k for k in keys if k.get("key_hex")}
    verified: set[str] = set()
    for db in databases:
        item = by_rel.get(db.relative_path)
        if not item:
            continue
        key = bytes.fromhex(item["key_hex"])
        salt = bytes.fromhex(item["salt_hex"]) if item.get("salt_hex") else None
        with db.path.open("rb") as handle:
            page1 = handle.read(4096)
        if verify_key(key, page1, explicit_salt=salt):
            verified.add(db.relative_path)
    return len(verified), verified


def is_core_database(relative_path: str) -> bool:
    return any(str(relative_path).endswith(suffix) for suffix in CORE_DB_SUFFIXES)


def assess_key_freshness(keys: list[dict], databases, *, observed_accounts=None) -> dict:
    """Lightweight stale-key signal. Secret-free.

    A core library (session.db / contact.db) whose page-1 HMAC fails means the
    key is stale. A new account directory that is not covered by the stored
    keys means the login changed. Missing databases are reported but do not
    ask for a scan, because there is nothing to capture against.
    """

    accounts_now = {str(db.relative_path).split("/", 1)[0] for db in databases}
    known = {
        str(item.get("relative_path") or "").split("/", 1)[0]
        for item in keys
        if item.get("relative_path")
    }
    known.discard("")
    baseline = known | set(observed_accounts or [])
    if databases and known and (accounts_now - baseline):
        return {"stale": True, "fresh": False, "needs_scan": True, "reason": "new_account"}
    if not databases:
        return {"stale": True, "fresh": False, "needs_scan": False, "reason": "databases_not_found"}
    if not keys:
        return {"stale": True, "fresh": False, "needs_scan": True, "reason": "no_keys"}
    # Historical accounts already present during this capture are not a new
    # login. Check only connected accounts; they still need real core HMACs.
    cores = [db for db in databases if is_core_database(db.relative_path)
             and str(db.relative_path).split("/", 1)[0] in known]
    if not cores:
        return {"stale": True, "fresh": False, "needs_scan": True, "reason": "core_missing"}
    _count, verified = verify_keys_against_libs(keys, cores)
    if any(db.relative_path not in verified for db in cores):
        return {"stale": True, "fresh": False, "needs_scan": True, "reason": "core_hmac_failed"}
    return {"stale": False, "fresh": True, "needs_scan": False, "reason": None}


class AutoCapture:
    """Idempotent, self-cleaning orchestrator. Inject collaborators for tests."""

    def __init__(
        self,
        config: AutoCaptureConfig,
        *,
        resign_actions,
        interpose_actions,
        store,
        prepare_copy=_prepare_copy,
        restore_original=_restore_original,
        build_hook=_build_hook,
        capture=_capture,
        sync_databases=_sync_databases,
        load_databases=load_databases,
    ):
        self.config = config
        self.resign_actions = resign_actions
        self.interpose_actions = interpose_actions
        self.store = store
        self._prepare_copy = prepare_copy
        self._restore_original = restore_original
        self._build_hook = build_hook
        self._capture = capture
        self._sync_databases = sync_databases
        self._load_databases = load_databases

    # -- idempotency -------------------------------------------------------
    def _account_libs(self):
        dbs = list(self._load_databases(self.config.db_root))
        if self.config.account_prefix:
            dbs = [d for d in dbs if d.relative_path.split("/", 1)[0] == self.config.account_prefix]
        return dbs

    def already_satisfied(self, report: AutoCaptureReport) -> bool:
        keys = load_keys(self.config.state_dir, self.store)
        if not keys:
            return False
        dbs = self._account_libs()
        report.libs_total = len(dbs)
        verified_count, verified = verify_keys_against_libs(keys, dbs)
        report.libs_key_verified = verified_count
        # Satisfied when every library we can see has a currently-valid key.
        return bool(dbs) and verified == {d.relative_path for d in dbs}

    # -- main entry --------------------------------------------------------
    def run(self, *, force: bool = False, report: AutoCaptureReport | None = None) -> AutoCaptureReport:
        report = report if report is not None else AutoCaptureReport()
        report.phase = PHASE_PREFLIGHT
        report.ok = False
        report.error = None
        report.needs_scan = False
        report.keys_already_valid = False
        report.official_reopened = False
        report.copy_cleaned = False
        report.scan_status = ""
        touched_copy = False
        try:
            from .mac_key_storage import cleanup_stale_capture_files, cleanup_hook_files
            cleanup_stale_capture_files(self.config.state_dir)
            cleanup_hook_files(self.config.db_root)
            if not force and self.already_satisfied(report):
                report.phase = PHASE_SATISFIED
                report.keys_already_valid = True
                report.needs_scan = False
                report.scan_status = "done"
                report.ok = True
                self._safe_sync(report)
                report.phase = PHASE_DONE
                report.scan_status = "done"
                return report

            # A scan is required: prepare the copy (quits official once).
            shadow = shadow_app(self.config.state_dir)
            prepare_force = bool(force)
            rebuild = prepare_force or not shadow.is_dir()
            if not rebuild and shadow.is_dir() and not copy_is_agent(shadow):
                # An older copy without LSUIElement must be rebuilt before signing.
                rebuild = True
                prepare_force = True
            if rebuild or shadow.is_dir():
                touched_copy = True
            if rebuild:
                report.phase = PHASE_PREPARE
                self._prepare_copy(
                    self.config.official_app,
                    self.config.state_dir,
                    self.resign_actions,
                    signature_text=self.resign_actions.signature_text(self.config.official_app),
                    force=prepare_force,
                )

            if self.config.is_cancelled and self.config.is_cancelled():
                raise CaptureError('cancelled')
            report.phase = PHASE_BUILD_HOOK
            self._build_hook(self.config.state_dir)

            report.phase = PHASE_AWAIT_SCAN
            report.needs_scan = True
            report.scan_status = "waiting"
            capture_config = CaptureConfig(
                official_app=self.config.official_app,
                work_root=self.config.state_dir,
                db_root=self.config.db_root,
                timeout_seconds=self.config.scan_timeout_seconds,
                shadow_app=shadow,
                account_prefix=self.config.account_prefix,
                require_live_login=self.config.require_live_login,
                is_cancelled=self.config.is_cancelled,
            )
            result = self._capture(
                capture_config,
                self.interpose_actions,
                self.store,
                acknowledge_debug_copy=True,
                force=True,
                on_verifying=lambda total: self._mark_verifying(report, total),
            )

            matched = getattr(result, "matched", None) or []
            capture_error = getattr(result, "error", None)
            report.libs_key_verified = len(matched)
            if not matched or capture_error:
                report.error = capture_error or "no_passphrase_captured"
                report.phase = PHASE_FAILED
                report.needs_scan = True
                report.scan_status = "failed"
                if report.error in USER_ACTION_ERRORS:
                    report.notes.append("请确认完成扫码，勿反复重试。")
                return report
            accounts = {item['relative_path'].split('/', 1)[0] for item in matched}
            if self.config.require_live_login:
                if len(accounts) != 1:
                    raise CaptureError('account_not_confirmed')
                self.config.account_prefix = next(iter(accounts))
            report.connected_account = self.config.account_prefix or (next(iter(accounts)) if len(accounts) == 1 else None)
            if self.config.on_account_captured and report.connected_account:
                report.library_id = self.config.on_account_captured(report.connected_account)
            report.phase = PHASE_CAPTURED
            report.scan_status = "captured"
            report.needs_scan = False
            report.phase = PHASE_DERIVE
            report.scan_status = "verifying"
            # Finish restoring WeChat before publishing a readable library.
            self._cleanup_copy(report)
            touched_copy = False
            if not report.copy_cleaned or not report.official_reopened:
                report.ok = False
                report.error = "wechat_restore_failed"
                report.phase = PHASE_FAILED
                report.scan_status = "failed"
                return report
            report.ok = True
            self._safe_sync(report)
            report.phase = PHASE_DONE
            report.scan_status = "done"
            return report
        except Exception as exc:  # noqa: BLE001 - surface code, never secrets
            report.phase = PHASE_FAILED
            report.error = getattr(exc, "code", None) or type(exc).__name__
            report.scan_status = "failed"
            return report
        finally:
            # Always delete the copy and reopen the official app.
            if touched_copy:
                self._cleanup_copy(report)

    @staticmethod
    def _mark_verifying(report: AutoCaptureReport, total: int) -> None:
        report.phase = PHASE_DERIVE
        report.scan_status = "verifying"
        report.needs_scan = False
        report.libs_total = total

    def _cleanup_copy(self, report: AutoCaptureReport) -> None:
        shadow = shadow_app(self.config.state_dir)
        try:
            restore = self._restore_original(
                self.config.official_app,
                self.config.state_dir,
                self.resign_actions,
                relaunch=True,
            )
            report.official_reopened = bool(restore.get("ok"))
            report.copy_cleaned = bool(restore.get("trial_removed"))
        except Exception as exc:  # noqa: BLE001
            report.notes.append(f"restore_failed:{getattr(exc, 'code', type(exc).__name__)}")
            report.copy_cleaned = False
        if report.copy_cleaned:
            return
        # Never unlink an executable while its temporary process is still alive.
        running = getattr(self.resign_actions, "shadow_still_running", None)
        if callable(running) and running(shadow):
            return
        remover = getattr(self.resign_actions, "remove_tree", None)
        if not callable(remover):
            return
        try:
            if shadow.exists():
                remover(shadow)
            report.copy_cleaned = not shadow.exists()
        except Exception as exc:  # noqa: BLE001
            report.notes.append(f"copy_delete_failed:{getattr(exc, 'code', type(exc).__name__)}")

    def _safe_sync(self, report: AutoCaptureReport) -> None:
        """Run the post-capture incremental sync. Never fails the capture: a
        sync error is recorded as a note and the successful key capture stands."""
        try:
            self._run_sync(report)
        except Exception as exc:  # noqa: BLE001 - never crash the main flow
            report.notes.append(f"sync_failed:{getattr(exc,'code',type(exc).__name__)}")

    def _run_sync(self, report: AutoCaptureReport) -> None:
        report.phase = PHASE_SYNC
        options = {'account_prefix': self.config.account_prefix} if self.config.account_prefix else {}
        if self.config.on_database_synced:
            options['on_database_synced'] = self.config.on_database_synced
        rows = self._sync_databases(self.config.db_root, self.config.project_root, self.store, **options)
        if self.config.account_prefix:
            rows = [r for r in rows if r["relative_path"].split("/", 1)[0] == self.config.account_prefix]
        report.libs_synced = sum(1 for r in rows if r["status"] == "synced")
        if not report.libs_total:
            report.libs_total = len(rows)


def run_with_retries(
    pipeline: AutoCapture,
    *,
    force: bool = False,
    report: AutoCaptureReport | None = None,
    sleep=None,
    max_retries: int = MAX_TRANSIENT_RETRIES,
) -> AutoCaptureReport:
    """Run a capture, retrying only infrastructure errors.

    At most ``max_retries`` extra attempts (default 2). Each retry uses
    ``force=True`` so ``prepare_copy`` deletes the previous copy first, and
    waits with exponential backoff. ``no_passphrase_captured`` and
    ``coverage_incomplete`` return immediately so the user is not prompted
    again in a loop. Every attempt still deletes the copy in ``run``'s finally.
    """

    if sleep is None:
        sleep = time.sleep
    report = report if report is not None else AutoCaptureReport()
    attempt_force = bool(force)
    attempts = max(0, int(max_retries)) + 1
    for attempt in range(attempts):
        pipeline.run(force=attempt_force, report=report)
        if report.ok or report.error not in TRANSIENT_ERRORS:
            return report
        if attempt >= attempts - 1:
            report.notes.append(f"retries_exhausted:{report.error}")
            return report
        report.notes.append(f"retry:{report.error}:{attempt + 1}")
        sleep(0.5 * (2 ** attempt))
        attempt_force = True
    return report


def run_auto_capture(
    config: AutoCaptureConfig,
    *,
    resign_actions,
    interpose_actions,
    store,
    **deps,
) -> dict:
    """Thin app-callable wrapper returning a secret-free report dict."""
    force = bool(deps.pop("force", False))
    sleep = deps.pop("retry_sleep", None)
    max_retries = int(deps.pop("max_retries", MAX_TRANSIENT_RETRIES))
    pipeline = AutoCapture(
        config,
        resign_actions=resign_actions,
        interpose_actions=interpose_actions,
        store=store,
        **deps,
    )
    return run_with_retries(
        pipeline,
        force=force,
        sleep=sleep,
        max_retries=max_retries,
    ).public()


class AutoCaptureJob:
    """Single background capture run, so an HTTP route can start the full-auto
    flow and return immediately instead of blocking for the whole scan window.

    The shared AutoCaptureReport is updated in place by AutoCapture.run, so
    snapshot() reflects the live phase (e.g. ``await_scan`` with
    ``needs_scan=True``) while the worker thread is still waiting for the user
    to scan / tap login. Secrets never enter the report, so snapshot() is safe
    to hand to a front-end. Only one job runs at a time.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._report: AutoCaptureReport | None = None

    def _alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def is_running(self) -> bool:
        with self._lock:
            return self._alive()

    def start(self, run_fn, *, force: bool = False) -> dict:
        """run_fn(report, force) must execute the pipeline, updating report in
        place. Returns a snapshot; if a job is already running it is not
        restarted (idempotent: no duplicate capture, no extra prompt)."""
        with self._lock:
            if self._alive():
                return {"started": False, "running": True, **self._report.public()}
            self._report = AutoCaptureReport()
            report = self._report

            def _target():
                try:
                    run_fn(report, force)
                except Exception as exc:  # noqa: BLE001
                    report.phase = PHASE_FAILED
                    report.error = getattr(exc, "code", type(exc).__name__)

            self._thread = threading.Thread(target=_target, name="auto-capture", daemon=True)
            self._thread.start()
            return {"started": True, "running": True, **report.public()}

    def arm_scan(self, reason: str) -> dict:
        """Ask for one scan without quitting WeChat.

        The card button is what calls ``run(force=True)``. Arming only marks
        the secret-free snapshot so the UI can show 「连接你的微信」 before the
        official app is logged out.
        """

        code = str(reason or "keys_stale")
        with self._lock:
            if self._alive() and self._report is not None:
                return {"armed": False, "running": True, **self._report.public()}
            if self._report is not None and self._report.phase in {
                PHASE_FAILED,
                PHASE_AWAIT_SCAN,
                PHASE_SCAN_READY,
                PHASE_PREPARE,
                PHASE_BUILD_HOOK,
                PHASE_CAPTURED,
                PHASE_DERIVE,
                PHASE_SYNC,
            }:
                marker = f"stale:{code}"
                if marker not in self._report.notes:
                    self._report.notes.append(marker)
                return {"armed": False, "running": self._alive(), **self._report.public()}
            self._report = AutoCaptureReport()
            self._report.needs_scan = True
            self._report.phase = PHASE_SCAN_READY
            self._report.scan_status = "ready"
            self._report.notes.append(f"stale:{code}")
            return {"armed": True, "running": False, **self._report.public()}

    def snapshot(self) -> dict:
        with self._lock:
            if self._report is None:
                return {"running": False, "report": None}
            return {"running": self._alive(), **self._report.public()}


_shared_job: AutoCaptureJob | None = None
_shared_job_lock = threading.Lock()


def shared_auto_job() -> AutoCaptureJob:
    """One capture job per process, shared by the desktop service routes."""

    global _shared_job
    with _shared_job_lock:
        if _shared_job is None:
            _shared_job = AutoCaptureJob()
        return _shared_job


def reset_shared_auto_job() -> AutoCaptureJob:
    """Test hook. Drops the previous job object; a running thread is left to exit."""

    global _shared_job
    with _shared_job_lock:
        _shared_job = AutoCaptureJob()
        return _shared_job
