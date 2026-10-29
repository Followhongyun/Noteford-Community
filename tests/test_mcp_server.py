import asyncio
import base64
import json
import sqlite3
from datetime import datetime
from mcp import Client

from noteford.indexer import _connect_index
from noteford.libraries import LibraryRegistry
from noteford.mcp_server import create_server
from noteford.metadata import ensure_metadata_schema


MESSAGE_TIME = int(datetime(2026, 9, 27, 12).timestamp())
REPORT_DAY = "2026-09-27"


def _indexed_account(app, registry, account, text):
    record = next(row for row in registry.list() if row.account == account)
    path = registry.context(record.id).index_path
    path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect_index(path)
    ensure_metadata_schema(con)
    with con:
        con.execute(
            """INSERT INTO conversations(account,chat_table,username,display_name,is_group,
               session_type,last_timestamp,sort_timestamp) VALUES(?,?,?,?,?,?,?,?)""",
            (account, "Msg_group", "group@chatroom", "测试群", 1, 0, MESSAGE_TIME, MESSAGE_TIME),
        )
        con.execute(
            """INSERT INTO conversation_selection(
                   account,username,chat_table,state,source_dbs,selected_at,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            (account, "group@chatroom", "Msg_group", "following", "[]", "now", "now"),
        )
        for number in (1, 2):
            con.execute(
                """INSERT INTO messages(uid,account,source_db,chat_table,local_id,
                   server_id,local_type,sort_seq,real_sender_id,create_time,status,
                   message_content,compress_content,packed_info_data,row_hash,indexed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"{account}-{number}", account, "message_0.db", "Msg_group", number,
                 str(number), 1, number, "0", MESSAGE_TIME, 0, text.encode(), b"", b"",
                 f"hash-{number}", "now"),
            )
    con.close()
    return record


def test_five_mcp_tools_read_isolated_libraries_and_page_same_second(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    wechat = tmp_path / "wechat"
    for account in ("acct_a", "acct_b"):
        for name in ("session", "contact"):
            folder = wechat / account / "db_storage" / name
            folder.mkdir(parents=True)
            (folder / f"{name}.db").write_bytes(b"fixture")
    config = app / "config.local.json"
    config.write_text(json.dumps({"project_root": str(app), "wechat_roots": [str(wechat)]}))
    registry = LibraryRegistry(app, {"project_root": str(app), "wechat_roots": [str(wechat)]})
    registry.discover(wechat)
    a = _indexed_account(app, registry, "acct_a", "A 独有讨论")
    b = _indexed_account(app, registry, "acct_b", "B 独有讨论")

    (app / 'data' / 'mcp_access.json').write_text(json.dumps({'version': 1, 'clients': {
        'a': {'enabled': True, 'library_id': a.id, 'conversations': ['group@chatroom']},
        'b': {'enabled': True, 'library_id': b.id, 'conversations': ['group@chatroom']},
    }}))
    server = create_server(config, 'a')

    async def exercise():
        async with Client(server) as client, Client(create_server(config, 'b')) as other_client:
            tools = await client.list_tools()
            assert {tool.name for tool in tools.tools} == {
                "list_libraries", "list_conversations", "read_messages",
                "search_messages", "get_status", "get_message", "read_changes", "list_content", "read_image",
                "get_history_overview", "read_history", "get_message_context", "read_attachment", "request_sync",
            }
            libraries_result = await client.call_tool("list_libraries", {})
            assert libraries_result.structured_content is not None, repr(libraries_result)
            libraries = libraries_result.structured_content
            assert {row["id"] for row in libraries["libraries"]} == {a.id}
            conversations = (await client.call_tool("list_conversations", {
                "library_id": a.id, "groups_only": True,
            })).structured_content
            assert conversations["conversations"][0]["username"] == "group@chatroom"

            first = (await client.call_tool("read_messages", {
                "library_id": a.id, "conversation_id": "group@chatroom",
                "start_day": REPORT_DAY, "end_day": REPORT_DAY, "limit": 1,
            })).structured_content
            assert first["has_more"] is True
            second = (await client.call_tool("read_messages", {
                "library_id": a.id, "conversation_id": "group@chatroom",
                "start_day": REPORT_DAY, "end_day": REPORT_DAY, "limit": 1,
                "offset": first["next_offset"],
            })).structured_content
            assert second["has_more"] is False
            assert first["messages"][0]["uid"] != second["messages"][0]["uid"]
            assert all("A 独有讨论" in item["text"] for item in
                       first["messages"] + second["messages"])
            other = (await other_client.call_tool("read_messages", {
                "library_id": b.id, "conversation_id": "group@chatroom",
            })).structured_content
            assert all("B 独有讨论" in item["text"] for item in other["messages"])
            search = (await client.call_tool("search_messages", {
                "library_id": a.id, "query": "A 独有",
            })).structured_content
            assert len(search["items"]) == 2
            assert "truncated_scan" in search
            assert search["results_limited"] is False
            limited = (await client.call_tool("search_messages", {
                "library_id": a.id, "query": "A 独有", "limit": 1,
            })).structured_content
            assert limited["results_limited"] is True

    before = {path: path.read_bytes() for path in (config, registry.registry_path,
              registry.context(a.id).index_path, registry.context(b.id).index_path)}
    asyncio.run(exercise())
    assert all(path.read_bytes() == contents for path, contents in before.items())


def test_mcp_read_rejects_bad_date_and_unknown_library(tmp_path):
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"project_root": str(tmp_path), "wechat_roots": []}))
    server = create_server(config)

    async def exercise():
        async with Client(server) as client:
            result = await client.call_tool("read_messages", {
                "library_id": "missing", "conversation_id": "group", "start_day": "bad-date",
            })
            assert result.is_error
            result = await client.call_tool("search_messages", {
                "library_id": "missing", "query": "hello",
            })
            assert result.is_error

    asyncio.run(exercise())


def test_read_image_returns_pixels_quality_and_enforces_grant(tmp_path):
    app = tmp_path / 'app'
    app.mkdir()
    wechat = tmp_path / 'wechat'
    for name in ('session', 'contact'):
        folder = wechat / 'acct' / 'db_storage' / name
        folder.mkdir(parents=True)
        (folder / f'{name}.db').write_bytes(b'fixture')
    config = app / 'config.local.json'
    settings = {'project_root': str(app), 'wechat_roots': [str(wechat)]}
    config.write_text(json.dumps(settings))
    registry = LibraryRegistry(app, settings)
    registry.discover(wechat)
    record = _indexed_account(app, registry, 'acct', 'fixture')
    context = registry.context(record.id)
    md5 = b'0123456789abcdef0123456789abcdef'
    nested = bytes([0x22, len(md5)]) + md5
    packed = bytes([0x1a, len(nested)]) + nested
    with sqlite3.connect(context.index_path) as con:
        con.execute('UPDATE messages SET local_type=3, packed_info_data=? WHERE uid=?', (packed, 'acct-1'))
    access = app / 'data' / 'mcp_access.json'
    access.write_text(json.dumps({'version': 1, 'clients': {'test': {
        'enabled': True, 'library_id': record.id, 'conversations': ['group@chatroom']}}}))
    cache = context.root / 'data' / 'media-cache' / 'images'
    cache.mkdir(parents=True)
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aI1sAAAAASUVORK5CYII=')
    (cache / (md5.decode() + '_thumb.png')).write_bytes(png)
    async def exercise():
        async with Client(create_server(config, 'test')) as client:
            args = {'library_id': record.id, 'message_uid': 'acct-1'}
            result = await client.call_tool('read_image', args)
            assert not result.is_error
            assert json.loads(result.content[0].text)['quality'] == 'thumbnail'
            assert result.content[1].type == 'image'
            assert base64.b64decode(result.content[1].data) == png
            (cache / (md5.decode() + '.png')).write_bytes(png)
            result = await client.call_tool('read_image', args)
            assert json.loads(result.content[0].text)['quality'] == 'original'
            assert (await client.call_tool('read_image', {**args, 'library_id': 'other'})).is_error
            access.write_text(json.dumps({'version': 1, 'clients': {}}))
            assert (await client.call_tool('read_image', args)).is_error
    asyncio.run(exercise())
