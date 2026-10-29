import sqlite3
from pathlib import Path

from noteford.indexer import _connect_index
from noteford.metadata import ensure_metadata_schema
from noteford.repository import WeChatRepository


def _fixture(path: Path):
    con = _connect_index(path)
    ensure_metadata_schema(con)
    with con:
        con.execute(
            """INSERT INTO conversations(
                account, chat_table, username, display_name, is_group,
                session_type, last_timestamp, sort_timestamp
            ) VALUES(?,?,?,?,?,?,?,?)""",
            ("acct", "Msg_friend", "wxid_friend", "Friend", 0, 0, 2000, 2000),
        )
        con.execute(
            """INSERT INTO contacts(
                account, username, alias, remark, nick_name, display_name, is_group
            ) VALUES(?,?,?,?,?,?,?)""",
            ("acct", "wxid_friend", "", "", "Friend", "Friend", 0),
        )
        con.execute(
            """INSERT INTO contacts(
                account, username, alias, remark, nick_name, display_name, is_group
            ) VALUES(?,?,?,?,?,?,?)""",
            ("acct", "wxid_me", "", "", "Me", "Me", 0),
        )
        con.execute(
            """INSERT INTO account_profile(
                account, self_username, self_display_name,
                inferred_direct_count, inference_confidence, updated_at
            ) VALUES(?,?,?,?,?,?)""",
            ("acct", "wxid_me", "Me", 20, 1.0, "now"),
        )
        con.executemany(
            "INSERT INTO sender_map(account,source_db,sender_rowid,username) VALUES(?,?,?,?)",
            [
                ("acct", "message_0.db", 1, "wxid_friend"),
                ("acct", "message_0.db", 2, "wxid_me"),
            ],
        )
        rows = [
            ("u1", "acct", "message_0.db", "Msg_friend", 1, "101", 1, 1, "1", 1000, 0, b"hello", b"", None, None, b"", "h1", "now"),
            ("u2", "acct", "message_0.db", "Msg_friend", 2, "102", 1, 2, "2", 2000, 0, b"world", b"", None, None, b"", "h2", "now"),
        ]
        con.executemany(
            """INSERT INTO messages(
                uid,account,source_db,chat_table,local_id,server_id,local_type,
                sort_seq,real_sender_id,create_time,status,message_content,
                compress_content,packed_info_data,wcdb_ct,source,row_hash,indexed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
    con.close()


def test_repository_lists_and_reads_messages(tmp_path):
    index = tmp_path / "index.db"
    _fixture(index)
    repo = WeChatRepository(index, "acct")

    status = repo.status()
    assert status["messages"] == 2
    assert status["conversations"] == 1
    assert status["self_identity_resolved"] is True

    conversations = repo.list_conversations(limit=20)
    assert len(conversations) == 1
    assert conversations[0]["name"] == "Friend"
    assert conversations[0]["preview"] == "world"

    payload = repo.list_messages("wxid_friend", limit=20)
    assert [row["text"] for row in payload["messages"]] == ["hello", "world"]
    assert payload["messages"][0]["sender"]["from_me"] is False
    assert payload["messages"][1]["sender"]["from_me"] is True

    search = repo.search_messages("world", limit=10)
    assert len(search["items"]) == 1
    assert search["items"][0]["text"] == "world"


def test_message_filters_search_dates_sender_and_page_all_history(tmp_path):
    index = tmp_path / 'index.db'
    _fixture(index)
    con = sqlite3.connect(index)
    with con:
        con.execute(
            """INSERT INTO messages(uid,account,source_db,chat_table,local_id,
               server_id,local_type,sort_seq,real_sender_id,create_time,status,
               message_content,compress_content,packed_info_data,row_hash,indexed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ('u3', 'acct', 'message_0.db', 'Msg_friend', 3, '103', 3,
             3, '1', 3000, 0, b'', b'', b'', 'h3', 'now'),
        )
    con.close()
    repo = WeChatRepository(index, 'acct')
    assert [m['uid'] for m in repo.list_messages('wxid_friend', category='media')['messages']] == ['u3']
    assert [m['uid'] for m in repo.list_messages('wxid_friend', query='world')['messages']] == ['u2']
    assert [m['uid'] for m in repo.list_messages('wxid_friend', sender='wxid_friend')['messages']] == ['u1', 'u3']
    assert [m['uid'] for m in repo.list_messages('wxid_friend', from_day=1500, to_day=3000)['messages']] == ['u2']
    first = repo.list_messages('wxid_friend', sender='wxid_friend', limit=1)
    assert first['next_offset'] == 1
    second = repo.list_messages('wxid_friend', sender='wxid_friend', limit=1, filter_offset=first['next_offset'])
    assert second['messages'][0]['uid'] != first['messages'][0]['uid']


def test_message_context_cursors_handle_same_second_null_native_keys(tmp_path):
    index = tmp_path / 'null-context.db'
    _fixture(index)
    with sqlite3.connect(index) as con:
        con.executemany(
            """INSERT INTO messages(
                   uid,account,source_db,chat_table,local_id,server_id,local_type,
                   sort_seq,real_sender_id,create_time,status,message_content,
                   compress_content,packed_info_data,row_hash,indexed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                ('null-a', 'acct', 'message_0.db', 'Msg_friend', 10, None, 1,
                 None, '1', 2000, 0, b'a', b'', b'', 'null-a', 'now'),
                ('null-b', 'acct', 'message_0.db', 'Msg_friend', 11, None, 1,
                 None, '1', 2000, 0, b'b', b'', b'', 'null-b', 'now'),
                ('null-c', 'acct', 'message_0.db', 'Msg_friend', 12, None, 1,
                 None, '1', 2000, 0, b'c', b'', b'', 'null-c', 'now'),
            ],
        )
    repo = WeChatRepository(index, 'acct')
    around = repo.list_messages('wxid_friend', around_uid='null-b', limit=3)
    assert [row['uid'] for row in around['messages']] == ['null-a', 'null-b', 'null-c']
    before = repo.list_messages('wxid_friend', before_uid='null-b', limit=10)
    after = repo.list_messages('wxid_friend', after_uid='null-b', limit=10)
    assert [row['uid'] for row in before['messages']] == ['u1', 'null-a']
    assert [row['uid'] for row in after['messages']] == ['null-c', 'u2']
    assert not ({row['uid'] for row in before['messages']} &
                {row['uid'] for row in after['messages']})


def test_group_members_come_from_contact_membership_not_recent_messages(tmp_path):
    index = tmp_path / 'index.db'
    _fixture(index)
    con = sqlite3.connect(index)
    with con:
        con.execute(
            '''INSERT INTO conversations(account,chat_table,username,display_name,is_group,
               session_type,last_timestamp,sort_timestamp) VALUES(?,?,?,?,?,?,?,?)''',
            ('acct', 'Msg_group', 'group@chatroom', 'Group', 1, 0, 0, 0),
        )
    con.close()
    contact_db = tmp_path / 'contact.db'
    contact = sqlite3.connect(contact_db)
    contact.executescript('''
        CREATE TABLE contact(id INTEGER PRIMARY KEY, username TEXT, remark TEXT, nick_name TEXT);
        CREATE TABLE chat_room(id INTEGER PRIMARY KEY, username TEXT);
        CREATE TABLE chatroom_member(room_id INTEGER, member_id INTEGER);
        INSERT INTO chat_room VALUES(1, 'group@chatroom');
        INSERT INTO contact VALUES(10, 'wxid_a', '备注 A', '昵称 A');
        INSERT INTO contact VALUES(11, 'wxid_b', '', '昵称 B');
        INSERT INTO chatroom_member VALUES(1, 10);
        INSERT INTO chatroom_member VALUES(1, 11);
    ''')
    contact.close()
    members = WeChatRepository(index, 'acct').group_members('group@chatroom', contact_db)
    assert members == [
        {'username': 'wxid_a', 'name': '备注 A'},
        {'username': 'wxid_b', 'name': '昵称 B'},
    ]


def test_repository_hides_internal_sessions_and_never_returns_unknown_preview(tmp_path):
    index = tmp_path / "index.db"
    _fixture(index)
    con = sqlite3.connect(index)
    try:
        con.executemany(
            """INSERT INTO conversations(
                account, chat_table, username, display_name, is_group,
                session_type, last_timestamp, sort_timestamp
            ) VALUES(?,?,?,?,?,?,?,?)""",
            [
                ("acct", "Msg_gh", "gh_public", "Public Account", 0, 0, 1500, 1500),
                ("acct", "Msg_internal", "brandsessionholder", "brandsessionholder", 0, 0, 1600, 1600),
                ("acct", "Msg_empty", "wxid_empty", "Empty Friend", 0, 0, 1400, 1400),
            ],
        )
        con.commit()
    finally:
        con.close()

    repo = WeChatRepository(index, "acct")
    rows = repo.list_conversations(limit=50)
    by_username = {row["username"]: row for row in rows}

    assert "brandsessionholder" not in by_username
    assert by_username["gh_public"]["preview"] == "[公众号]"
    assert by_username["wxid_empty"]["preview"] == "暂无本地消息"
    assert all(row["preview"] != "[unknown]" for row in rows)


def test_repository_filters_following_without_hiding_directory_rows(tmp_path):
    index = tmp_path / "index.db"
    _fixture(index)
    con = sqlite3.connect(index)
    try:
        con.execute(
            """INSERT INTO conversations(
                account, chat_table, username, display_name, is_group,
                session_type, last_timestamp, sort_timestamp
            ) VALUES(?,?,?,?,?,?,?,?)""",
            ("acct", "Msg_other", "wxid_other", "Other", 0, 0, 1500, 1500),
        )
        con.execute(
            """INSERT INTO conversation_selection(
                account, username, chat_table, state, source_dbs,
                selected_at, updated_at, last_refresh_at, last_error
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "acct", "wxid_friend", "Msg_friend", "following",
                '["message_0.db"]', "now", "now", "now", None,
            ),
        )
        con.commit()
    finally:
        con.close()

    repo = WeChatRepository(index, "acct")
    all_rows = repo.list_conversations(limit=20)
    following = repo.list_conversations(
        limit=20,
        selection_state="following",
    )

    assert {row["username"] for row in all_rows} == {
        "wxid_friend",
        "wxid_other",
    }
    assert [row["username"] for row in following] == ["wxid_friend"]
    assert following[0]["selection_state"] == "following"


def test_quote_target_requires_unique_native_id_in_same_account_and_chat(tmp_path):
    index = tmp_path / 'quote.db'
    _fixture(index)
    con = sqlite3.connect(index)
    xml = '<msg><appmsg><title>reply</title><type>57</type><refermsg><type>1</type><svrid>101</svrid><displayname>Friend</displayname><content>selected fragment</content></refermsg></appmsg></msg>'
    with con:
        con.execute('UPDATE messages SET local_type=?,message_content=? WHERE uid=?', ((57 << 32) | 49, xml, 'u2'))
    repo = WeChatRepository(index, 'acct')
    quote = repo.list_messages('wxid_friend')['messages'][-1]['quote']
    assert quote['server_id'] == '101'
    assert quote['text'] == 'selected fragment'
    assert quote['target']['uid'] == 'u1'
    assert quote['target']['text'] == 'hello'
    assert quote['target']['quote'] is None
    # A different conversation with identical text/time/native ID must never be chosen.
    with con:
        con.execute("UPDATE messages SET chat_table='Msg_other' WHERE uid='u1'")
    assert repo.list_messages('wxid_friend')['messages'][-1]['quote']['state'] == 'unavailable'
    with con:
        con.execute("UPDATE messages SET chat_table='Msg_friend',account='other-account' WHERE uid='u1'")
    assert repo.list_messages('wxid_friend')['messages'][-1]['quote']['target'] is None
    # No ID means no fuzzy matching, even if a timestamp/text looks plausible.
    with con:
        con.execute("UPDATE messages SET account='acct' WHERE uid='u1'")
        con.execute('UPDATE messages SET message_content=? WHERE uid=?', (xml.replace('<svrid>101</svrid>', ''), 'u2'))
    assert repo.list_messages('wxid_friend')['messages'][-1]['quote']['target'] is None
    con.close()


def test_quote_ambiguous_native_id_does_not_pick_first_match(tmp_path):
    index = tmp_path / 'duplicate.db'
    _fixture(index)
    con = sqlite3.connect(index)
    with con:
        con.execute("INSERT INTO messages SELECT 'duplicate',account,'message_9.db',chat_table,99,server_id,local_type,sort_seq,real_sender_id,create_time,status,message_content,compress_content,packed_info_data,wcdb_ct,source,row_hash,indexed_at FROM messages WHERE uid='u1'")
    items = [{'uid': 'reply', 'quote': {'server_id': '101'}}]
    WeChatRepository(index, 'acct')._resolve_quotes(items, 'Msg_friend')
    assert items[0]['quote']['target'] is None
    con.close()


def test_nested_quote_keeps_snapshot_without_recursive_resolution(tmp_path):
    index = tmp_path / 'nested.db'
    _fixture(index)
    con = sqlite3.connect(index)
    with con:
        for uid, target in [('u1', '102'), ('u2', '101')]:
            xml = f'<msg><appmsg><title>reply</title><type>57</type><refermsg><type>1</type><svrid>{target}</svrid><content>snapshot</content></refermsg></appmsg></msg>'
            con.execute('UPDATE messages SET local_type=?,message_content=? WHERE uid=?', ((57 << 32) | 49, xml, uid))
    items = WeChatRepository(index, 'acct').list_messages('wxid_friend')['messages']
    assert items[0]['quote']['target']['uid'] == 'u2'
    assert items[0]['quote']['target']['quote']['server_id'] == '101'
    assert 'target' not in items[0]['quote']['target']['quote']
    con.close()


def test_exact_context_cursor_keeps_same_second_order_and_rejects_other_chat(tmp_path):
    import pytest
    index = tmp_path / 'context.db'
    _fixture(index)
    con = sqlite3.connect(index)
    with con:
        con.execute('UPDATE messages SET create_time=1000')
    repo = WeChatRepository(index, 'acct')
    assert [x['uid'] for x in repo.list_messages('wxid_friend', around_uid='u1', limit=2)['messages']] == ['u1', 'u2']
    assert [x['uid'] for x in repo.list_messages('wxid_friend', after_uid='u1')['messages']] == ['u2']
    assert repo.list_messages('wxid_friend', after_uid='u2')['messages'] == []
    assert [x['uid'] for x in repo.list_messages('wxid_friend', before_uid='u2')['messages']] == ['u1']
    with pytest.raises(ValueError, match='incompatible_message_cursor'):
        repo.list_messages('wxid_friend', around_uid='u1', category='file')
    with con:
        con.execute("UPDATE messages SET chat_table='Msg_other' WHERE uid='u1'")
    with pytest.raises(ValueError, match='message_cursor_unavailable'):
        repo.list_messages('wxid_friend', around_uid='u1')
    con.close()

def test_time_cursor_returns_first_at_or_after_with_context(tmp_path):
    import pytest
    index = tmp_path / 'index.db'
    _fixture(index)
    repo = WeChatRepository(index, 'acct')
    assert repo.list_messages('wxid_friend', at_time=1000, limit=1)['messages'][0]['uid'] == 'u1'
    assert repo.list_messages('wxid_friend', at_time=1001, limit=1)['messages'][0]['uid'] == 'u2'
    assert repo.list_messages('wxid_friend', at_time=2001)['messages'] == []
    assert [m['uid'] for m in repo.list_messages('wxid_friend', at_time=1500, limit=60)['messages']] == ['u1', 'u2']
    with pytest.raises(ValueError, match='incompatible_message_cursor'):
        repo.list_messages('wxid_friend', at_time=1000, query='hello')

def test_combined_multi_type_member_time_filters_and_pagination(tmp_path):
    import pytest
    index = tmp_path / 'index.db'
    _fixture(index)
    with sqlite3.connect(index) as con:
        con.execute("UPDATE messages SET local_type=3 WHERE uid='u1'")
        con.execute("UPDATE messages SET local_type=43 WHERE uid='u2'")
    repo = WeChatRepository(index, 'acct')
    args = dict(category='image,video', sender='wxid_friend,wxid_me', from_day=1000, to_day=2001)
    first = repo.list_messages('wxid_friend', limit=1, **args)
    assert [m['uid'] for m in first['messages']] == ['u2']
    second = repo.list_messages('wxid_friend', limit=1, filter_offset=first['next_offset'], **args)
    assert [m['uid'] for m in second['messages']] == ['u1']
    assert repo.list_messages('wxid_friend', category='image', sender='wxid_me')['messages'] == []
    assert [m['uid'] for m in repo.list_messages('wxid_friend', category='image,video', to_day=2000)['messages']] == ['u1']
    assert repo.list_messages('wxid_friend', query='no-match', **args)['messages'] == []
    with pytest.raises(ValueError, match='invalid_message_category'):
        repo.list_messages('wxid_friend', category='image,unknown')
