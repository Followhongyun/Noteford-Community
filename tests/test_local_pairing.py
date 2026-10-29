import asyncio
import json
import secrets

import pytest
from mcp import Client

from noteford.libraries import LibraryRegistry
from noteford.local_pairing import REQUEST_TTL, LocalPairing, build_paired_server
from test_mcp_server import _indexed_account


class Clock:
    now = 1_800_000_000.0

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
    lib = _indexed_account(app, registry, "acct_a", "LOCAL-SECRET-TEXT")
    access = app / "data" / "mcp_access.json"
    access.write_text(json.dumps({"version": 1, "clients": {
        "g1": {"enabled": True, "library_id": lib.id, "scope": "selected", "conversations": ["group@chatroom"], "label": "Codex"},
    }}))
    clock = Clock()
    pairing = LocalPairing(app, secrets.token_bytes(32), clock)
    return dict(config=config, lib=lib, access=access, clock=clock, pairing=pairing, app=app)


def call(env, credential, program="C:/Apps/Codex/codex.exe", tool="list_conversations"):
    server, _ = build_paired_server(env["config"], env["pairing"], credential, lambda: program)

    async def go():
        async with Client(server) as client:
            if tool == "request_pairing":
                return (await client.call_tool("request_pairing", {"client_name": "Codex"})).structured_content
            result = await client.call_tool("read_messages", {"library_id": env["lib"].id, "conversation_id": "group@chatroom"})
            text = json.dumps(result.structured_content or [c.text for c in result.content if hasattr(c, "text")], ensure_ascii=False)
            return result.is_error, text
    return asyncio.run(go())


def review(env, token):
    call(env, token)
    return env['pairing'].review(env['pairing'].requests()[0]['request_id'])


def test_native_approval_single_use_and_no_duplicate_prompt(env):
    p=env['pairing']; token=p.issue_token('g1')
    err,text=call(env,token)
    assert err and 'LOCAL-SECRET-TEXT' not in text
    first=p.requests()
    call(env,token)
    assert p.requests()==first
    req=p.review(first[0]['request_id'])
    assert req['program']=='C:/Apps/Codex/codex.exe'
    assert req['library_id']==env['lib'].id
    assert call(env,token)[0] and p.requests()==[]
    with pytest.raises(ValueError): p.review(req['request_id'])
    p.approve(req['request_id'])
    assert not call(env,token)[0]
    with pytest.raises(ValueError): p.approve(req['request_id'])


def test_deny_expiry_and_unknown_program(env):
    p=env['pairing'];token=p.issue_token('g1')
    assert '无法识别' in call(env,token,program='unknown')[1]
    assert p.requests()==[]
    req=review(env,token);p.deny(req['request_id'])
    assert '用户已拒绝' in call(env,token)[1] and p.requests()==[]
    env['clock'].now+=REQUEST_TTL+1
    req=review(env,token);env['clock'].now+=REQUEST_TTL+1
    with pytest.raises(ValueError): p.approve(req['request_id'])
    assert call(env,token)[0]


def test_revoke_scope_and_program_boundaries(env):
    p=env['pairing'];token=p.issue_token('g1')
    p.approve(review(env,token)['request_id'])
    assert not call(env,token)[0]
    assert call(env,token,program='C:/Other/evil.exe')[0]
    access=json.loads(env['access'].read_text());access['clients']['g1']['scope']='all_synced'
    env['access'].write_text(json.dumps(access))
    assert call(env,token)[0]
    access['clients']['g1']['scope']='selected';env['access'].write_text(json.dumps(access))
    request=next(x for x in p.requests() if x)
    # Select the original caller's pending request without trusting caller labels.
    with p.tx() as data:
        request_id=next(r['request_id'] for r in data['requests'].values() if r['program']=='C:/Apps/Codex/codex.exe')
    p.approve(p.review(request_id)['request_id'])
    p.revoke(next(b['id'] for b in p.bindings() if b['active']))
    assert call(env,token)[0]


def test_changed_scope_during_confirmation_rejected(env):
    p=env['pairing'];token=p.issue_token('g1');req=review(env,token)
    access=json.loads(env['access'].read_text());access['clients']['g1']['scope']='all_synced'
    env['access'].write_text(json.dumps(access))
    with pytest.raises(ValueError,match='scope_changed'): p.approve(req['request_id'])
    assert p.bindings()==[]


def test_unbound_token_requires_grant_and_persists_after_approval(env):
    p=env['pairing'];token=p.issue_token();req=review(env,token)
    with pytest.raises(ValueError,match='grant_not_enabled'):p.approve(req['request_id'])
    p.approve(req['request_id'],'g1');env['clock'].now+=2*86400
    assert not call(env,token)[0]
    assert call(env,'g1')[0]  # old client IDs are never accepted
    assert call(env,'nfp_forged')[0]


def test_store_encrypted_and_discovery_read_only(env):
    p=env['pairing'];token=p.issue_token('g1');call(env,token)
    path=env['app']/'data/mcp_local.store';raw=path.read_bytes()
    assert p.requests() and path.read_bytes()==raw
    for needle in (token,'Codex','codex.exe','group@chatroom'):
        assert needle.encode() not in raw
    assert raw[:4]==b'NFR1'
    env['clock'].now+=REQUEST_TTL+1
    assert p.requests()==[]


def test_retired_admin_and_client_flags_rejected(env):
    from noteford.local_pairing import run_admin, run_local_mcp
    for action in ('lookup','pending'):
        with pytest.raises(SystemExit):run_admin(['--config',str(env['config']),action])
    with pytest.raises(SystemExit):run_local_mcp(['--config',str(env['config']),'--client','g1'])
