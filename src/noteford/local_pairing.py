"""Noteford owner-approved local MCP connections.

Only current token-based connections are accepted. The desktop reviews pending
requests and explicitly approves read-only scope. No legacy client IDs or codes.
Credentials are encrypted using AES-GCM. Windows wraps the data key with
DPAPI. macOS keeps it in the login keychain. Automatic profiles keep reusable
tokens inside that encrypted store only.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Callable

from .libraries import LibraryRegistry, load_root_config
from .mcp_access import read_grant
from .remote_mcp import RemoteStore

REQUEST_TTL = 300
CONFIRM_TTL = 300
PAIR_TOKEN_TTL = 30 * 86400
PROMPT_TOKEN_TTL = 86400  # unbound token from "复制提示词"; scope is chosen at approval
LAST_USED_THROTTLE = 60
HINT = "请在 Noteford 弹窗中确认本次连接，并选择允许或拒绝"


from mcp.server.mcpserver.exceptions import ToolError


class PairingDenied(ToolError, PermissionError):
    """Shown verbatim to the AI client (ToolError messages are not masked)."""


class PairingRequired(PairingDenied):
    pass


def parent_executable() -> str:
    try:
        import psutil
        return psutil.Process(os.getppid()).exe() or "unknown"
    except Exception:
        return "unknown"


@contextlib.contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            for _ in range(200):
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                raise TimeoutError("pairing store busy")
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _scope_snapshot(grant: dict) -> dict:
    return {"library_id": grant["library_id"], "scope": grant.get("scope", "selected"),
            "conversations": sorted(set(grant["conversations"])), "allow_sync": grant.get("allow_sync") is True}


def _within(current: dict, snap: dict) -> bool:
    if current["library_id"] != snap["library_id"]:
        return False
    if current.get("allow_sync") is True and not snap.get("allow_sync"):
        return False
    if snap["scope"] == "all_indexed":
        return True
    if snap["scope"] == "all_synced":
        return current.get("scope", "selected") in ("selected", "all_synced")
    return current.get("scope", "selected") == "selected" and set(current["conversations"]) <= set(snap["conversations"])


class LocalPairing:
    def __init__(self, app_root: Path, key: bytes, clock: Callable[[], float] = time.time):
        self.app_root = app_root
        from .product_workspace import WorkspaceAccess
        self.workspace_access = WorkspaceAccess(app_root)
        self.clock = clock
        self.store = RemoteStore(app_root / "data" / "mcp_local.store", key)
        self._lock_path = app_root / "data" / "mcp_local.lock"

    @contextlib.contextmanager
    def tx(self):
        self.workspace_access.check()
        with _file_lock(self._lock_path):
            self.store.data = self.store._load()
            for table in ("tokens", "requests", "bindings"):
                self.store.data.setdefault(table, {})
            try:
                yield self.store.data
            except (ValueError, PermissionError):
                self.store.save()  # persist attempt counters / pending requests before reporting
                raise
            self.workspace_access.check()
            self.store.save()

    def _grant(self, grant_client: str) -> dict | None:
        try:
            return read_grant(self.app_root, grant_client)
        except PermissionError:
            return None

    def _prune(self, d: dict) -> None:
        now = self.clock()
        for k in [k for k, v in d["requests"].items() if v["expires_at"] < now]:
            del d["requests"][k]
        for k in [k for k, v in d["tokens"].items() if not v.get("paired") and v["expires_at"] < now]:
            del d["tokens"][k]

    # ---------------------------------------------------------------- app side
    def auto_token(self, client_name: str, program: str) -> str:
        """Application-owned profile, encrypted at rest; never returned to Skill output."""
        if not program or program == 'unknown':
            raise ValueError('client_unknown')
        profile = self.store.digest(program.lower() + '|' + client_name)
        with self.tx() as d:
            profiles = d.setdefault('auto_profiles', {})
            token = profiles.get(profile)
            stored = d['tokens'].get(self.store.digest(token)) if token else None
            if stored and (stored.get('paired') or stored['expires_at'] >= self.clock()):
                return token
            token = 'nfp_' + secrets.token_urlsafe(24)
            profiles[profile] = token
            d['tokens'][self.store.digest(token)] = {'grant_client': None, 'created_at': self.clock(), 'expires_at': self.clock() + PROMPT_TOKEN_TTL}
            return token

    def issue_token(self, grant_client: str | None = None) -> str:
        """grant_client=None issues an unbound token; the owner picks the scope when approving."""
        if grant_client is not None and self._grant(grant_client) is None:
            raise ValueError("grant_not_enabled")
        token = "nfp_" + secrets.token_urlsafe(24)
        ttl = PAIR_TOKEN_TTL if grant_client else PROMPT_TOKEN_TTL
        with self.tx() as d:
            d["tokens"][self.store.digest(token)] = {"grant_client": grant_client, "created_at": self.clock(),
                                                     "expires_at": self.clock() + ttl}
        return token

    def requests(self) -> list[dict]:
        """Desktop-only discovery; no tokens or verification codes leave the store."""
        with _file_lock(self._lock_path):
            self.store.data = self.store._load()
            return [{"request_id": r["request_id"]} for r in self.store.data.get("requests", {}).values()
                    if r["state"] == "awaiting_review" and r["expires_at"] >= self.clock()]

    def review(self, request_id: str) -> dict:
        with self.tx() as d:
            req = next((r for r in d["requests"].values() if r["request_id"] == request_id
                        and r["state"] == "awaiting_review" and r["expires_at"] >= self.clock()), None)
            if req is None:
                raise ValueError("request_not_awaiting_confirm")
            if not req["program"] or req["program"] == "unknown":
                req["state"] = "denied"
                raise ValueError("program_unknown")
            grant = self._grant(req["grant_client"]) if req["grant_client"] else None
            if req["grant_client"] and grant is None:
                req["state"] = "denied"
                raise ValueError("grant_not_enabled")
            req["review_scope"] = _scope_snapshot(grant) if grant else None
            req["state"] = "awaiting_confirm"
            req["expires_at"] = self.clock() + CONFIRM_TTL
            return {"request_id": req["request_id"], "client_name": req["client_name"], "program": req["program"],
                    "grant_client": req["grant_client"],
                    "scope": grant.get("scope", "selected") if grant else "all_indexed",
                    "allow_sync": grant.get("allow_sync") is True if grant else True,
                    "library_id": grant["library_id"] if grant else None,
                    "conversations": len(grant.get("conversations", [])) if grant else 0}

    def _by_request(self, d: dict, request_id: str) -> tuple[str, dict]:
        for k, v in d["requests"].items():
            if v["request_id"] == request_id and v["state"] == "awaiting_confirm" and v["expires_at"] >= self.clock():
                return k, v
        raise ValueError("request_not_awaiting_confirm")

    def approve(self, request_id: str, grant_client: str | None = None) -> dict:
        with self.tx() as d:
            key, req = self._by_request(d, request_id)
            if req["grant_client"] and grant_client and grant_client != req["grant_client"]:
                raise ValueError("grant_mismatch")
            req["grant_client"] = req["grant_client"] or grant_client
            grant = self._grant(req["grant_client"]) if req["grant_client"] else None
            if grant is None:
                raise ValueError("grant_not_enabled")
            if req.get("review_scope") and _scope_snapshot(grant) != req["review_scope"]:
                raise ValueError("scope_changed")
            binding_id = secrets.token_hex(8)
            d["bindings"][binding_id] = {"binding_key": req["binding_key"], "grant_client": req["grant_client"],
                                         "client_name": req["client_name"], "program": req["program"],
                                         "scope": _scope_snapshot(grant), "created_at": self.clock(),
                                         "last_used_at": None, "revoked": False}
            tok = d["tokens"].get(req["token_digest"])
            if tok:
                tok["paired"] = True
            del d["requests"][key]
            return {"binding_id": binding_id}

    def deny(self, request_id: str) -> None:
        with self.tx() as d:
            _, req = self._by_request(d, request_id)
            req["state"] = "denied"
            req["expires_at"] = self.clock() + CONFIRM_TTL

    def bindings(self) -> list[dict]:
        with self.tx() as d:
            return [{"id": k, "client_name": v["client_name"], "program": v["program"], "grant_client": v["grant_client"],
                     "created_at": v["created_at"], "last_used_at": v["last_used_at"],
                     "active": not v["revoked"] and self._grant(v["grant_client"]) is not None}
                    for k, v in d["bindings"].items() if not v["revoked"]]

    def revoke(self, binding_id: str) -> None:
        with self.tx() as d:
            if binding_id not in d["bindings"]:
                raise ValueError("binding_missing")
            d["bindings"][binding_id]["revoked"] = True

    # ---------------------------------------------------------------- MCP side
    def resolve(self, credential: str, program: str, client_name: str) -> str:
        """Return the approved mcp_access client id, or raise PairingRequired with a user hint."""
        if not program or program == "unknown":
            raise PairingDenied("mcp_access_denied: 无法识别连接程序，请重新连接 MCP。")
        now = self.clock()
        token_digest = self.store.digest(credential)
        binding_key = self.store.digest(token_digest + "|" + program.lower())
        with self.tx() as d:
            self._prune(d)
            for bid, b in d["bindings"].items():
                if b["binding_key"] != binding_key or b["revoked"]:
                    continue
                grant = self._grant(b["grant_client"])
                if grant is None:
                    raise PairingDenied("mcp_access_denied: 该连接已在 Noteford 中撤销或停用。")
                if not _within(grant, b["scope"]):
                    b["revoked"] = True  # scope was widened outside the app: require re-approval
                    break
                if not b["last_used_at"] or now - b["last_used_at"] > LAST_USED_THROTTLE:
                    b["last_used_at"] = now
                return b["grant_client"]
            tok = d["tokens"].get(token_digest)
            if tok is None or (not tok.get("paired") and tok["expires_at"] < now):
                raise PairingDenied("mcp_access_denied: 连接凭据无效或已过期，请通过 Noteford Skill 重新连接。")
            grant_client = tok["grant_client"]
            if grant_client is not None and self._grant(grant_client) is None:
                raise PairingDenied("mcp_access_denied: 该连接已在 Noteford 中撤销或停用。")
            waiting = next((v for v in d["requests"].values() if v["binding_key"] == binding_key
                            and v["state"] in ("awaiting_confirm", "denied")), None)
            if waiting:
                if waiting["state"] == "denied":
                    raise PairingDenied("mcp_access_denied: 用户已拒绝本次连接，请勿重复请求；5 分钟后可重新申请。")
                raise PairingRequired(f"pairing_required: {HINT}。请等待用户确认后再试。")
            existing = next((v for v in d["requests"].values()
                             if v["binding_key"] == binding_key and v["state"] == "awaiting_review"), None)
            if existing is not None:
                if client_name:
                    existing["client_name"] = client_name[:80]
            else:
                request_id = secrets.token_hex(12)
                d["requests"][request_id] = {
                    "request_id": request_id, "binding_key": binding_key, "token_digest": token_digest,
                    "grant_client": grant_client,
                    "client_name": (client_name or Path(program).stem or "未知 AI")[:80],
                    "program": program[:260], "state": "awaiting_review",
                    "created_at": now, "expires_at": now + REQUEST_TTL}
        raise PairingRequired(f"pairing_required: 该 AI 尚未获得授权。{HINT}。无需输入验证码，确认后重试本工具。")



def make_reader(config_path: Path, pairing: LocalPairing | None, credential: str,
                program_provider: Callable[[], str] = parent_executable):
    from .mcp_server import LocalIndexReader

    state: dict[str, Any] = {"client_name": "", "program": None}

    class _Reader(LocalIndexReader):
        @property
        def client_id(self):  # type: ignore[override]
            if pairing is None:
                raise PairingDenied('metadata_only: tool execution is disabled')
            from .mcp_connection_state import require_connected
            try:
                require_connected(config_path)
            except ValueError:
                raise PairingDenied('mcp_connection_changed: 连接已断开或变更，请重新连接后检查状态。') from None
            if state["program"] is None:
                state["program"] = program_provider()
            return pairing.resolve(credential, state["program"], state["client_name"])

        @client_id.setter
        def client_id(self, _value):
            pass

    return _Reader(config_path), state


def open_pairing(config_path: Path, key_provider: Callable[[Path], bytes] | None = None,
                 clock: Callable[[], float] = time.time) -> LocalPairing:
    if key_provider is None:
        from .platform_seam import protected_data_key as key_provider
    config = load_root_config(config_path.resolve())
    app_root = LibraryRegistry(Path(config["project_root"]), config).app_root
    return LocalPairing(app_root, key_provider(app_root / "data" / "mcp_local.key"), clock)


def build_paired_server(config_path: Path, pairing: LocalPairing | None, credential: str,
                        program_provider: Callable[[], str] = parent_executable):
    from .mcp_server import READ_ONLY, create_server
    reader, state = make_reader(config_path, pairing, credential, program_provider)
    server = create_server(config_path, reader=reader)

    @server.tool(annotations=READ_ONLY)
    def request_pairing(client_name: str | None = None) -> dict[str, Any]:
        """Request read-only access. Ask the user to approve the native Noteford dialog; no code entry is needed. Retry only after the user confirms."""
        if client_name:
            state["client_name"] = client_name[:80]
        try:
            reader.client_id
            return {"paired": True}
        except PairingRequired as exc:
            return {"paired": False, "message": str(exc).split(": ", 1)[1]}

    return server, state


def run_local_mcp(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Noteford local MCP (stdio) with owner pairing")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--pair", required=True, help="Pairing token exported by Noteford")
    args = parser.parse_args(argv)
    pairing = open_pairing(args.config)
    server, _ = build_paired_server(args.config, pairing, args.pair)
    server.run(transport="stdio")
    return 0


def run_admin(argv: list[str] | None = None) -> int:
    """JSON CLI used by the desktop app: issue | review | approve | deny | bindings | requests | revoke."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--noninteractive-keychain", action="store_true")
    parser.add_argument("command", choices=["issue", "approve", "deny", "bindings", "requests", "review", "revoke"])
    parser.add_argument("value", nargs="?")
    parser.add_argument("grant", nargs="?")
    args = parser.parse_args(argv)
    key_provider = None
    if sys.platform == 'darwin' and args.noninteractive_keychain:
        config = load_root_config(args.config.resolve())
        if args.command in {'requests', 'bindings'} and not (Path(config['project_root']) / 'data/mcp_local.store').exists():
            sys.stdout.write(json.dumps({'ok': True, 'result': []}))
            return 0  # A background check must not create a new credential store.
        from .platform_seam import MacKeychainSecretProtector, protected_data_key
        protector = MacKeychainSecretProtector(allow_interaction=False)
        key_provider = lambda path: protected_data_key(path, protector=protector)
    try:
        pairing = open_pairing(args.config, key_provider=key_provider)
    except OSError:
        if sys.platform != 'darwin':
            raise
        sys.stdout.write(json.dumps({'ok': False, 'error': 'keychain_access_required'}))
        return 2
    try:
        if args.command == "issue":
            result: Any = {"token": pairing.issue_token(args.value or None)}
        elif args.command == "requests":
            result = pairing.requests()
        elif args.command == "review":
            result = pairing.review(args.value)
        elif args.command == "approve":
            result = pairing.approve(args.value, args.grant)
        elif args.command == "deny":
            pairing.deny(args.value); result = {"ok": True}
        elif args.command == "bindings":
            result = pairing.bindings()
        else:
            pairing.revoke(args.value); result = {"ok": True}
        sys.stdout.write(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
        return 0
    except ValueError as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": str(exc)}))
        return 2
