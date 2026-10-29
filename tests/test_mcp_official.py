import asyncio
import json
import sqlite3
from pathlib import Path
import pytest
from mcp import Client
from noteford.indexer import _connect_index
from noteford.libraries import LibraryRegistry
from noteford.mcp_server import create_server
from test_mcp_server import _indexed_account


@pytest.fixture
def sample(tmp_path):
    app = tmp_path / 'app'; app.mkdir()
    wechat = tmp_path / 'wechat'
    for name in ('session','contact'):
        folder = wechat / 'acct_a' / 'db_storage' / name
        folder.mkdir(parents=True); (folder / f'{name}.db').write_bytes(b'fixture')
    config = app / 'config.local.json'
    config.write_text(json.dumps({'project_root': str(app), 'wechat_roots': [str(wechat)]}))
    registry = LibraryRegistry(app, {'project_root': str(app), 'wechat_roots': [str(wechat)]})
    registry.discover(wechat)
    record = _indexed_account(app, registry, 'acct_a', 'authorized text')
    policy = app / 'data/mcp_access.json'
    policy.write_text(json.dumps({'version': 1, 'clients': {'test': {
        'enabled': True, 'library_id': record.id, 'conversations': ['group@chatroom']}}}))
    return config, record.id, registry.context(record.id).index_path, policy


def test_updates_retry_late_message_edit_delete_and_epoch_reset(sample):
    config, library, index, policy = sample
    async def exercise():
        async with Client(create_server(config, 'test')) as client:
            async def read(**args):
                result = await client.call_tool('read_changes', {'library_id': library, **args})
                assert not result.is_error, result
                return result.structured_content
            first = await read(limit=1)
            assert first['has_more'] and first['events'][0]['uid'] == 'acct_a-1'
            retry = await read(limit=1)
            assert retry == first
            second = await read(cursor=first['cursor'], limit=1)
            assert not second['has_more'] and second['events'][0]['uid'] == 'acct_a-2'
            assert (await read(cursor=second['cursor']))['events'] == []
            assert (await read(start='now'))['events'] == []
            with sqlite3.connect(index) as con:
                # A newly indexed message with a much older sent timestamp.
                con.execute('''INSERT INTO messages(uid,account,source_db,chat_table,local_id,server_id,
                    local_type,sort_seq,real_sender_id,create_time,status,message_content,compress_content,
                    packed_info_data,row_hash,indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    ('late','acct_a','message_0.db','Msg_group',3,'3',1,3,'0',1,0,b'late',b'',b'','late','now'))
                con.execute("UPDATE messages SET message_content=?,row_hash='edited' WHERE uid='acct_a-1'", (b'edited',))
                con.execute("DELETE FROM messages WHERE uid='acct_a-2'")
            changes = await read(cursor=second['cursor'])
            assert [(e['uid'], e['kind']) for e in changes['events']] == [('late','upsert'),('acct_a-1','upsert'),('acct_a-2','delete')]
            assert changes['events'][0]['current']['message']['timestamp'] == 1
            assert changes['events'][1]['current']['message']['text'] == 'edited'
            assert changes['events'][2]['current'] is None
            with sqlite3.connect(index) as con:
                con.execute("UPDATE mcp_stream SET epoch='replacement'")
            invalid = await client.call_tool('read_changes', {'library_id': library, 'cursor': changes['cursor']})
            assert invalid.is_error
    asyncio.run(exercise())


def test_scope_every_tool_revocation_and_no_unapproved_history(sample):
    config, library, index, policy = sample
    with sqlite3.connect(index) as con:
        con.execute("INSERT INTO conversations(account,chat_table,username,display_name,is_group,session_type) VALUES('acct_a','Msg_private','secret','hidden',0,0)")
        con.execute('''INSERT INTO messages(uid,account,source_db,chat_table,local_id,server_id,local_type,
            message_content,row_hash,indexed_at) VALUES('hidden','acct_a','message_0.db','Msg_private',1,'9',1,?,'h','now')''', (b'secret text',))
    state = index.parents[1] / 'state' / 'hourly_refresh.json'
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({'last_success_at': '2026-10-10T01:00:00+00:00'}))
    sync = state.with_name('sync.json')
    sync.write_text(json.dumps({'state': 'partial', 'error': 'selected_refresh_partial',
                               'failed_conversations': 1, 'source_path': 'private',
                               'recent_receipts': {'broken': 'not a list'}}))
    sync_before = sync.read_bytes()
    before = index.read_bytes()
    async def exercise():
        async with Client(create_server(config, 'test')) as client:
            result = (await client.call_tool('list_conversations', {'library_id': library})).structured_content
            assert [c['username'] for c in result['conversations']] == ['group@chatroom']
            assert (await client.call_tool('read_messages', {'library_id': library,'conversation_id':'secret'})).is_error
            assert (await client.call_tool('get_message', {'library_id': library,'message_uid':'hidden'})).structured_content == {'found':False}
            found = (await client.call_tool('search_messages', {'library_id': library,'query':'secret'})).structured_content
            assert found['items'] == [] and found['scanned'] == 2
            changes = (await client.call_tool('read_changes', {'library_id':library})).structured_content
            assert len(changes['events']) == 2
            status = (await client.call_tool('get_status', {'library_id': library})).structured_content
            assert status['incremental_ready'] and status['source_online'] is None
            assert status['archive_last_success_at'] == '2026-10-10T01:00:00+00:00'
            assert status['sync']['state'] == 'partial'
            assert status['sync']['failed_conversations'] == 1
            assert not status['sync_runtime_verified'] and 'source_path' not in status['sync']
            first = (await client.call_tool('read_messages', {'library_id':library,'conversation_id':'group@chatroom','limit':1})).structured_content
            second = (await client.call_tool('read_messages', {'library_id':library,'conversation_id':'group@chatroom','limit':1,'offset':first['next_offset']})).structured_content
            assert first['has_more'] and not second['has_more']
            assert first['messages'][0]['uid'] != second['messages'][0]['uid']
            assert (await client.call_tool('get_message', {'library_id': library,'message_uid':'acct_a-1'})).structured_content['found']
            state=json.loads(policy.read_text()); state['clients']['test']['enabled']=False; policy.write_text(json.dumps(state))
            for name,args in [('list_libraries',{}),('read_changes',{'library_id':library}),('get_status',{'library_id':library})]:
                assert (await client.call_tool(name,args)).is_error
        async with Client(create_server(config)) as client:
            assert (await client.call_tool('list_libraries',{})).is_error
    asyncio.run(exercise())
    assert index.read_bytes() == before
    assert sync.read_bytes() == sync_before


def test_journal_upgrade_idempotence_and_old_index_compatibility(sample):
    config, library, index, policy = sample
    with sqlite3.connect(index) as con:
        for name in ('mcp_message_insert','mcp_message_update','mcp_message_delete'):
            con.execute(f'DROP TRIGGER {name}')
        con.execute('DROP TABLE mcp_changes'); con.execute('DROP TABLE mcp_stream')
    async def before_upgrade():
        async with Client(create_server(config,'test')) as client:
            assert (await client.call_tool('read_messages',{'library_id':library,'conversation_id':'group@chatroom'})).structured_content['messages']
            assert (await client.call_tool('read_changes',{'library_id':library})).is_error
    asyncio.run(before_upgrade())
    _connect_index(index).close(); _connect_index(index).close()
    with sqlite3.connect(index) as con:
        assert con.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 2
        assert con.execute('SELECT COUNT(*) FROM mcp_changes').fetchone()[0] == 2
        con.execute("UPDATE messages SET row_hash=row_hash WHERE uid='acct_a-1'")
        assert con.execute('SELECT COUNT(*) FROM mcp_changes').fetchone()[0] == 2

def test_content_scope_counts_sources_dates_pagination_and_revocation(sample):
    config, library, index, policy = sample
    with sqlite3.connect(index) as con:
        con.execute("INSERT OR IGNORE INTO conversation_selection(account,username,chat_table,state,selected_at,updated_at) VALUES('acct_a','group@chatroom','Msg_group','following','now','now')")
        con.execute("UPDATE messages SET message_content=? WHERE uid='acct_a-1'", (b'https://example.com/a https://mp.weixin.qq.com/s/test',))
        con.execute("UPDATE messages SET message_content=? WHERE uid='acct_a-2'", (b'https://example.com/a',))
        con.execute("INSERT INTO conversations(account,chat_table,username,display_name,is_group,session_type) VALUES('acct_a','Msg_secret','secret','SECRET NAME',0,0)")
        con.execute("INSERT INTO conversation_selection(account,username,chat_table,state,selected_at,updated_at) VALUES('acct_a','secret','Msg_secret','following','now','now')")
        con.execute("INSERT INTO messages(uid,account,source_db,chat_table,local_type,message_content,row_hash,indexed_at) VALUES('secret-msg','acct_a','message_0.db','Msg_secret',1,?,'s','now')", (b'https://example.com/a https://secret.test/',))
    before=index.read_bytes()
    async def exercise():
        async with Client(create_server(config,'test')) as client:
            first=await client.call_tool('list_content',{'library_id':library,'limit':1})
            assert not first.is_error
            data=first.structured_content
            assert data['total']==2 and data['counts']['link']==1 and data['counts']['article']==1
            assert data['next_offset']==1
            assert len(data['items'][0]['sources'])==2
            assert all(s['conversation_id']=='group@chatroom' for s in data['items'][0]['sources'])
            second=(await client.call_tool('list_content',{'library_id':library,'offset':1,'limit':1})).structured_content
            assert second['next_offset'] is None and second['items'][0]['category']=='article'
            article=(await client.call_tool('list_content',{'library_id':library,'category':'article'})).structured_content
            assert article['total']==1
            assert (await client.call_tool('get_message',{'library_id':library,'message_uid':article['items'][0]['sources'][0]['message_uid']})).structured_content['found']
            for args in [{'conversation_id':'secret'},{'library_id':'other'},{'limit':101},{'offset':-1},{'category':'unknown'},{'start_day':'2026-10-10','end_day':'2026-01-01'}]:
                assert (await client.call_tool('list_content',{'library_id':library,**args})).is_error
            assert (await client.call_tool('list_content',{'library_id':library,'start_day':'2027-01-01'})).structured_content['total']==0
            assert (await client.call_tool('list_content',{'library_id':library,'query':'secret'})).structured_content['total']==0
            policy_data=json.loads(policy.read_text());policy_data['clients']['test']['enabled']=False;policy.write_text(json.dumps(policy_data))
            assert (await client.call_tool('list_content',{'library_id':library})).is_error
    asyncio.run(exercise())
    assert index.read_bytes()==before

def test_all_synced_excludes_unselected_and_new_selection_has_history(sample):
    config, library, index, policy = sample
    value = json.loads(policy.read_text())
    value['clients']['test'].update(scope='all_synced', conversations=[])
    policy.write_text(json.dumps(value))
    async def exercise():
        async with Client(create_server(config, 'test')) as client:
            async def call(name, **args):
                result = await client.call_tool(name, {'library_id': library, **args})
                assert not result.is_error, result
                return result.structured_content
            first = await call('read_changes', start='now')
            with sqlite3.connect(index) as con:
                con.execute("INSERT INTO conversations(account,chat_table,username,display_name,is_group,session_type) VALUES('acct_a','Msg_future','future','Future',0,0)")
                con.execute('''INSERT INTO messages(uid,account,source_db,chat_table,local_id,server_id,local_type,
                    message_content,row_hash,indexed_at,create_time) VALUES('future-message','acct_a','message_0.db','Msg_future',1,'9',1,?,'h','now',1)''', (b'https://example.com/future',))
            changes = await call('read_changes', cursor=first['cursor'])
            assert changes['events'] == []
            assert not (await call('get_message', message_uid='future-message'))['found']
            assert (await call('list_content', category='link'))['total'] == 0
            assert not (await call('search_messages', query='example.com'))['items']
            assert (await client.call_tool('read_messages', {'library_id': library, 'conversation_id': 'future'})).is_error
            with sqlite3.connect(index) as con:
                con.execute("INSERT INTO conversation_selection(account,username,chat_table,state,selected_at,updated_at) VALUES('acct_a','future','Msg_future','following','now','now')")
            assert (await call('get_message', message_uid='future-message'))['found']
            assert (await call('list_content', category='link'))['total'] == 1
            assert (await call('search_messages', query='example.com'))['items']
            assert (await call('read_history', conversation_id='future'))['messages'][0]['message']['uid'] == 'future-message'
            # Selection changes reset the cursor so new historical scope cannot
            # silently be skipped. Read the new snapshot before checkpointing.
            assert (await client.call_tool('read_changes', {'library_id': library, 'cursor': changes['cursor']})).is_error
            assert (await call('read_changes', start='now'))['events'] == []
            assert (await client.call_tool('get_status', {'library_id':'wrong'})).is_error
            value['clients']['test']['enabled'] = False
            policy.write_text(json.dumps(value))
            assert (await client.call_tool('list_content', {'library_id':library})).is_error
    asyncio.run(exercise())
