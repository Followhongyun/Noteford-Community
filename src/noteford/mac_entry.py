"""Minimal macOS entry: prepare copy, LLDB capture (QR), restore, read-only sync.

Electron on darwin can spawn this module. The Windows service keeps its own
process. Importing this module does not import winmem.

Morning acceptance (needs Hongyun QR scan):
  python -m noteford.mac_entry capture --acknowledge-debug-copy
"""

from __future__ import annotations

import argparse
import hmac
import json
import secrets
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .mac_capture import (
    CaptureConfig,
    CaptureError,
    DarwinLldbActions,
    SecurityKeychainStore,
    capture_login,
    default_db_roots,
    select_active_root,
    load_core_databases,
)
from .mac_resign import (
    DarwinResignActions,
    assess_pipeline,
    cleanup_legacy_copies,
    default_state_dir,
    keys_path,
    load_record,
    prepare_copy,
    restore_original,
    restore_steps,
    shadow_app,
)
from .mac_interpose import build_hook, capture_login_interpose
from .mac_auto import (
    AutoCapture,
    AutoCaptureConfig,
    AutoCaptureJob,
    assess_key_freshness,
    load_keys,
    run_auto_capture,
    run_with_retries,
    shared_auto_job,
)
from .mac_sync import LoginGatedSync, needs_recapture, sync_databases


def route_status(
    platform: str | None = None,
    *,
    app: Path | None = None,
    state_dir: Path | None = None,
    signature_text: str | None = None,
) -> dict:
    platform = platform or sys.platform
    if platform == "win32":
        return {"ok": False, "error": "windows_key_path_unchanged"}
    if platform != "darwin" and signature_text is None:
        return {"ok": False, "error": "darwin_required", "restore": restore_steps()}
    if app is None:
        app = Path("/Applications/WeChat.app")
    state_dir = state_dir or default_state_dir()
    if signature_text is None:
        if not Path("/usr/bin/codesign").exists():
            return {"ok": False, "error": "darwin_required", "restore": restore_steps()}
        signature_text = DarwinResignActions().signature_text(app)
    status = assess_pipeline(app, signature_text, state_dir)
    status["ok"] = status["state"] == "ready"
    status["restore"] = restore_steps()
    status["morning_command"] = (
        "python -m noteford.mac_entry capture --acknowledge-debug-copy"
        "  # 需洪云扫码；先菜单退出微信"
    )
    return status


def route_action(payload: dict, *, platform: str | None = None) -> dict:
    from .workspace_access import validate_capture_paths
    validate_capture_paths(payload)
    platform = platform or sys.platform
    if platform == "win32":
        return {"ok": False, "error": "windows_key_path_unchanged"}
    if platform != "darwin":
        return {"ok": False, "error": "darwin_required"}
    action = str(payload.get("action") or "status")
    app = Path(str(payload.get("app") or "/Applications/WeChat.app"))
    state_dir = Path(str(payload.get("state_dir") or default_state_dir()))
    if action == "status":
        return route_status(platform, app=app, state_dir=state_dir)
    if action in {"prepare", "resign"}:
        # "resign" kept as alias name in HTTP API but runs prepare_copy.
        actions = DarwinResignActions()
        try:
            result = prepare_copy(
                app,
                state_dir,
                actions,
                signature_text=actions.signature_text(app),
                force=bool(payload.get("force", False)),
            )
        except CaptureError as exc:
            return {"ok": False, "error": exc.code}
        result["ok"] = result["state"] in {"needs_capture", "ready"}
        return result
    if action == "restore":
        actions = DarwinResignActions()
        try:
            result = restore_original(app, state_dir, actions, relaunch=bool(payload.get("relaunch", True)))
        except CaptureError as exc:
            return {"ok": False, "error": exc.code}
        result["ok"] = True
        return result
    if action == "sync":
        db_root = Path(str(payload["db_root"]))
        project_root = Path(str(payload.get("project_root") or state_dir.parent))
        rows = sync_databases(db_root, project_root, SecurityKeychainStore())
        return {"ok": True, "needs_recapture": needs_recapture(rows), "databases": rows}
    if action == "capture":
        return {"ok": False, "error": "capture_is_cli_only_needs_qr_scan"}
    return {"ok": False, "error": "unknown_action"}



def build_auto_config(
    *,
    app: Path,
    state_dir: Path,
    db_root: Path | None = None,
    project_root: Path | None = None,
    account_prefix: str | None = None,
    timeout: float = 600.0,
) -> AutoCaptureConfig | None:
    db_root = db_root or select_active_root(default_db_roots())
    if db_root is None:
        return None
    return AutoCaptureConfig(
        official_app=app,
        state_dir=state_dir,
        db_root=db_root,
        project_root=project_root or state_dir.parent,
        account_prefix=account_prefix,
        scan_timeout_seconds=float(timeout),
    )


def _auto_actions():
    from .mac_interpose import DarwinInterposeActions

    return DarwinResignActions(), DarwinInterposeActions(), SecurityKeychainStore()


def run_auto_capture_blocking(config: AutoCaptureConfig, *, force: bool = False) -> dict:
    """Blocking full-auto capture for the CLI. Returns a secret-free report."""
    resign, interpose, store = _auto_actions()
    return run_auto_capture(
        config,
        resign_actions=resign,
        interpose_actions=interpose,
        store=store,
        force=force,
    )


def auto_capture_run_fn(config: AutoCaptureConfig):
    """Build a run_fn(report, force) for AutoCaptureJob (non-blocking serve)."""
    resign, interpose, store = _auto_actions()

    def _run(report, force):
        pipeline = AutoCapture(
            config,
            resign_actions=resign,
            interpose_actions=interpose,
            store=store,
        )
        run_with_retries(pipeline, force=force, report=report)

    return _run


def _freshness(state_dir: Path, db_root: Path | None) -> dict:
    keys = load_keys(state_dir)
    if db_root is None:
        return {"stale": True, "fresh": False, "needs_scan": False, "reason": "databases_not_found"}
    from .mac_key_storage import database_payload
    observed = database_payload(keys_path(state_dir)).get('observed_accounts')
    return assess_key_freshness(keys, load_core_databases(db_root), observed_accounts=observed)


_ACTIVE_CAPTURE_PHASES = {
    "failed",
    "await_scan",
    "scan_ready",
    "prepare_copy",
    "build_hook",
    "captured",
    "derive_verify",
    "sync",
    "preflight",
}


def _scan_ready_view(reason: str | None) -> dict:
    code = reason or "keys_stale"
    return {
        "running": False,
        "ok": False,
        "needs_scan": True,
        "phase": "scan_ready",
        "scan_status": "ready",
        "keys_already_valid": False,
        "error": None,
        "stale_reason": code,
        "notes": [f"stale:{code}"],
    }


def present_auto_capture(job: AutoCaptureJob, freshness: dict | None = None) -> dict:
    """Secret-free snapshot, filled in from the lightweight key check when idle."""

    snap = job.snapshot()
    if snap.get("running"):
        return snap
    phase = snap.get("phase")
    if snap.get("error") == "wechat_restore_failed":
        return snap
    if freshness and freshness.get("needs_scan") and phase not in _ACTIVE_CAPTURE_PHASES:
        return _scan_ready_view(freshness.get("reason"))
    if phase in _ACTIVE_CAPTURE_PHASES or (snap.get("ok") and phase == "done"):
        return snap
    if freshness and freshness.get("fresh"):
        return {
            "running": False,
            "ok": True,
            "needs_scan": False,
            "phase": "already_satisfied",
            "scan_status": "done",
            "keys_already_valid": True,
            "error": None,
            "stale_reason": None,
            "notes": [],
        }
    if freshness and freshness.get("reason") == "databases_not_found":
        return {
            "running": False,
            "ok": False,
            "needs_scan": False,
            "phase": "scan_ready",
            "scan_status": "blocked",
            "keys_already_valid": False,
            "error": None,
            "stale_reason": "databases_not_found",
            "notes": ["stale:databases_not_found"],
        }
    return snap


def arm_macos_key_check(*, job: AutoCaptureJob | None = None, state_dir: Path | None = None) -> dict:
    """Startup / pre-sync check. Arms the scan card; does not quit WeChat."""

    if sys.platform != "darwin":
        return {"skipped": "not_darwin"}
    state_dir = state_dir or default_state_dir()
    try:
        cleanup_legacy_copies(state_dir, DarwinResignActions())
    except Exception:
        pass
    db_root = select_active_root(default_db_roots())
    fresh = _freshness(state_dir, db_root)
    if fresh.get("needs_scan"):
        (job or shared_auto_job()).arm_scan(fresh.get("reason") or "keys_stale")
    return fresh


def route_auto_capture_snapshot(*, platform: str | None = None, job: AutoCaptureJob | None = None, state_dir: Path | None = None) -> dict:
    platform = platform or sys.platform
    if platform == "win32":
        return {"ok": False, "error": "windows_key_path_unchanged"}
    if platform != "darwin":
        return {"ok": False, "error": "darwin_required"}
    job = job or shared_auto_job()
    snap = job.snapshot()
    # A running capture already publishes its phase. Don't re-read libraries on
    # every UI poll.
    if snap.get("running") or snap.get("phase") in _ACTIVE_CAPTURE_PHASES:
        return snap
    state_dir = state_dir or default_state_dir()
    db_root = select_active_root(default_db_roots())
    return present_auto_capture(job, _freshness(state_dir, db_root))


def route_auto_capture_start(payload: dict | None = None, *, platform: str | None = None, job: AutoCaptureJob | None = None) -> dict:
    from .workspace_access import validate_capture_paths
    validate_capture_paths(payload or {})
    platform = platform or sys.platform
    if platform == "win32":
        return {"ok": False, "error": "windows_key_path_unchanged"}
    if platform != "darwin":
        return {"ok": False, "error": "darwin_required"}
    payload = payload or {}
    app = Path(str(payload.get("app") or "/Applications/WeChat.app"))
    state_dir = Path(str(payload.get("state_dir") or default_state_dir()))
    config = build_auto_config(
        app=app,
        state_dir=state_dir,
        db_root=Path(str(payload["db_root"])) if payload.get("db_root") else None,
        project_root=Path(str(payload["project_root"])) if payload.get("project_root") else None,
        account_prefix=payload.get("account_prefix"),
        timeout=float(payload.get("timeout", 600.0)),
    )
    if config is None:
        return {"ok": False, "error": "databases_not_found"}
    # The button is the consent to quit official WeChat once. Stale keys use force.
    force = bool(payload.get("force", True))
    return (job or shared_auto_job()).start(auto_capture_run_fn(config), force=force)


def _load_or_create_token(path: Path) -> str:
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        token = payload.get("token")
        if payload.get("version") == 1 and isinstance(token, str) and len(token) >= 32:
            return token
        raise ValueError("invalid API token file")
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"version": 1, "token": token}), encoding="utf-8")
    temporary.replace(path)
    return token


def build_server(
    host: str,
    port: int,
    token_file: Path,
    state_dir: Path,
    app: Path,
    *,
    sync_interval: float = 0.0,
) -> ThreadingHTTPServer:
    token = _load_or_create_token(token_file)
    auto_job = AutoCaptureJob()

    # Resident scheduled incremental sync (reuses the serve process + keychain).
    def _scheduled_sync_once():
        db_root = select_active_root(default_db_roots())
        if db_root is None:
            raise CaptureError("databases_not_found")
        sync_databases(db_root, state_dir.parent, SecurityKeychainStore())

    # Single login-gated scheduler: waits for an active WeChat login, syncs once,
    # then every interval anchored on that sync. No separate boot thread.
    # A stale core key arms the scan card and skips this cycle instead of
    # syncing with a key that no longer opens session.db / contact.db.
    from .login_status import mac_login_status

    def _before_sync(_account=None):
        db_root = select_active_root(default_db_roots())
        fresh = _freshness(state_dir, db_root)
        if fresh.get("needs_scan"):
            auto_job.arm_scan(fresh.get("reason") or "keys_stale")
        return fresh

    scheduler = LoginGatedSync(
        mac_login_status,
        _scheduled_sync_once,
        sync_interval,
        before_sync=_before_sync,
    )
    arm_macos_key_check(job=auto_job, state_dir=state_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "NotefordMac/0.2"
        sys_version = ""

        def log_message(self, _format: str, *_args) -> None:
            return

        def _json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            presented = self.headers.get("X-Noteford-Token", "")
            return bool(presented) and hmac.compare_digest(presented, token)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/api/v1/health":
                self._json(HTTPStatus.OK, {"ok": True})
                return
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                return
            if path == "/api/v1/mac/wechat":
                self._json(HTTPStatus.OK, route_status(app=app, state_dir=state_dir))
                return
            if path == "/api/v1/mac/auto-capture":
                # Poll the background full-auto capture job (non-blocking).
                self._json(HTTPStatus.OK, auto_job.snapshot())
                return
            if path == "/api/v1/mac/sync-scheduler":
                # Status of the resident scheduled incremental sync (no secrets).
                self._json(HTTPStatus.OK, {"ok": True, **scheduler.snapshot()})
                return
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except (UnicodeError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
                return
            if not isinstance(payload, dict):
                payload = {}
            payload.setdefault("app", str(app))
            payload.setdefault("state_dir", str(state_dir))
            if path == "/api/v1/mac/wechat":
                self._json(HTTPStatus.OK, route_action(payload))
                return
            if path == "/api/v1/mac/auto-capture":
                # Start the full-auto capture in the background and return
                # immediately. The caller polls GET /api/v1/mac/auto-capture;
                # while a scan/login tap is needed the snapshot carries
                # needs_scan=True instead of the request blocking. Never blocks,
                # never returns secrets.
                config = build_auto_config(
                    app=app,
                    state_dir=state_dir,
                    db_root=Path(str(payload["db_root"])) if payload.get("db_root") else None,
                    project_root=Path(str(payload["project_root"])) if payload.get("project_root") else None,
                    account_prefix=payload.get("account_prefix"),
                    timeout=float(payload.get("timeout", 600.0)),
                )
                if config is None:
                    self._json(HTTPStatus.OK, {"ok": False, "error": "databases_not_found"})
                    return
                snap = auto_job.start(
                    auto_capture_run_fn(config),
                    force=bool(payload.get("force", False)),
                )
                self._json(HTTPStatus.OK, snap)
                return
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    server = ThreadingHTTPServer((host, port), Handler)
    server.sync_scheduler = scheduler  # type: ignore[attr-defined]
    return server


def serve(
    host: str,
    port: int,
    token_file: Path,
    state_dir: Path,
    app: Path,
    *,
    sync_interval: float = 0.0,
) -> None:
    server = build_server(host, port, token_file, state_dir, app, sync_interval=sync_interval)
    actual = int(server.server_address[1])
    scheduler = getattr(server, "sync_scheduler", None)
    started = scheduler.start() if scheduler is not None else False
    print(
        json.dumps(
            {
                "ready": True,
                "host": host,
                "port": actual,
                "token_file": str(token_file),
                "sync_scheduler": {"enabled": bool(started), "interval_seconds": float(sync_interval)},
            }
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        if scheduler is not None:
            scheduler.stop()


def _image_probe(path: Path | None) -> bytes | None:
    if path is None:
        return None
    data = path.read_bytes()
    if data[:6] == b"\x07\x08V2\x08\x07" and len(data) >= 31:
        return data[15:31]
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Noteford macOS WeChat: prepare copy + LLDB capture + sync"
    )
    parser.add_argument(
        "command",
        choices=("status", "prepare", "resign", "capture", "restore", "sync", "serve", "auto-capture"),
    )
    parser.add_argument("--app", type=Path, default=Path("/Applications/WeChat.app"))
    parser.add_argument("--state", type=Path)
    parser.add_argument("--db-root", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--account", type=str, help="account prefix (wxid_...) to scope auto-capture idempotency/sync")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--image-dat", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-relaunch", action="store_true")
    parser.add_argument(
        "--acknowledge-debug-copy",
        action="store_true",
        help="Required for capture: you quit WeChat and accept a temporary debug copy of your own app",
    )
    parser.add_argument(
        "--lldb",
        action="store_true",
        help="Use the LLDB-launch fallback route instead of the default DYLD interpose route",
    )
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument(
        "--sync-interval",
        type=float,
        default=300.0,
        help="login-gated incremental sync interval in seconds for serve (default 300 = 5 min; 0 disables)",
    )
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--signature-file", type=Path)
    parser.add_argument("--hook", type=Path, help=argparse.SUPPRESS)  # abandoned
    args = parser.parse_args(argv)
    state_dir = args.state or default_state_dir()
    signature_text = args.signature_file.read_text(encoding="utf-8") if args.signature_file else None

    if args.command == "serve":
        if sys.platform != "darwin":
            print("mac_entry serve runs on macOS.", file=sys.stderr)
            return 2
        token_file = args.token_file or (state_dir / "api_token.json")
        serve("127.0.0.1", args.port, token_file, state_dir, args.app, sync_interval=args.sync_interval)
        return 0

    if args.command == "status":
        payload = route_status(app=args.app, state_dir=state_dir, signature_text=signature_text)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("state") or payload.get("error") else 1

    if sys.platform != "darwin":
        print(json.dumps({"ok": False, "error": "darwin_required"}, ensure_ascii=False))
        return 2

    actions = DarwinResignActions()
    if args.command in {"prepare", "resign"}:
        if args.hook is not None:
            print(json.dumps({"ok": False, "error": "hook_abandoned_use_prepare_copy"}, ensure_ascii=False))
            return 1
        try:
            result = prepare_copy(
                args.app,
                state_dir,
                actions,
                signature_text=signature_text or actions.signature_text(args.app),
                force=args.force,
            )
        except CaptureError as exc:
            print(json.dumps({"ok": False, "error": exc.code}, ensure_ascii=False, indent=2))
            return 1
        public = {key: value for key, value in result.items() if key != "command"}
        public["codesign"] = result.get("command")
        public["next"] = (
            "早上验收：菜单退出微信后运行 "
            "python -m noteford.mac_entry capture --acknowledge-debug-copy"
        )
        print(json.dumps(public, ensure_ascii=False, indent=2))
        return 0 if result.get("state") in {"needs_capture", "ready"} else 1

    if args.command == "restore":
        try:
            result = restore_original(args.app, state_dir, actions, relaunch=not args.no_relaunch)
        except CaptureError as exc:
            print(json.dumps({"ok": False, "error": exc.code}, ensure_ascii=False))
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "capture":
        # === MORNING QR SCAN STEP — Hongyun must be at the Mac ===
        if not args.acknowledge_debug_copy:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "acknowledge_debug_copy_required",
                        "hint": "先菜单退出微信，再加 --acknowledge-debug-copy；临时窗口里扫码，不要在副本里聊天。",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        db_root = args.db_root or select_active_root(default_db_roots())
        if db_root is None:
            print(json.dumps({"ok": False, "error": "databases_not_found"}, ensure_ascii=False))
            return 1
        shadow = shadow_app(state_dir)
        if not shadow.is_dir():
            try:
                prepare_copy(
                    args.app,
                    state_dir,
                    actions,
                    signature_text=signature_text or actions.signature_text(args.app),
                    force=args.force,
                )
            except CaptureError as exc:
                print(json.dumps({"ok": False, "error": exc.code, "phase": "prepare"}, ensure_ascii=False))
                return 1
        config = CaptureConfig(
            official_app=args.app,
            work_root=state_dir,
            db_root=db_root,
            timeout_seconds=args.timeout,
            shadow_app=shadow_app(state_dir),
            image_probe=_image_probe(args.image_dat),
        )
        print(
            json.dumps(
                {
                    "phase": "capture",
                    "instruction": "临时微信窗口出现后请扫码/确认登录；完成后会自动结束副本并尝试恢复原版。",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        if args.lldb:
            result = capture_login(
                config,
                DarwinLldbActions(),
                SecurityKeychainStore(),
                acknowledge_debug_copy=True,
                force=args.force,
            )
        else:
            # Default: DYLD interpose route. Build the hook dylib into the state dir.
            try:
                build_hook(state_dir)
            except CaptureError as exc:
                print(json.dumps({"ok": False, "error": exc.code, "phase": "build_hook"}, ensure_ascii=False))
                return 1
            from .mac_interpose import DarwinInterposeActions

            result = capture_login_interpose(
                config,
                DarwinInterposeActions(),
                SecurityKeychainStore(),
                acknowledge_debug_copy=True,
                force=args.force,
            )
        # Always attempt restore so original WeChat comes back after the window.
        try:
            restore_original(args.app, state_dir, actions, relaunch=not args.no_relaunch)
        except CaptureError as restore_exc:
            summary = result.public_summary()
            summary["restore_error"] = restore_exc.code
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps(result.public_summary(), ensure_ascii=False, indent=2))
        return 0 if result.error is None else 1

    if args.command == "auto-capture":
        # Full-auto, idempotent capture: skips scan when Keychain keys already
        # verifies; otherwise runs the whole copy->resign->hook->launch->
        # capture->derive->verify->sync chain and always restores the official
        # app. Blocking (a CLI run); the serve route is the non-blocking path.
        config = build_auto_config(
            app=args.app,
            state_dir=state_dir,
            db_root=args.db_root,
            project_root=args.project,
            account_prefix=args.account,
            timeout=args.timeout if args.timeout and args.timeout > 1 else 600.0,
        )
        if config is None:
            print(json.dumps({"ok": False, "error": "databases_not_found"}, ensure_ascii=False))
            return 1
        report = run_auto_capture_blocking(config, force=args.force)
        print(json.dumps({"ok": report.get("ok", False), **report}, ensure_ascii=False, indent=2))
        return 0 if report.get("ok") else 1

    db_root = args.db_root
    if db_root is None:
        db_root = select_active_root(default_db_roots())
    if db_root is None:
        print(json.dumps({"ok": False, "error": "databases_not_found"}))
        return 1
    project = args.project or state_dir.parent
    from .mac_key_storage import database_payload
    store = SecurityKeychainStore()
    database_payload(keys_path(state_dir), store)
    rows = sync_databases(db_root, project, store)
    print(
        json.dumps(
            {"ok": True, "needs_recapture": needs_recapture(rows), "databases": _public_rows(rows)},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _public_rows(rows: list[dict]) -> list[dict]:
    return [
        {
            "relative_path": row["relative_path"],
            "status": row["status"],
            "integrity": row.get("integrity"),
            "wal_frames": row["wal_frames"],
        }
        for row in rows
    ]


if __name__ == "__main__":
    raise SystemExit(main())
