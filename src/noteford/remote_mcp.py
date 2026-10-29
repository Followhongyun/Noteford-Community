"""Owner-approved remote MCP: streamable HTTP + OAuth 2.1 (authorization code, PKCE, dynamic registration).

Flow: the external AI registers (DCR) and opens /authorize. The browser lands on a
Noteford consent page that asks for a short verification code generated inside the
desktop app (Settings -> AI connections). A correct code moves the request to
"awaiting_confirm"; the desktop app then shows the AI name and the user picks an
existing read-only connection (conversation whitelist from mcp_access.json). Only
then is an authorization code issued. All MCP tools stay read-only.

Storage: data/mcp_remote.store is AES-256-GCM encrypted. Windows wraps the data
key with DPAPI (data/mcp_remote.key). macOS stores that key in the login keychain
and leaves only a marker on disk. Tokens, refresh tokens, codes and verification
codes are kept only as keyed HMAC-SHA256 digests. Nothing secret is logged.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
                                      RefreshToken, RegistrationError, TokenError, construct_redirect_uri)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .libraries import LibraryRegistry, load_root_config
from .mcp_access import read_grant
from .mcp_server import LocalIndexReader, create_server

log = logging.getLogger("noteford.remote_mcp")

SCOPE = "wechat.read"
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
AUTH_CODE_TTL = 300
REQUEST_TTL = 900
PAIR_CODE_TTL = 300
PAIR_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L
PAIR_CODE_LEN = 8
MAX_CODE_ATTEMPTS_PER_REQUEST = 5
MAX_CODE_FAILURES_GLOBAL = 10
MAX_CLIENTS = 200
MAX_PENDING = 50


# ---------------------------------------------------------------- key protection
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    entropy_raw = b"Noteford remote MCP store v1"
    ebuf = ctypes.create_string_buffer(entropy_raw, len(entropy_raw))
    entropy = BLOB(len(entropy_raw), ctypes.cast(ebuf, ctypes.POINTER(ctypes.c_char)))
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    args = ((ctypes.byref(blob_in), "Noteford Community", ctypes.byref(entropy), None, None, 0x1, ctypes.byref(blob_out))
            if protect else
            (ctypes.byref(blob_in), None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(blob_out)))
    if not fn(*args):
        raise OSError("DPAPI operation failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def dpapi_key_provider(key_file: Path) -> bytes:
    """Return the 32-byte data key, creating it on first use. Only the DPAPI blob touches disk."""
    if sys.platform != "win32":
        raise RuntimeError("remote MCP storage requires Windows DPAPI")
    if key_file.exists():
        key = _dpapi(key_file.read_bytes(), protect=False)
        if len(key) != 32:
            raise ValueError("invalid remote MCP key")
        return key
    key = AESGCM.generate_key(bit_length=256)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = key_file.with_suffix(f".{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(_dpapi(key, protect=True))
    os.replace(tmp, key_file)
    return key


# ---------------------------------------------------------------- encrypted store
class RemoteStore:
    """Single encrypted JSON document; written atomically on every change."""

    def __init__(self, path: Path, key: bytes):
        self.path = path
        from .workspace_access import WorkspaceAccess
        self.workspace_access = WorkspaceAccess(path.parent.parent)
        self._aead = AESGCM(key)
        self._mac_key = hashlib.sha256(b"noteford-mac|" + key).digest()
        self._lock = threading.RLock()
        self.data = self._load()

    def digest(self, secret: str) -> str:
        return hmac.new(self._mac_key, secret.encode(), hashlib.sha256).hexdigest()

    def _empty(self) -> dict:
        return {"version": 1, "clients": {}, "requests": {}, "codes": {}, "tokens": {},
                "pair": None, "pair_failures": 0, "grants": {}}

    def _load(self) -> dict:
        self.workspace_access.check()
        if not self.path.exists():
            return self._empty()
        raw = self.path.read_bytes()
        if raw[:4] != b"NFR1":
            raise ValueError("unknown remote MCP store format")
        plain = self._aead.decrypt(raw[4:16], raw[16:], b"noteford-remote-mcp")
        return json.loads(plain)

    def save(self) -> None:
        self.workspace_access.check()
        with self._lock:
            nonce = secrets.token_bytes(12)
            blob = b"NFR1" + nonce + self._aead.encrypt(nonce, json.dumps(self.data).encode(), b"noteford-remote-mcp")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".{secrets.token_hex(4)}.tmp")
            tmp.write_bytes(blob)
            os.replace(tmp, self.path)

    def prune(self, now: float) -> None:
        d = self.data
        for table in ("requests", "codes", "tokens"):
            for k in [k for k, v in d[table].items() if v.get("expires_at", now + 1) < now]:
                del d[table][k]
        if d["pair"] and d["pair"]["expires_at"] < now:
            d["pair"] = None


# ---------------------------------------------------------------- OAuth provider
class NotefordOAuthProvider:
    def __init__(self, store: RemoteStore, app_root: Path, public_url: str,
                 clock: Callable[[], float] = time.time):
        self.store, self.app_root, self.public_url, self.clock = store, app_root, public_url.rstrip("/"), clock

    # -- helpers
    def _grant_ok(self, grant_client: str | None) -> bool:
        try:
            read_grant(self.app_root, grant_client)
            return True
        except PermissionError:
            return False

    def _issue(self, client_id: str, grant_client: str, scopes: list[str], resource: str | None,
               family: str | None = None) -> OAuthToken:
        now, s = self.clock(), self.store
        family = family or secrets.token_hex(8)
        access, refresh = "nfa_" + secrets.token_urlsafe(32), "nfr_" + secrets.token_urlsafe(32)
        base = {"client_id": client_id, "grant_client": grant_client, "scopes": scopes,
                "resource": resource, "family": family, "created_at": now}
        s.data["tokens"][s.digest(access)] = {**base, "kind": "access", "expires_at": now + ACCESS_TTL}
        s.data["tokens"][s.digest(refresh)] = {**base, "kind": "refresh", "expires_at": now + REFRESH_TTL}
        s.data["grants"].setdefault(family, {"client_id": client_id, "grant_client": grant_client,
                                             "client_name": self._client_name(client_id), "created_at": now})
        s.data["grants"][family]["last_issued_at"] = now
        s.save()
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          refresh_token=refresh, scope=" ".join(scopes))

    def _client_name(self, client_id: str) -> str:
        info = self.store.data["clients"].get(client_id) or {}
        return (info.get("client_name") or "未命名 AI")[:80]

    # -- DCR
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = self.store.data["clients"].get(client_id)
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if len(self.store.data["clients"]) >= MAX_CLIENTS:
            raise RegistrationError("invalid_client_metadata", "too many registered clients")
        for uri in client_info.redirect_uris or []:
            parsed = urlparse(str(uri))
            if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
                raise RegistrationError("invalid_redirect_uri", "redirect_uri must be https or loopback")
        self.store.data["clients"][client_info.client_id] = client_info.model_dump(mode="json")
        self.store.save()

    # -- authorization
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        now = self.clock()
        self.store.prune(now)
        if len(self.store.data["requests"]) >= MAX_PENDING:
            raise AuthorizeError("temporarily_unavailable", "too many pending authorization requests")
        request_id = secrets.token_urlsafe(24)
        self.store.data["requests"][self.store.digest(request_id)] = {
            "client_id": client.client_id, "client_name": self._client_name(client.client_id),
            "params": params.model_dump(mode="json"), "state": "awaiting_code", "attempts": 0,
            "created_at": now, "expires_at": now + REQUEST_TTL, "display_id": secrets.token_hex(6)}
        self.store.save()
        return f"{self.public_url}/oauth/consent?request={request_id}"

    def _request(self, request_id: str) -> dict | None:
        req = self.store.data["requests"].get(self.store.digest(request_id or ""))
        return req if req and req["expires_at"] >= self.clock() else None

    def new_pair_code(self) -> dict:
        now = self.clock()
        code = "".join(secrets.choice(PAIR_CODE_ALPHABET) for _ in range(PAIR_CODE_LEN))
        self.store.data["pair"] = {"digest": self.store.digest(code), "expires_at": now + PAIR_CODE_TTL}
        self.store.data["pair_failures"] = 0
        self.store.save()
        return {"code": code, "expires_at": now + PAIR_CODE_TTL}  # shown once, in-app only

    def submit_code(self, request_id: str, code: str) -> dict:
        now, s = self.clock(), self.store
        req = self._request(request_id)
        if req is None:
            return {"ok": False, "error": "request_expired"}
        if req["state"] != "awaiting_code":
            return {"ok": False, "error": "already_submitted"}
        pair = s.data["pair"]
        normalized = "".join(ch for ch in (code or "").upper() if ch.isalnum())
        good = bool(pair) and pair["expires_at"] >= now and hmac.compare_digest(pair["digest"], s.digest(normalized))
        if not good:
            req["attempts"] += 1
            s.data["pair_failures"] += 1
            if req["attempts"] >= MAX_CODE_ATTEMPTS_PER_REQUEST:
                req["state"] = "denied"
            if s.data["pair_failures"] >= MAX_CODE_FAILURES_GLOBAL:
                s.data["pair"] = None  # brute-force guard: force a fresh code in the app
            s.save()
            return {"ok": False, "error": "invalid_code",
                    "remaining": max(0, MAX_CODE_ATTEMPTS_PER_REQUEST - req["attempts"])}
        s.data["pair"] = None  # one-time
        req["state"] = "awaiting_confirm"
        req["code_verified_at"] = now
        s.save()
        return {"ok": True}

    def pending(self) -> list[dict]:
        self.store.prune(self.clock())
        return [{"display_id": r["display_id"], "client_name": r["client_name"], "created_at": r["created_at"],
                 "expires_at": r["expires_at"]}
                for r in self.store.data["requests"].values() if r["state"] == "awaiting_confirm"]

    def _by_display(self, display_id: str) -> dict | None:
        for r in self.store.data["requests"].values():
            if r["display_id"] == display_id and r["expires_at"] >= self.clock():
                return r
        return None

    def approve(self, display_id: str, grant_client: str) -> None:
        req = self._by_display(display_id)
        if req is None or req["state"] != "awaiting_confirm":
            raise ValueError("request_not_awaiting_confirm")
        if not self._grant_ok(grant_client):
            raise ValueError("grant_not_enabled")
        params = AuthorizationParams.model_validate(req["params"])
        code = secrets.token_urlsafe(32)
        self.store.data["codes"][self.store.digest(code)] = {
            "client_id": req["client_id"], "grant_client": grant_client, "params": req["params"],
            "expires_at": self.clock() + AUTH_CODE_TTL}
        req["state"] = "approved"
        req["redirect"] = construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)
        self.store.save()

    def deny(self, display_id: str) -> None:
        req = self._by_display(display_id)
        if req is None:
            raise ValueError("request_missing")
        req["state"] = "denied"
        self.store.save()

    def poll(self, request_id: str) -> dict:
        req = self._request(request_id)
        if req is None:
            return {"state": "expired"}
        if req["state"] == "approved" and req.get("redirect"):
            redirect = req.pop("redirect")  # deliver once
            req["state"] = "delivered"
            self.store.save()
            return {"state": "approved", "redirect": redirect}
        if req["state"] == "denied":
            p = AuthorizationParams.model_validate(req["params"])
            return {"state": "denied", "redirect": construct_redirect_uri(
                str(p.redirect_uri), error="access_denied", state=p.state)}
        return {"state": req["state"]}

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        row = self.store.data["codes"].get(self.store.digest(authorization_code))
        if not row or row["client_id"] != client.client_id or row["expires_at"] < self.clock():
            return None
        p = row["params"]
        return AuthorizationCode(code=authorization_code, scopes=p.get("scopes") or [SCOPE],
                                 expires_at=row["expires_at"], client_id=row["client_id"],
                                 code_challenge=p["code_challenge"], redirect_uri=p["redirect_uri"],
                                 redirect_uri_provided_explicitly=p["redirect_uri_provided_explicitly"],
                                 resource=p.get("resource"), subject=row["grant_client"])

    async def exchange_authorization_code(self, client, authorization_code) -> OAuthToken:
        row = self.store.data["codes"].pop(self.store.digest(authorization_code.code), None)  # single use
        if row is None:
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(client.client_id, row["grant_client"], authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token: str):
        row = self.store.data["tokens"].get(self.store.digest(refresh_token))
        if (not row or row["kind"] != "refresh" or row["client_id"] != client.client_id
                or row["expires_at"] < self.clock() or not self._grant_ok(row["grant_client"])):
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=row["scopes"],
                            expires_at=int(row["expires_at"]), resource=row["resource"], subject=row["grant_client"])

    async def exchange_refresh_token(self, client, refresh_token, scopes: list[str]) -> OAuthToken:
        row = self.store.data["tokens"].get(self.store.digest(refresh_token.token))
        if row is None:
            raise TokenError("invalid_grant", "refresh token revoked")
        family = row["family"]
        self._revoke_family(family, keep_grant=True)  # rotate
        return self._issue(client.client_id, row["grant_client"], scopes or row["scopes"], row["resource"], family)

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self.store.data["tokens"].get(self.store.digest(token))
        if not row or row["kind"] != "access" or row["expires_at"] < self.clock():
            return None
        if not self._grant_ok(row["grant_client"]):  # in-app revocation of the connection wins
            return None
        return AccessToken(token=token, client_id=row["client_id"], scopes=row["scopes"],
                           expires_at=int(row["expires_at"]), resource=row["resource"], subject=row["grant_client"])

    def _revoke_family(self, family: str, keep_grant: bool = False) -> None:
        tokens = self.store.data["tokens"]
        for k in [k for k, v in tokens.items() if v["family"] == family]:
            del tokens[k]
        if not keep_grant:
            self.store.data["grants"].pop(family, None)
        self.store.save()

    async def revoke_token(self, token) -> None:
        row = self.store.data["tokens"].get(self.store.digest(token.token))
        if row:
            self._revoke_family(row["family"])

    def list_grants(self) -> list[dict]:
        now = self.clock()
        out = []
        for family, g in self.store.data["grants"].items():
            live = [t for t in self.store.data["tokens"].values() if t["family"] == family and t["expires_at"] >= now]
            out.append({"id": family, "client_name": g["client_name"], "grant_client": g["grant_client"],
                        "created_at": g["created_at"], "active": bool(live) and self._grant_ok(g["grant_client"])})
        return out

    def revoke_grant(self, family: str) -> None:
        if family not in self.store.data["grants"]:
            raise ValueError("grant_missing")
        self._revoke_family(family)

    async def exchange_identity_assertion(self, client, params):
        raise TokenError("unsupported_grant_type", "not supported")


# ---------------------------------------------------------------- reader bound to the token
class RemoteIndexReader(LocalIndexReader):
    """Resolves the mcp_access grant from the current request's access token."""

    @property
    def client_id(self) -> str | None:  # type: ignore[override]
        token = get_access_token()
        return token.subject if token is not None else None

    @client_id.setter
    def client_id(self, _value) -> None:
        pass


# ---------------------------------------------------------------- HTTP apps
CONSENT_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>连接 Noteford</title>
<style>body{font-family:system-ui,'Microsoft YaHei',sans-serif;background:#f5f7f5;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}
.card{background:#fff;border-radius:16px;box-shadow:0 8px 30px #0001;padding:32px;max-width:420px;width:90%}
h1{font-size:20px;margin:0 0 8px}p{color:#555;line-height:1.6}input{font-size:24px;letter-spacing:6px;text-transform:uppercase;width:100%;box-sizing:border-box;padding:12px;border:1px solid #ccd;border-radius:10px;text-align:center}
button{margin-top:16px;width:100%;padding:12px;font-size:16px;border:0;border-radius:10px;background:#2f8f5b;color:#fff;cursor:pointer}
#msg{margin-top:12px;min-height:1.4em;color:#b33}</style></head><body><div class="card">
<h1>「__CLIENT__」请求读取你的 Noteford</h1>
<p>打开 Noteford 桌面端 → <b>设置 → AI 连接 → 远程连接</b>，点「生成验证码」，把验证码填到下面。验证通过后，还需要你在 Noteford 里确认可读的会话范围。</p>
<form id="f"><input id="c" maxlength="9" autocomplete="one-time-code" autofocus placeholder="XXXX-XXXX"><button>验证</button></form>
<div id="msg"></div></div><script>
const rid=new URLSearchParams(location.search).get('request');const m=document.getElementById('msg');
async function poll(){const r=await fetch('/oauth/consent/status?request='+encodeURIComponent(rid));const j=await r.json();
if(j.redirect){location.href=j.redirect;return}if(j.state==='expired'){m.textContent='请求已过期，请回到 AI 重新发起连接';return}
if(j.state==='awaiting_confirm'){m.style.color='#2f8f5b';m.textContent='验证码正确，请在 Noteford 桌面端确认授权…'}setTimeout(poll,2000)}
document.getElementById('f').onsubmit=async e=>{e.preventDefault();const r=await fetch('/oauth/consent/code',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({request:rid,code:document.getElementById('c').value})});
const j=await r.json();if(j.ok){document.getElementById('f').style.display='none';poll()}else m.textContent=j.error==='invalid_code'?('验证码不正确或已过期，还可尝试 '+j.remaining+' 次'):'请求已失效，请回到 AI 重新发起连接'};
</script></body></html>"""


def build_public_app(config_path: Path, provider: NotefordOAuthProvider, public_url: str) -> Starlette:
    public_url = public_url.rstrip("/")
    reader = RemoteIndexReader(config_path)
    server = create_server(
        config_path, reader=reader, auth_server_provider=provider,
        auth=AuthSettings(issuer_url=public_url, resource_server_url=f"{public_url}/mcp",
                          validate_token_resource=False, required_scopes=[SCOPE],
                          client_registration_options=ClientRegistrationOptions(
                              enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
                          revocation_options=RevocationOptions(enabled=True)))

    @server.custom_route("/oauth/consent", methods=["GET"])
    async def consent(request: Request) -> Response:
        req = provider._request(request.query_params.get("request", ""))
        if req is None:
            return HTMLResponse("<p>授权请求不存在或已过期，请回到 AI 重新发起连接。</p>", status_code=404)
        return HTMLResponse(CONSENT_HTML.replace("__CLIENT__", html.escape(req["client_name"])),
                            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                     "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'"})

    @server.custom_route("/oauth/consent/code", methods=["POST"])
    async def consent_code(request: Request) -> Response:
        try:
            body = await request.json()
            result = provider.submit_code(str(body.get("request", "")), str(body.get("code", ""))[:16])
        except (ValueError, AttributeError):
            result = {"ok": False, "error": "bad_request"}
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @server.custom_route("/oauth/consent/status", methods=["GET"])
    async def consent_status(request: Request) -> Response:
        return JSONResponse(provider.poll(request.query_params.get("request", "")), headers={"Cache-Control": "no-store"})

    host = urlparse(public_url).netloc
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=[host, "127.0.0.1:*", "localhost:*"],
                                         allowed_origins=[public_url, "http://127.0.0.1:*", "http://localhost:*"])
    return server.streamable_http_app(transport_security=security)


def build_admin_app(provider: NotefordOAuthProvider, admin_secret: str) -> Starlette:
    """Loopback-only control API used by the desktop app. Never routed through the tunnel."""

    def guard(request: Request) -> bool:
        presented = request.headers.get("x-noteford-admin", "")
        return bool(presented) and hmac.compare_digest(presented, admin_secret)

    def endpoint(fn):
        async def handler(request: Request) -> Response:
            if not guard(request):
                return JSONResponse({"error": "forbidden"}, status_code=403)
            try:
                raw = await request.body() if request.method == "POST" else b""
                body = json.loads(raw) if raw else {}
                if not isinstance(body, dict):
                    raise ValueError("bad_request")
                return JSONResponse(fn(body) if fn.__code__.co_argcount else fn(), headers={"Cache-Control": "no-store"})
            except (ValueError, KeyError) as exc:
                return JSONResponse({"error": type(exc).__name__ if isinstance(exc, KeyError) else str(exc)}, status_code=400)
        return handler

    def ok(fn):
        def run(body):
            fn(body)
            return {"ok": True}
        return run

    return Starlette(routes=[
        Route("/admin/pair-code", endpoint(lambda: provider.new_pair_code()), methods=["POST"]),
        Route("/admin/pending", endpoint(lambda: provider.pending()), methods=["GET"]),
        Route("/admin/approve", endpoint(ok(lambda b: provider.approve(str(b["display_id"]), str(b["grant_client"])))), methods=["POST"]),
        Route("/admin/deny", endpoint(ok(lambda b: provider.deny(str(b["display_id"])))), methods=["POST"]),
        Route("/admin/grants", endpoint(lambda: provider.list_grants()), methods=["GET"]),
        Route("/admin/revoke", endpoint(ok(lambda b: provider.revoke_grant(str(b["id"])))), methods=["POST"]),
        Route("/admin/health", endpoint(lambda: {"ok": True}), methods=["GET"]),
    ])


def open_provider(config_path: Path, public_url: str, key_provider: Callable[[Path], bytes] | None = None):
    if key_provider is None:
        from .platform_seam import protected_data_key as key_provider
    config = load_root_config(config_path.resolve())
    app_root = LibraryRegistry(Path(config["project_root"]), config).app_root
    data = app_root / "data"
    store = RemoteStore(data / "mcp_remote.store", key_provider(data / "mcp_remote.key"))
    return NotefordOAuthProvider(store, app_root, public_url)


def run_remote(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Noteford remote MCP (streamable HTTP + OAuth)")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--public-url", required=True, help="Public HTTPS origin, e.g. https://noteford.example.com")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--admin-port", type=int, default=8766)
    args = parser.parse_args(argv)
    admin_secret = os.environ.pop("NOTEFORD_REMOTE_ADMIN_SECRET", "")
    if len(admin_secret) < 32:
        parser.error("NOTEFORD_REMOTE_ADMIN_SECRET (>=32 chars) must be provided by the desktop app")
    if not args.public_url.startswith("https://"):
        parser.error("--public-url must be https")
    import uvicorn
    provider = open_provider(args.config, args.public_url)
    public = uvicorn.Server(uvicorn.Config(build_public_app(args.config, provider, args.public_url),
                                           host="127.0.0.1", port=args.port, access_log=False, log_level="warning"))
    admin = uvicorn.Server(uvicorn.Config(build_admin_app(provider, admin_secret),
                                          host="127.0.0.1", port=args.admin_port, access_log=False, log_level="warning"))

    async def main() -> None:
        await asyncio.gather(public.serve(), admin.serve())

    asyncio.run(main())
    return 0


if __name__ == "__main__":
    raise SystemExit(run_remote())
