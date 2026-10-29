import asyncio
import json
import sqlite3
import gc
import sys
from pathlib import Path

import pytest
from mcp import Client

from noteford.mac_ai_access import direct_server
from noteford import skill_connection
from test_mcp_official import sample


@pytest.fixture
def direct(sample):
    config, library, index, _ = sample
    policy = config.parent / 'data/mac_ai_access.json'
    policy.write_text(json.dumps({'version': 1, 'enabled': True, 'library_id': library}))
    with sqlite3.connect(index) as con:
        con.execute("INSERT INTO conversations(account,chat_table,username,display_name,is_group,session_type) VALUES('acct_a','Msg_unselected','unselected','Not followed',0,0)")
        con.execute("""INSERT INTO messages(uid,account,source_db,chat_table,local_id,server_id,
            local_type,message_content,row_hash,indexed_at,create_time)
            VALUES('unselected-msg','acct_a','message_0.db','Msg_unselected',1,'9',1,?,'u','now',1)""",
                    (b'UNSELECTED https://example.com/',))
    return config, library, index, policy


def test_following_scope_filters_all_tools_and_tracks_unfollow_without_removing_history(direct):
    config, library, index, policy = direct
    policy.write_text(json.dumps({'version': 1, 'enabled': True, 'library_id': library, 'scope': 'following'}))
    async def exercise():
        async with Client(direct_server(config)) as client:
            async def call(tool, **args):
                result = await client.call_tool(tool, {'library_id': library, **args})
                assert not result.is_error, result
                return result.structured_content
            assert (await call('get_history_overview'))['totals']['indexed_count'] == 2
            assert len((await call('read_history'))['messages']) == 2
            assert not (await call('search_messages', query='UNSELECTED'))['items']
            assert not (await call('get_message', message_uid='unselected-msg'))['found']
            assert (await client.call_tool('read_messages', {'library_id': library, 'conversation_id': 'unselected'})).is_error
            with sqlite3.connect(index) as con:
                con.execute("UPDATE conversation_selection SET state='paused'")
            assert (await call('read_history'))['messages'] == []
            assert (await call('read_changes'))['events'] == []
            with sqlite3.connect(index) as con:
                assert con.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 3
                con.execute("UPDATE conversation_selection SET state='following'")
            assert len((await call('read_history'))['messages']) == 2
    asyncio.run(exercise())


def test_two_clients_read_all_indexed_history_then_disable_denies_existing_session(direct, monkeypatch):
    config, library, index, policy = direct
    monkeypatch.setattr(skill_connection.sys, 'platform', 'darwin')
    monkeypatch.setattr(skill_connection, 'open_pairing', lambda _: pytest.fail('direct access must not create a pairing'))
    gc.collect()
    with sqlite3.connect(index) as checkpoint:
        checkpoint.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    checkpoint.close()
    before = index.read_bytes()

    async def exercise():
        for name, program in [('Codex', '/Applications/Codex'), ('Example AI', '/Applications/Example')]:
            server = skill_connection.connected_server({'config': str(config)}, name, program)
            async with Client(server) as client:
                async def call(tool, **args):
                    result = await client.call_tool(tool, args)
                    assert not result.is_error, result
                    return result.structured_content
                assert (await call('request_pairing'))['scope'] == 'all_indexed'
                assert len((await call('list_libraries'))['libraries']) == 1
                assert (await call('get_status', library_id=library))['read_only']
                overview = await call('get_history_overview', library_id=library)
                assert overview['totals']['indexed_count'] == 3
                assert len(overview['conversations']) == 2
                changes = await call('read_changes', library_id=library, start='now')
                uids = []
                cursor = None
                while True:
                    page = await call('read_history', library_id=library, cursor=cursor, limit=1)
                    uids += [row['message']['uid'] for row in page['messages']]
                    if not page['has_more']: break
                    cursor = page['cursor']
                assert len(uids) == len(set(uids)) == 3 and 'unselected-msg' in uids
                assert not (await call('read_changes', library_id=library, cursor=changes['cursor']))['events']
                assert (await call('search_messages', library_id=library, query='UNSELECTED'))['items'][0]['uid'] == 'unselected-msg'
                assert (await call('list_content', library_id=library))['total'] == 1
                assert (await client.call_tool('read_messages', {'library_id': 'wrong', 'conversation_id': 'unselected'})).is_error
                if name == 'Example AI':
                    policy.write_text(json.dumps({'version': 1, 'enabled': False, 'library_id': library}))
                    for tool, args in [('list_libraries', {}), ('read_history', {'library_id': library}), ('get_message', {'library_id': library, 'message_uid': 'unselected-msg'})]:
                        assert (await client.call_tool(tool, args)).is_error
    asyncio.run(exercise())
    assert index.read_bytes() == before
    assert direct_server(config) is None


def test_new_messages_resume_and_policy_cannot_silently_switch_account(direct):
    config, library, index, policy = direct
    async def exercise():
        async with Client(direct_server(config)) as client:
            result = await client.call_tool('read_changes', {'library_id': library, 'start': 'now'})
            cursor = result.structured_content['cursor']
        with sqlite3.connect(index) as con:
            con.execute("UPDATE messages SET message_content=?,row_hash='updated' WHERE uid='unselected-msg'", (b'NEW MESSAGE',))
        async with Client(direct_server(config)) as client:
            result = await client.call_tool('read_changes', {'library_id': library, 'cursor': cursor})
            assert not result.is_error
            assert [row['uid'] for row in result.structured_content['events']] == ['unselected-msg']
            policy.write_text(json.dumps({'version': 1, 'enabled': True, 'library_id': 'different'}))
            assert (await client.call_tool('list_libraries', {})).is_error
    asyncio.run(exercise())


def test_all_indexed_history_can_cover_more_than_one_thousand_chats(direct):
    config, library, index, _ = direct
    with sqlite3.connect(index) as con:
        con.executemany("INSERT INTO conversations(account,chat_table,username,display_name,is_group,session_type) VALUES('acct_a',?,?,?,0,0)",
                        [(f'Msg_{i}', f'chat-{i}', str(i)) for i in range(1001)])
    async def exercise():
        async with Client(direct_server(config)) as client:
            result = await client.call_tool('read_history', {'library_id': library})
            assert not result.is_error and len(result.structured_content['messages']) == 3
    asyncio.run(exercise())


def test_policy_missing_invalid_or_disabled_is_not_implicit_approval(direct):
    config, _, _, policy = direct
    for value in ['null', 'broken', '{}', '{"version":1,"enabled":"true","library_id":"lib"}']:
        policy.write_text(value)
        assert direct_server(config) is None
    policy.unlink()
    assert direct_server(config) is None


def test_standard_mcp_stdio_transport_works_without_skill_or_pairing(direct):
    from mcp.client.stdio import StdioServerParameters
    config, library, _, _ = direct
    source = str(Path(__file__).resolve().parents[1] / 'src')
    server = StdioServerParameters(
        command=sys.executable,
        args=['-c', 'import sys; from pathlib import Path; from noteford.mac_ai_access import direct_server; direct_server(Path(sys.argv[1])).run(transport="stdio")', str(config)],
        env={'PYTHONPATH': source},
    )
    async def exercise():
        async with Client(server) as client:
            tools = await client.list_tools()
            assert {'read_history', 'read_changes', 'search_messages'} <= {item.name for item in tools.tools}
            assert all(item.annotations.read_only_hint for item in tools.tools if item.name != 'request_sync')
            assert next(item for item in tools.tools if item.name == 'request_sync').annotations.read_only_hint is False
            status = await client.call_tool('get_status', {'library_id': library})
            assert status.structured_content['scope'] == 'all_indexed'
            history = await client.call_tool('read_history', {'library_id': library})
            assert len(history.structured_content['messages']) == 3
    asyncio.run(exercise())
