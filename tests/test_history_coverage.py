import sqlite3

import pytest

from noteford.history_coverage import COVERAGE_NOTE, history_overview
from noteford.indexer import _connect_index
from noteford.metadata import ensure_metadata_schema
from noteford.repository import WeChatRepository


def _repo(path):
    con = _connect_index(path)
    ensure_metadata_schema(con)
    conversations = [
        ("acct", "Msg_follow", "follow", "Follow", 0),
        ("acct", "Msg_pause", "pause", "Paused", 1),
        ("acct", "Msg_empty", "empty", "", 0),
        ("acct", "Msg_unselected", "unselected", "Unselected", 0),
        ("other", "Msg_other", "other", "Other account", 0),
    ]
    with con:
        con.executemany(
            "INSERT INTO conversations(account,chat_table,username,display_name,is_group) "
            "VALUES(?,?,?,?,?)",
            conversations,
        )
        con.executemany(
            "INSERT INTO conversation_selection(account,username,chat_table,state,source_dbs,"
            "selected_at,updated_at,last_refresh_at,last_error) VALUES(?,?,?,?,?,?,?,?,?)",
            [
                ("acct", "follow", "Msg_follow", "following", '["private-path"]',
                 "selected", "updated", "refresh-follow", None),
                ("acct", "pause", "Msg_pause", "paused", '["private-path"]',
                 "selected", "updated", "refresh-pause", "private failure text"),
                ("acct", "empty", "Msg_empty", "following", '[]',
                 "selected", "updated", None, None),
                ("other", "other", "Msg_other", "following", '[]',
                 "selected", "updated", "other-refresh", None),
            ],
        )
        for uid, account, source, table, timestamp in [
            ("f1", "acct", "message_0.db", "Msg_follow", 100),
            ("f2", "acct", "message_1.db", "Msg_follow", 300),
            ("p1", "acct", "message_0.db", "Msg_pause", 200),
            ("u1", "acct", "message_0.db", "Msg_unselected", 400),
            ("o1", "other", "message_0.db", "Msg_other", 500),
        ]:
            con.execute(
                "INSERT INTO messages(uid,account,source_db,chat_table,create_time,row_hash,indexed_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (uid, account, source, table, timestamp, uid, "now"),
            )
    con.close()
    return WeChatRepository(path, "acct")


def test_overview_intersects_authorization_selection_and_account(tmp_path):
    repo = _repo(tmp_path / "index.db")
    result = history_overview(
        repo,
        ["follow", "pause", "empty", "unselected", "other"],
        limit=2,
    )

    assert [row["id"] for row in result["conversations"]] == ["follow", "pause"]
    assert result["next_offset"] == 2
    assert result["coverage"] == COVERAGE_NOTE
    assert result["totals"] == {
        "conversation_count": 3,
        "indexed_count": 3,
        "following_count": 2,
        "paused_count": 1,
        "refresh_error_count": 1,
        "source_database_count": 3,
        "source_total_known": False,
    }

    second = history_overview(
        repo,
        ["follow", "pause", "empty", "unselected", "other"],
        limit=2,
        offset=result["next_offset"],
    )
    assert second["next_offset"] is None
    assert second["conversations"] == [{
        "id": "empty",
        "name": "empty",
        "is_group": False,
        "state": "following",
        "indexed_count": 0,
        "earliest_message_timestamp": None,
        "latest_message_timestamp": None,
        "last_refresh_at": None,
        "has_refresh_error": False,
        "source_database_count": 0,
        "source_total_known": False,
    }]


def test_overview_reports_bounds_pause_and_redacts_details(tmp_path):
    repo = _repo(tmp_path / "index.db")
    result = history_overview(repo, ["follow", "pause"], conversation_id="pause")
    assert result["conversations"] == [{
        "id": "pause",
        "name": "Paused",
        "is_group": True,
        "state": "paused",
        "indexed_count": 1,
        "earliest_message_timestamp": 200,
        "latest_message_timestamp": 200,
        "last_refresh_at": "refresh-pause",
        "has_refresh_error": True,
        "source_database_count": 1,
        "source_total_known": False,
    }]
    serialized = repr(result)
    assert "private failure text" not in serialized
    assert "private-path" not in serialized

    followed = history_overview(repo, ["follow"], conversation_id="follow")
    row = followed["conversations"][0]
    assert (row["earliest_message_timestamp"], row["latest_message_timestamp"]) == (100, 300)
    assert row["source_database_count"] == 2


def test_overview_empty_authorization_and_invalid_pagination(tmp_path):
    repo = _repo(tmp_path / "index.db")
    result = history_overview(repo, [])
    assert result["conversations"] == []
    assert result["totals"]["conversation_count"] == 0
    assert result["next_offset"] is None
    with pytest.raises(ValueError, match="invalid_limit"):
        history_overview(repo, ["follow"], limit=0)
    with pytest.raises(ValueError, match="invalid_offset"):
        history_overview(repo, ["follow"], offset=-1)


def test_overview_is_read_only_and_does_not_create_database(tmp_path):
    path = tmp_path / "index.db"
    repo = _repo(path)
    before = path.read_bytes()
    history_overview(repo, ["follow", "pause", "empty"])
    assert path.read_bytes() == before

    path.unlink()
    with pytest.raises(sqlite3.OperationalError):
        history_overview(repo, ["follow"])
    assert not path.exists()
