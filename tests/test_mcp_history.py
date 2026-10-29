import asyncio
import json
import sqlite3

import pytest
from mcp import Client

from noteford.libraries import LibraryRegistry
from noteford.mcp_server import LocalIndexReader, create_server
from test_mcp_server import MESSAGE_TIME, _indexed_account


def _sample(tmp_path, *, scope="all_synced", explicit=None):
    app = tmp_path / "app"
    app.mkdir()
    wechat = tmp_path / "wechat"
    for name in ("session", "contact"):
        folder = wechat / "acct" / "db_storage" / name
        folder.mkdir(parents=True)
        (folder / f"{name}.db").write_bytes(b"fixture")
    settings = {"project_root": str(app), "wechat_roots": [str(wechat)]}
    config = app / "config.local.json"
    config.write_text(json.dumps(settings))
    registry = LibraryRegistry(app, settings)
    registry.discover(wechat)
    record = _indexed_account(app, registry, "acct", "ordinary")
    index = registry.context(record.id).index_path
    with sqlite3.connect(index) as con:
        con.executemany(
            """INSERT INTO conversations(account,chat_table,username,display_name,is_group,session_type)
               VALUES(?,?,?,?,?,?)""",
            [
                ("acct", "Msg_pause", "pause", "Paused", 0, 0),
                ("acct", "Msg_hidden", "hidden", "Hidden", 0, 0),
            ],
        )
        con.execute(
            """INSERT INTO conversation_selection(
                   account,username,chat_table,state,source_dbs,selected_at,updated_at)
               VALUES('acct','pause','Msg_pause','paused','[]','now','now')"""
        )
        rows = [
            (f"follow-{number}", "Msg_group", number, f"ordinary {number}")
            for number in range(3, 10)
        ] + [
            ("target", "Msg_group", 10, "跨页 Straße 目标"),
            ("paused-message", "Msg_pause", 1, "paused history"),
            ("hidden-message", "Msg_hidden", 1, "hidden history"),
        ]
        con.executemany(
            """INSERT INTO messages(
                   uid,account,source_db,chat_table,local_id,server_id,local_type,sort_seq,
                   real_sender_id,create_time,status,message_content,compress_content,
                   packed_info_data,row_hash,indexed_at)
               VALUES(?,'acct','message_0.db',?,?,?,1,?,'0',?,0,?,'','',?,'now')""",
            [
                (uid, chat, number, str(number), number, MESSAGE_TIME, text, f"hash-{uid}")
                for uid, chat, number, text in rows
            ],
        )
    grant = {
        "enabled": True,
        "library_id": record.id,
        "scope": scope,
        "conversations": explicit or [],
    }
    policy = app / "data" / "mcp_access.json"
    policy.write_text(json.dumps({"version": 1, "clients": {"test": grant}}))
    return config, record.id, index, policy


class _ChangingGrantReader(LocalIndexReader):
    def __init__(self, config_path, policy, replacement):
        super().__init__(config_path, "test")
        self.policy = policy
        self.replacement = replacement
        self.grant_calls = 0

    def grant(self, library_id=None, conversation_id=None):
        self.grant_calls += 1
        if self.grant_calls == 3:
            self.policy.write_text(json.dumps(self.replacement))
        return super().grant(library_id, conversation_id)


def test_history_tools_intersect_all_synced_with_selected_and_keep_snapshot(tmp_path):
    config, library, index, _ = _sample(tmp_path)

    async def exercise():
        async with Client(create_server(config, "test")) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            for name in ("get_history_overview", "read_history", "get_message_context"):
                assert tools[name].annotations.read_only_hint is True
            overview = (await client.call_tool("get_history_overview", {
                "library_id": library, "limit": 10,
            })).structured_content
            assert {row["id"] for row in overview["conversations"]} == {
                "group@chatroom", "pause",
            }
            assert overview["totals"]["indexed_count"] == 11
            assert overview["totals"]["paused_count"] == 1

            changes_checkpoint = (await client.call_tool("read_changes", {
                "library_id": library, "start": "now",
            })).structured_content
            first = (await client.call_tool("read_history", {
                "library_id": library, "limit": 3, "scan_budget": 3,
            })).structured_content
            assert first["has_more"] and first["total_messages"] == 11
            assert first["progress"]["after_rowid"] <= first["progress"]["upper_rowid"]
            with sqlite3.connect(index) as con:
                con.execute(
                    """INSERT INTO messages(
                           uid,account,source_db,chat_table,local_type,create_time,
                           message_content,row_hash,indexed_at)
                       VALUES('new-after-snapshot','acct','message_0.db','Msg_group',1,1,
                              'new','new','now')"""
                )
            ids = [row["message"]["uid"] for row in first["messages"]]
            cursor = first["cursor"]
            while cursor:
                page = (await client.call_tool("read_history", {
                    "library_id": library, "cursor": cursor,
                    "limit": 3, "scan_budget": 3,
                })).structured_content
                ids.extend(row["message"]["uid"] for row in page["messages"])
                cursor = page["cursor"]
            assert "paused-message" in ids
            assert "hidden-message" not in ids
            assert "new-after-snapshot" not in ids
            assert len(ids) == len(set(ids)) == 11

            changes = (await client.call_tool("read_changes", {
                "library_id": library, "cursor": changes_checkpoint["cursor"],
            })).structured_content
            assert [event["uid"] for event in changes["events"]] == ["new-after-snapshot"]

    asyncio.run(exercise())


def test_search_zero_match_continuation_legacy_shape_and_context_directions(tmp_path):
    config, library, index, _ = _sample(tmp_path)
    with sqlite3.connect(index) as con:
        con.execute(
            "INSERT INTO sender_map(account,source_db,sender_rowid,username) VALUES(?,?,?,?)",
            ("acct", "message_0.db", 0, "wxid_speaker"),
        )
        con.execute(
            """INSERT INTO contacts(account,username,display_name,is_group)
               VALUES('acct','wxid_speaker','兼容发言人',0)"""
        )
        con.execute("UPDATE conversations SET display_name='测试群' WHERE username='hidden'")

    async def exercise():
        async with Client(create_server(config, "test")) as client:
            page = (await client.call_tool("search_messages", {
                "library_id": library, "query": "STRASSE 目标",
                "limit": 2, "scan_budget": 2,
            })).structured_content
            assert page["items"] == [] and page["has_more"]
            assert page["cursor"] and page["scanned"] == 2 and page["total"] == 11
            while page["has_more"] and not page["items"]:
                page = (await client.call_tool("search_messages", {
                    "library_id": library, "query": "STRASSE 目标",
                    "cursor": page["cursor"], "limit": 2, "scan_budget": 2,
                })).structured_content
            assert page["items"] == [{
                "uid": "target",
                "timestamp": MESSAGE_TIME,
                "time": page["items"][0]["time"],
                "chat": {"username": "group@chatroom", "name": "测试群", "is_group": True},
                "sender": {"username": "wxid_speaker", "name": "兼容发言人"},
                "kind": "text", "text": "跨页 Straße 目标", "title": None,
                "solitaire_key": None,
            }]

            sender = (await client.call_tool("search_messages", {
                "library_id": library, "query": "兼容发言人",
                "limit": 100, "scan_budget": 100,
            })).structured_content
            assert len(sender["items"]) == 11
            assert all(item["sender"] == {
                "username": "wxid_speaker", "name": "兼容发言人",
            } for item in sender["items"])
            assert all(item["chat"]["username"] != "hidden" for item in sender["items"])
            chat = (await client.call_tool("search_messages", {
                "library_id": library, "query": "测试群",
                "limit": 100, "scan_budget": 100,
            })).structured_content
            assert len(chat["items"]) == 10
            assert {item["chat"]["username"] for item in chat["items"]} == {"group@chatroom"}

            around = (await client.call_tool("get_message_context", {
                "library_id": library, "conversation_id": "group@chatroom",
                "message_uid": "follow-6", "direction": "around", "limit": 5,
            })).structured_content
            around_ids = [row["uid"] for row in around["messages"]]
            assert "follow-6" in around_ids
            older = (await client.call_tool("get_message_context", {
                "library_id": library, "conversation_id": "group@chatroom",
                "message_uid": around["continuation"]["before_uid"],
                "direction": "before", "limit": 2,
            })).structured_content
            newer = (await client.call_tool("get_message_context", {
                "library_id": library, "conversation_id": "group@chatroom",
                "message_uid": around["continuation"]["after_uid"],
                "direction": "after", "limit": 2,
            })).structured_content
            assert all(row["uid"] not in around_ids for row in older["messages"])
            assert all(row["uid"] not in around_ids for row in newer["messages"])
            assert set(around["continuation"]) == {"before_uid", "after_uid", "around_uid"}

            denied = await client.call_tool("get_message_context", {
                "library_id": library, "conversation_id": "hidden",
                "message_uid": "hidden-message",
            })
            assert denied.is_error

    asyncio.run(exercise())


def test_explicit_grant_intersection_and_cursor_reset_are_tool_errors(tmp_path):
    config, library, index, policy = _sample(
        tmp_path, scope="selected", explicit=["group@chatroom", "hidden"],
    )

    async def exercise():
        async with Client(create_server(config, "test")) as client:
            overview = (await client.call_tool("get_history_overview", {
                "library_id": library,
            })).structured_content
            assert [row["id"] for row in overview["conversations"]] == ["group@chatroom"]
            first = (await client.call_tool("read_history", {
                "library_id": library, "limit": 2, "scan_budget": 2,
            })).structured_content
            with sqlite3.connect(index) as con:
                con.execute(
                    "UPDATE messages SET message_content='edited',row_hash='edited' WHERE uid='target'"
                )
            reset = await client.call_tool("read_history", {
                "library_id": library, "cursor": first["cursor"],
            })
            assert reset.is_error
            assert "cursor_reset_required" in str(reset)

            policy.write_text(json.dumps({"version": 1, "clients": {}}))
            assert (await client.call_tool("get_history_overview", {
                "library_id": library,
            })).is_error

    asyncio.run(exercise())


@pytest.mark.parametrize(("tool", "arguments"), [
    ("get_history_overview", {}),
    ("read_history", {"limit": 2, "scan_budget": 2}),
    ("search_messages", {"query": "ordinary", "limit": 2, "scan_budget": 2}),
    ("list_conversations", {}),
    ("read_messages", {"conversation_id": "group@chatroom"}),
    ("list_content", {}),
    ("get_message_context", {
        "conversation_id": "group@chatroom", "message_uid": "acct-1",
    }),
    ("get_message", {"message_uid": "acct-1"}),
])
def test_read_results_fail_closed_when_grant_shrinks_mid_read(tmp_path, tool, arguments):
    config, library, _, policy = _sample(tmp_path)
    replacement = {"version": 1, "clients": {"test": {
        "enabled": True, "library_id": library, "scope": "selected",
        "conversations": ["group@chatroom"],
    }}}
    reader = _ChangingGrantReader(config, policy, replacement)

    async def exercise():
        async with Client(create_server(config, reader=reader)) as client:
            result = await client.call_tool(tool, {"library_id": library, **arguments})
            assert result.is_error
            assert "scope_changed_retry" in str(result)

    asyncio.run(exercise())


def test_get_message_returns_no_data_when_grant_is_revoked_mid_read(tmp_path):
    config, library, _, policy = _sample(tmp_path)
    reader = _ChangingGrantReader(config, policy, {"version": 1, "clients": {}})

    async def exercise():
        async with Client(create_server(config, reader=reader)) as client:
            result = await client.call_tool("get_message", {
                "library_id": library, "message_uid": "acct-1",
            })
            assert result.is_error
            assert "ordinary" not in str(result)

    asyncio.run(exercise())
