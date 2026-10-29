import sqlite3

import pytest

from noteford.history_read import read_history
from noteford.indexer import _connect_index
from noteford.metadata import ensure_metadata_schema
from noteford.repository import WeChatRepository


def _build_repo(tmp_path, *, count=12):
    path = tmp_path / "history.db"
    con = _connect_index(path)
    ensure_metadata_schema(con)
    with con:
        con.executemany(
            """INSERT INTO conversations(account,chat_table,username,display_name,is_group,session_type)
               VALUES(?,?,?,?,?,?)""",
            [
                ("acct", "Msg_follow", "follow", "Follow", 0, 0),
                ("acct", "Msg_pause", "pause", "Pause", 0, 0),
                ("acct", "Msg_hidden", "hidden", "Hidden", 0, 0),
            ],
        )
        con.executemany(
            """INSERT INTO conversation_selection(
                   account,username,chat_table,state,source_dbs,selected_at,updated_at)
               VALUES(?,?,?,?,?,?,?)""",
            [
                ("acct", "follow", "Msg_follow", "following", "[]", "now", "now"),
                ("acct", "pause", "Msg_pause", "paused", "[]", "now", "now"),
            ],
        )
        rows = [
            (
                f"u{i:06d}", "acct", "message.db", "Msg_follow", i, str(i), 1,
                i, "0", 1000, 0, f"ordinary {i}".encode(), b"", b"", f"h{i}", "now",
            )
            for i in range(1, count + 1)
        ]
        con.executemany(
            """INSERT INTO messages(
                   uid,account,source_db,chat_table,local_id,server_id,local_type,sort_seq,
                   real_sender_id,create_time,status,message_content,compress_content,
                   packed_info_data,row_hash,indexed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        con.execute(
            """INSERT INTO messages(
                   uid,account,source_db,chat_table,local_id,server_id,local_type,sort_seq,
                   real_sender_id,create_time,status,message_content,compress_content,
                   packed_info_data,row_hash,indexed_at)
               VALUES('paused','acct','message.db','Msg_pause',1,'p',1,1,'0',1000,0,
                      'paused history','','','paused-hash','now')"""
        )
        con.execute(
            """INSERT INTO messages(
                   uid,account,source_db,chat_table,local_id,server_id,local_type,sort_seq,
                   real_sender_id,create_time,status,message_content,compress_content,
                   packed_info_data,row_hash,indexed_at)
               VALUES('hidden','acct','message.db','Msg_hidden',1,'x',1,1,'0',1000,0,
                      'hidden history','','','hidden-hash','now')"""
        )
    con.close()
    return WeChatRepository(path, "acct"), path


def _read_all(repo, **kwargs):
    cursor = None
    messages = []
    pages = []
    while True:
        page = read_history(
            repo, "library", ["follow", "pause", "hidden"], cursor=cursor, **kwargs
        )
        pages.append(page)
        messages.extend(page["messages"])
        if not page["has_more"]:
            assert page["cursor"] is None
            return messages, pages
        assert page["cursor"]
        cursor = page["cursor"]


def test_searches_more_than_twenty_thousand_with_bounded_zero_match_pages(tmp_path):
    repo, path = _build_repo(tmp_path, count=20_050)
    with sqlite3.connect(path) as con:
        xml = "<msg><appmsg><title>跨页Straße目标标题</title><des>正文</des></appmsg></msg>"
        con.execute(
            "UPDATE messages SET local_type=?,message_content=?,row_hash=? WHERE uid=?",
            (49, xml, "target", "u020050"),
        )

    calls = []
    original = repo.message_by_uid

    def counted(uid):
        calls.append(uid)
        return original(uid)

    repo.message_by_uid = counted
    messages, pages = _read_all(repo, query="STRASSE目标标题", limit=5, scan_budget=997)
    assert [item["message"]["uid"] for item in messages] == ["u020050"]
    assert len(pages) > 20
    assert any(page["has_more"] and page["matched_count_this_page"] == 0 for page in pages)
    assert all(page["scanned"] <= 997 for page in pages)
    assert pages[0]["total_messages"] == 20_051  # following + paused; hidden is not selected
    assert calls == ["u020050"]
    assert "入库 rowid 顺序" in pages[0]["coverage"]


def test_same_second_pages_have_no_gaps_or_duplicates_and_include_paused(tmp_path):
    repo, _ = _build_repo(tmp_path, count=23)
    messages, pages = _read_all(repo, limit=4, scan_budget=7)
    ids = [item["message"]["uid"] for item in messages]
    assert ids == [*(f"u{i:06d}" for i in range(1, 24)), "paused"]
    assert len(ids) == len(set(ids))
    assert all(item["message"]["timestamp"] == 1000 for item in messages)
    assert pages[-1]["cursor"] is None


def test_search_matches_chat_and_sender_names_without_unselected_leak(tmp_path):
    repo, path = _build_repo(tmp_path, count=3)
    with sqlite3.connect(path) as con:
        con.execute(
            "INSERT INTO sender_map(account,source_db,sender_rowid,username) VALUES(?,?,?,?)",
            ("acct", "message.db", 0, "wxid_speaker"),
        )
        con.execute(
            """INSERT INTO contacts(account,username,display_name,is_group)
               VALUES('acct','wxid_speaker','兼容发言人',0)"""
        )
        con.execute(
            "UPDATE conversations SET display_name='授权同名目标' WHERE username='follow'"
        )
        con.execute(
            "UPDATE conversations SET display_name='授权同名目标' WHERE username='hidden'"
        )
    by_sender, _ = _read_all(repo, query="兼容发言人", limit=10, scan_budget=10)
    assert {item["message"]["uid"] for item in by_sender} == {
        "u000001", "u000002", "u000003", "paused",
    }
    by_chat, _ = _read_all(repo, query="授权同名目标", limit=10, scan_budget=10)
    assert [item["message"]["uid"] for item in by_chat] == [
        "u000001", "u000002", "u000003",
    ]
    assert all(item["conversation"]["username"] != "hidden" for item in by_sender + by_chat)


def test_post_snapshot_old_and_new_timestamp_inserts_do_not_move_pages(tmp_path):
    repo, path = _build_repo(tmp_path, count=9)
    first = read_history(repo, "library", ["follow"], limit=3, scan_budget=3)
    with sqlite3.connect(path) as con:
        for uid, timestamp in (("late-old", 1), ("late-new", 999_999)):
            con.execute(
                """INSERT INTO messages(
                       uid,account,source_db,chat_table,local_id,server_id,local_type,sort_seq,
                       real_sender_id,create_time,status,message_content,compress_content,
                       packed_info_data,row_hash,indexed_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (uid, "acct", "message.db", "Msg_follow", None, uid, 1, 0, "0", timestamp,
                 0, uid, b"", b"", uid, "now"),
            )
    cursor = first["cursor"]
    ids = [item["message"]["uid"] for item in first["messages"]]
    while cursor:
        page = read_history(repo, "library", ["follow"], cursor=cursor, limit=3, scan_budget=3)
        ids.extend(item["message"]["uid"] for item in page["messages"])
        cursor = page["cursor"]
    assert ids == [f"u{i:06d}" for i in range(1, 10)]
    assert first["total_messages"] == 9


def test_unselected_tail_rowid_reuse_cannot_enter_selected_snapshot(tmp_path):
    repo, path = _build_repo(tmp_path, count=5)
    first = read_history(repo, "library", ["follow", "pause", "hidden"], limit=2, scan_budget=2)
    snapshot_upper = first["progress"]["upper_rowid"]
    with sqlite3.connect(path) as con:
        hidden_rowid = con.execute(
            "SELECT rowid FROM messages WHERE uid='hidden'"
        ).fetchone()[0]
        assert hidden_rowid > snapshot_upper
        con.execute("DELETE FROM messages WHERE uid='hidden'")
        con.execute(
            """INSERT INTO messages(
                   uid,account,source_db,chat_table,local_type,create_time,
                   message_content,row_hash,indexed_at)
               VALUES('reused-tail','acct','message.db','Msg_follow',1,1,
                      'new after snapshot','new-after','now')"""
        )
        assert con.execute(
            "SELECT rowid FROM messages WHERE uid='reused-tail'"
        ).fetchone()[0] == hidden_rowid
    ids = [item["message"]["uid"] for item in first["messages"]]
    cursor = first["cursor"]
    while cursor:
        page = read_history(
            repo, "library", ["follow", "pause", "hidden"], cursor=cursor,
            limit=2, scan_budget=2,
        )
        ids.extend(item["message"]["uid"] for item in page["messages"])
        cursor = page["cursor"]
    assert "reused-tail" not in ids
    assert ids == [*(f"u{i:06d}" for i in range(1, 6)), "paused"]


@pytest.mark.parametrize("mutation", ["edit", "delete"])
def test_existing_snapshot_edit_or_delete_requires_restart(tmp_path, mutation):
    repo, path = _build_repo(tmp_path, count=6)
    first = read_history(repo, "library", ["follow"], limit=2, scan_budget=2)
    with sqlite3.connect(path) as con:
        if mutation == "edit":
            con.execute(
                "UPDATE messages SET message_content='changed',row_hash='changed' WHERE uid='u000005'"
            )
        else:
            con.execute("DELETE FROM messages WHERE uid='u000005'")
    with pytest.raises(ValueError, match="cursor_reset_required"):
        read_history(repo, "library", ["follow"], cursor=first["cursor"])


@pytest.mark.parametrize(
    "change",
    ["other_library", "auth_change", "selection_change", "filter_change", "epoch_change", "bad"],
)
def test_cursor_rejects_cross_scope_and_invalid_continuations(tmp_path, change):
    repo, path = _build_repo(tmp_path, count=6)
    first = read_history(repo, "library", ["follow", "pause"], limit=2, scan_budget=2)
    kwargs = {"cursor": first["cursor"]}
    library = "library"
    conversations = ["follow", "pause"]
    if change == "other_library":
        library = "elsewhere"
    elif change == "auth_change":
        conversations = ["follow"]
    elif change == "selection_change":
        with sqlite3.connect(path) as con:
            con.execute("DELETE FROM conversation_selection WHERE username='pause'")
    elif change == "filter_change":
        kwargs["query"] = "ordinary"
    elif change == "epoch_change":
        with sqlite3.connect(path) as con:
            con.execute("UPDATE mcp_stream SET epoch='replacement'")
    else:
        kwargs["cursor"] = "%%%not-base64%%%"
    with pytest.raises(ValueError, match="cursor_reset_required"):
        read_history(repo, library, conversations, **kwargs)


def test_conversation_and_time_filters_are_bound_and_authorized(tmp_path):
    repo, _ = _build_repo(tmp_path, count=5)
    page = read_history(
        repo, "library", ["follow", "pause"], conversation_id="pause",
        start_timestamp=1000, end_timestamp=1001,
    )
    assert [item["message"]["uid"] for item in page["messages"]] == ["paused"]
    assert page["total_messages"] == 1
    with pytest.raises(ValueError, match="conversation_not_authorized_or_selected"):
        read_history(repo, "library", ["follow", "hidden"], conversation_id="hidden")


def test_missing_journal_is_explicit_and_failed_reads_create_nothing(tmp_path):
    repo, path = _build_repo(tmp_path, count=3)
    with sqlite3.connect(path) as con:
        for trigger in ("mcp_message_insert", "mcp_message_update", "mcp_message_delete"):
            con.execute(f"DROP TRIGGER {trigger}")
        con.execute("DROP TABLE mcp_changes")
        con.execute("DROP TABLE mcp_stream")
        before = con.execute(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
    with pytest.raises(ValueError, match="history_index_unavailable"):
        read_history(repo, "library", ["follow"])
    with sqlite3.connect(path) as con:
        after = con.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    assert after == before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 0},
        {"scan_budget": 20_001},
        {"start_timestamp": True},
        {"start_timestamp": 2, "end_timestamp": 1},
        {"query": "x" * 1001},
        {"conversations": "follow"},
    ],
)
def test_input_bounds_are_strict(tmp_path, kwargs):
    repo, _ = _build_repo(tmp_path, count=1)
    conversations = kwargs.pop("conversations", ["follow"])
    with pytest.raises(ValueError):
        read_history(repo, "library", conversations, **kwargs)
