import asyncio
import base64
import json
import sqlite3
import threading
from types import SimpleNamespace

from mcp import Client
import pytest
from noteford.mcp_server import LocalIndexReader, create_server
from noteford.mcp_sync import SyncRequests
from noteford.mac_attachments import cache_file
from test_mcp_official import sample


def full_grant(policy, library, **extra):
    policy.write_text(json.dumps({'version': 1, 'clients': {'test': {
        'enabled': True, 'library_id': library, 'scope': 'all_indexed', 'conversations': [], **extra}}}))


def test_full_scope_unselected_and_cached_attachment_chunks_revocation(sample, tmp_path):
    config, library, index, policy = sample
    full_grant(policy, library)
    reader = LocalIndexReader(config, 'test')
    ctx = reader._registry().context(library)
    with sqlite3.connect(index) as con:
        con.execute("DELETE FROM conversation_selection")
        con.execute("UPDATE messages SET local_type=25769803825,message_content=? WHERE uid='acct_a-1'",
                    (b'<msg><appmsg><title>test.txt</title><type>6</type></appmsg></msg>',))
    path = cache_file({'project_root': str(ctx.root), 'account': ctx.account}, 'acct_a-1', b'hello attachment', 'txt')
    async def exercise():
        async with Client(create_server(config, 'test')) as client:
            async def call(name, **args):
                return await client.call_tool(name, {'library_id': library, **args})
            assert len((await call('read_history')).structured_content['messages']) == 2
            result = await call('read_attachment', message_uid='acct_a-1', limit=5)
            assert not result.is_error, result
            first = result.structured_content
            assert base64.b64decode(first['data']) == b'hello'
            rest = (await call('read_attachment', message_uid='acct_a-1', offset=first['next_offset'], expected_sha256=first['sha256'])).structured_content
            assert base64.b64decode(rest['data']) == b' attachment' and rest['next_offset'] is None
            assert (await call('read_attachment', message_uid='acct_a-1', expected_sha256='changed')).is_error
            assert (await call('request_sync')).is_error  # Explicit read grant is not sync consent.
            assert (await client.call_tool('read_attachment', {'library_id': 'other', 'message_uid': 'acct_a-1'})).is_error
            full_grant(policy, 'other')
            assert (await call('read_attachment', message_uid='acct_a-1')).is_error
    asyncio.run(exercise())
    assert path.read_bytes() == b'hello attachment'


def test_attachment_export_cannot_escape_cache(sample, tmp_path):
    config, library, index, _ = sample
    outside = tmp_path/'private.txt'; outside.write_text('private')
    reader = LocalIndexReader(config, 'test')
    root = reader._registry().context(library).root/'data/media-cache'
    root.mkdir(parents=True, exist_ok=True)
    link = root/'escape.txt'; link.symlink_to(outside)
    with sqlite3.connect(index) as con:
        con.execute('CREATE TABLE media_exports(uid TEXT,row_hash TEXT,kind TEXT,status TEXT,path TEXT)')
        con.execute('INSERT INTO media_exports VALUES(?,?,?,?,?)', ('acct_a-1','hash-1','file','ready',str(link)))
    async def exercise():
        async with Client(create_server(config, 'test')) as client:
            assert (await client.call_tool('read_attachment', {'library_id': library, 'message_uid': 'acct_a-1'})).is_error
    asyncio.run(exercise())


def test_sync_only_explicit_grant_and_preserves_selection(sample):
    config, library, index, policy = sample
    entered = threading.Event(); release = threading.Event(); done = threading.Event()
    calls = []
    def refresh(**kwargs):
        calls.append(kwargs); entered.set(); release.wait(3); done.set()
    core = SimpleNamespace(refresh_lock=threading.Lock(), refresh=refresh)
    def get_core(identity):
        assert identity == library
        return core
    jobs = SyncRequests(SimpleNamespace(_core=get_core), config)
    payload = {'library_id': library, 'mode': 'grant', 'client_id': 'test'}
    with pytest.raises(PermissionError): jobs.submit(payload)
    full_grant(policy, library, allow_sync=True)
    with sqlite3.connect(index) as con:
        before = con.execute('SELECT * FROM conversation_selection').fetchall()
    try:
        assert jobs.submit(payload)['accepted']
        assert entered.wait(2)
        assert jobs.submit(payload)['state'] == 'busy'
    finally:
        release.set()
    assert done.wait(2)
    assert calls == [{'trigger': 'ai_request'}]
    with sqlite3.connect(index) as con:
        assert con.execute('SELECT * FROM conversation_selection').fetchall() == before
    full_grant(policy, 'other', allow_sync=True)
    with pytest.raises(PermissionError): jobs.submit(payload)


def test_mcp_sync_reaches_authenticated_desktop_bridge(sample):
    from http.server import ThreadingHTTPServer
    from noteford.service import make_handler
    config, library, _, policy = sample
    full_grant(policy, library, allow_sync=True)
    refreshed = threading.Event()
    core = SimpleNamespace(refresh_lock=threading.Lock(), refresh=lambda **kwargs: refreshed.set())
    api = SimpleNamespace(config_path=config, root_config={'project_root': str(config.parent)}, _core=lambda identity: core)
    token = 'synthetic-test-token-only'
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(api, token))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    secret = config.parent/'token.json'; secret.write_text(json.dumps({'token': token}))
    bridge = config.parent/'data/runtime/mcp-service.json'; bridge.parent.mkdir(parents=True)
    bridge.write_text(json.dumps({'port': server.server_address[1], 'token_file': str(secret)}))
    async def exercise():
        async with Client(create_server(config, 'test')) as client:
            result = await client.call_tool('request_sync', {'library_id': library})
            assert not result.is_error, result
            assert result.structured_content['accepted'] is True
            assert token not in str(result)
    try:
        asyncio.run(exercise())
        assert refreshed.wait(2)
    finally:
        server.shutdown(); server.server_close(); thread.join(2)
