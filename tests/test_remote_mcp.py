import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from noteford.libraries import LibraryRegistry
from noteford.remote_mcp import (NotefordOAuthProvider, RemoteStore, build_admin_app,
                                            build_public_app, PAIR_CODE_TTL)
from test_mcp_server import _indexed_account

URL = "https://nf.test"
REDIRECT = "https://client.test/cb"
ADMIN = "a" * 40


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def env(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    wechat = tmp_path / "wechat"
    for name in ("session", "contact"):
        folder = wechat / "acct_a" / "db_storage" / name
        folder.mkdir(parents=True)
        (folder / f"{name}.db").write_bytes(b"fixture")
    cfg = {"project_root": str(app), "wechat_roots": [str(wechat)]}
    config = app / "config.local.json"
    config.write_text(json.dumps(cfg))
    registry = LibraryRegistry(app, cfg)
    registry.discover(wechat)
    lib = _indexed_account(app, registry, "acct_a", "REMOTE-SECRET-TEXT")
    access = app / "data" / "mcp_access.json"
    access.write_text(json.dumps({"version": 1, "clients": {
        "g1": {"enabled": True, "library_id": lib.id, "conversations": ["group@chatroom"]},
        "g2": {"enabled": True, "library_id": lib.id, "conversations": ["someone-else"]},
    }}))
    clock = Clock()
    store = RemoteStore(app / "data" / "mcp_remote.store", secrets.token_bytes(32))
    provider = NotefordOAuthProvider(store, app, URL, clock=clock)
    public = TestClient(build_public_app(config, provider, URL), base_url=URL)
    admin = TestClient(build_admin_app(provider, ADMIN), base_url="http://127.0.0.1:8766")
    with public:
        yield dict(app=app, lib=lib, access=access, clock=clock, provider=provider, public=public,
                   admin=admin, store_path=app / "data" / "mcp_remote.store")


def adm(env, method, path, body=None):
    r = env["admin"].request(method, path, json=body, headers={"x-noteford-admin": ADMIN})
    return r


def start_authorize(env):
    c = env["public"]
    reg = c.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Grok Bot",
                                    "token_endpoint_auth_method": "none",
                                    "grant_types": ["authorization_code", "refresh_token"]})
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = c.get("/authorize", params={"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
                                    "code_challenge": challenge, "code_challenge_method": "S256",
                                    "state": "st", "scope": "wechat.read"}, follow_redirects=False)
    assert r.status_code == 302, r.text
    loc = urlparse(r.headers["location"])
    assert loc.path == "/oauth/consent"
    rid = parse_qs(loc.query)["request"][0]
    assert c.get("/oauth/consent", params={"request": rid}).status_code == 200
    return client_id, verifier, rid


def submit(env, rid, code):
    return env["public"].post("/oauth/consent/code", json={"request": rid, "code": code}).json()


def finish(env, client_id, verifier, rid, grant):
    code = adm(env, "POST", "/admin/pair-code").json()["code"]
    assert submit(env, rid, code)["ok"] is True
    pending = adm(env, "GET", "/admin/pending").json()
    assert [p["client_name"] for p in pending] == ["Grok Bot"]
    assert adm(env, "POST", "/admin/approve", {"display_id": pending[0]["display_id"], "grant_client": grant}).json() == {"ok": True}
    status = env["public"].get("/oauth/consent/status", params={"request": rid}).json()
    assert status["state"] == "approved"
    q = parse_qs(urlparse(status["redirect"]).query)
    assert q["state"] == ["st"]
    form = {"grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
            "client_id": client_id, "code_verifier": verifier}
    tok = env["public"].post("/token", data=form)
    assert tok.status_code == 200, tok.text
    assert env["public"].post("/token", data=form).status_code == 400  # code is single-use
    return tok.json()


def rpc(env, token, method, params, sid=None, rid=1):
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    if token:
        headers["authorization"] = f"Bearer {token}"
    if sid:
        headers["mcp-session-id"] = sid
    r = env["public"].post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    return r


def payload(r):
    if r.headers.get("content-type", "").startswith("application/json"):
        return r.json()
    for line in r.text.splitlines():
        if line.startswith("data:") and line[5:].strip():
            return json.loads(line[5:])
    raise AssertionError(r.text)


def call_read(env, token):
    init = rpc(env, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "t", "version": "1"}})
    if init.status_code != 200:
        return init.status_code, None
    sid = init.headers.get("mcp-session-id")
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json",
               "authorization": f"Bearer {token}"}
    if sid:
        headers["mcp-session-id"] = sid
    env["public"].post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    r = rpc(env, token, "tools/call", {"name": "read_messages", "arguments": {
        "library_id": env["lib"].id, "conversation_id": "group@chatroom"}}, sid=sid, rid=2)
    if r.status_code != 200:
        return r.status_code, None
    return 200, payload(r)


def test_unauthorized_rejected_and_admin_guarded(env):
    assert rpc(env, None, "initialize", {}).status_code == 401
    assert rpc(env, "nfa_forged", "initialize", {}).status_code == 401
    assert env["admin"].post("/admin/pair-code").status_code == 403
    assert env["admin"].post("/admin/pair-code", headers={"x-noteford-admin": "wrong"}).status_code == 403
    meta = env["public"].get("/.well-known/oauth-authorization-server").json()
    assert "S256" in meta["code_challenge_methods_supported"] and meta["registration_endpoint"]


def test_remote_tools_no_longer_expose_group_safety(env):
    client_id, verifier, rid = start_authorize(env)
    token = finish(env, client_id, verifier, rid, 'g1')['access_token']
    init = rpc(env, token, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                        'clientInfo': {'name': 'retired-feature-test', 'version': '1'}})
    sid = init.headers.get('mcp-session-id')
    tools = payload(rpc(env, token, 'tools/list', {}, sid=sid, rid=2))['result']['tools']
    names = {tool['name'] for tool in tools}
    assert {'read_messages', 'search_messages', 'read_changes', 'get_message'} <= names
    assert not {'list_monitored_groups', 'read_monitor_results', 'get_monitor_result'} & names
    result = payload(rpc(env, token, 'tools/call', {
        'name': 'read_monitor_results',
        'arguments': {'library_id': env['lib'].id, 'conversation_id': 'group@chatroom'},
    }, sid=sid, rid=3))['result']
    assert result['isError'] and 'REMOTE-SECRET-TEXT' not in json.dumps(result)


def test_code_wrong_expired_reused_and_bruteforce(env):
    client_id, verifier, rid = start_authorize(env)
    code = adm(env, "POST", "/admin/pair-code").json()["code"]
    assert len(code) == 8
    assert submit(env, rid, "AAAAAAAA")["error"] == "invalid_code"
    env["clock"].now += PAIR_CODE_TTL + 1
    assert submit(env, rid, code)["error"] == "invalid_code"  # expired
    code = adm(env, "POST", "/admin/pair-code").json()["code"]
    assert submit(env, rid, code.lower()[:4] + "-" + code.lower()[4:])["ok"] is True  # tolerant input
    _, _, rid2 = start_authorize(env)
    assert submit(env, rid2, code)["error"] == "invalid_code"  # one-time
    # approve is impossible before the code was verified
    assert adm(env, "POST", "/admin/pending").status_code == 405
    for _ in range(4):
        submit(env, rid2, "BBBBBBBB")
    assert env["public"].get("/oauth/consent/status", params={"request": rid2}).json()["state"] == "denied"
    fresh = adm(env, "POST", "/admin/pair-code").json()["code"]
    assert submit(env, rid2, fresh)["ok"] is False  # locked request stays locked


def test_authorized_read_then_revoked(env):
    client_id, verifier, rid = start_authorize(env)
    tok = finish(env, client_id, verifier, rid, "g1")
    status, body = call_read(env, tok["access_token"])
    assert status == 200 and "REMOTE-SECRET-TEXT" in json.dumps(body, ensure_ascii=False), body
    # refresh rotates; old refresh dies
    form = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": client_id}
    new = env["public"].post("/token", data=form)
    assert new.status_code == 200, new.text
    assert env["public"].post("/token", data=form).status_code == 400
    access = new.json()["access_token"]
    assert call_read(env, tok["access_token"])[0] == 401
    assert call_read(env, access)[0] == 200
    grants = adm(env, "GET", "/admin/grants").json()
    assert grants[0]["client_name"] == "Grok Bot" and grants[0]["active"] is True
    # in-app revoke of the remote authorization
    assert adm(env, "POST", "/admin/revoke", {"id": grants[0]["id"]}).json() == {"ok": True}
    assert call_read(env, access)[0] == 401


def test_disabling_connection_kills_token(env):
    client_id, verifier, rid = start_authorize(env)
    tok = finish(env, client_id, verifier, rid, "g1")
    data = json.loads(env["access"].read_text())
    data["clients"]["g1"]["enabled"] = False
    env["access"].write_text(json.dumps(data))
    assert call_read(env, tok["access_token"])[0] == 401


def test_out_of_scope_conversation_denied(env):
    client_id, verifier, rid = start_authorize(env)
    tok = finish(env, client_id, verifier, rid, "g2")
    status, body = call_read(env, tok["access_token"])
    assert status == 200
    text = json.dumps(body, ensure_ascii=False)
    assert body["result"]["isError"] is True and "REMOTE-SECRET-TEXT" not in text


def test_store_has_no_plaintext(env):
    client_id, verifier, rid = start_authorize(env)
    tok = finish(env, client_id, verifier, rid, "g1")
    pair = adm(env, "POST", "/admin/pair-code").json()["code"]
    raw = env["store_path"].read_bytes()
    for needle in (tok["access_token"], tok["refresh_token"], client_id, "Grok Bot", pair, rid, REDIRECT, "REMOTE-SECRET-TEXT"):
        assert needle.encode() not in raw
    assert raw[:4] == b"NFR1"


def test_dpapi_key_roundtrip(tmp_path):
    import sys
    if sys.platform != "win32":
        pytest.skip("Windows only")
    from noteford.remote_mcp import dpapi_key_provider
    k = dpapi_key_provider(tmp_path / "k")
    assert k == dpapi_key_provider(tmp_path / "k") and k not in (tmp_path / "k").read_bytes()
