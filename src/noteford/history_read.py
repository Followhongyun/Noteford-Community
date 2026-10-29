"""Bounded, stable, read-only traversal of selected indexed history."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from typing import Any

from .decode import decode_message


_CURSOR_KEYS = {
    "v", "library", "epoch", "scope", "filters", "upper_rowid",
    "journal_upper", "after_rowid", "total_messages",
}


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _encode_cursor(value: dict[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    envelope = {"p": base64.urlsafe_b64encode(raw).decode(), "h": hashlib.sha256(raw).hexdigest()}
    return base64.urlsafe_b64encode(
        json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode()
    ).decode()


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 4096:
            raise ValueError
        packed = base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True)
        envelope = json.loads(packed.decode("utf-8"))
        if not isinstance(envelope, dict) or set(envelope) != {"p", "h"}:
            raise ValueError
        payload = base64.b64decode(envelope["p"].encode("ascii"), altchars=b"-_", validate=True)
        if not isinstance(envelope["h"], str) or hashlib.sha256(payload).hexdigest() != envelope["h"]:
            raise ValueError
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict) or set(value) != _CURSOR_KEYS:
            raise ValueError
        if type(value["v"]) is not int or value["v"] != 1:
            raise ValueError
        if not isinstance(value["library"], str) or not 1 <= len(value["library"]) <= 512:
            raise ValueError
        if not isinstance(value["epoch"], str) or not 1 <= len(value["epoch"]) <= 512:
            raise ValueError
        if any(
            not isinstance(value[name], str)
            or len(value[name]) != 64
            or any(char not in "0123456789abcdef" for char in value[name])
            for name in ("scope", "filters")
        ):
            raise ValueError
        for name in ("upper_rowid", "journal_upper", "after_rowid", "total_messages"):
            if type(value[name]) is not int or not 0 <= value[name] <= 9_223_372_036_854_775_807:
                raise ValueError
        if value["after_rowid"] > value["upper_rowid"] or value["total_messages"] > value["upper_rowid"]:
            raise ValueError
        return value
    except (ValueError, KeyError, TypeError, UnicodeError, json.JSONDecodeError):
        raise ValueError(
            "cursor_reset_required: 游标无效、资料库已重建、范围已变化或快照已失效。"
        ) from None


def _bounded_int(name: str, value: Any, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"invalid_{name}: 必须是 {minimum} 到 {maximum} 的整数。")
    return value


def _optional_timestamp(name: str, value: Any) -> int | None:
    if value is None:
        return None
    return _bounded_int(name, value, 0, 9_223_372_036_854_775_807)


def _normalize_conversations(conversations: Any, *, all_indexed: bool = False) -> list[str]:
    if not isinstance(conversations, list) or (not all_indexed and len(conversations) > 1000):
        raise ValueError("invalid_conversations: 必须是最多 1000 项的会话 ID 列表。")
    if any(not isinstance(item, str) or not item or len(item) > 512 for item in conversations):
        raise ValueError("invalid_conversations: 会话 ID 必须是 1 到 512 字符的字符串。")
    return sorted(set(conversations))


def _selected_scope(con: sqlite3.Connection, account: str, authorized: list[str], include_unselected: bool = False) -> list[tuple[str, str]]:
    if include_unselected:
        return [(str(row[0]), str(row[1])) for row in con.execute(
            """SELECT username,chat_table FROM conversations
               WHERE account=? AND username IN (SELECT value FROM json_each(?))
               ORDER BY username,chat_table""", (account, json.dumps(authorized, ensure_ascii=False)))]
    try:
        rows = con.execute(
            """SELECT c.username,c.chat_table
               FROM conversations c
               JOIN conversation_selection s
                 ON s.account=c.account AND s.username=c.username AND s.chat_table=c.chat_table
               WHERE c.account=? AND s.state IN ('following','paused')
                 AND c.username IN (SELECT value FROM json_each(?))
               ORDER BY c.username,c.chat_table""",
            (account, json.dumps(authorized, ensure_ascii=False)),
        ).fetchall()
    except sqlite3.OperationalError:
        raise ValueError("history_scope_unavailable: 资料库缺少会话选择信息，请先刷新索引。") from None
    return [(str(row["username"]), str(row["chat_table"])) for row in rows]


def _stream_state(con: sqlite3.Connection) -> tuple[str, int]:
    try:
        row = con.execute("SELECT epoch FROM mcp_stream WHERE id=1").fetchone()
        if row is None or not isinstance(row[0], str) or not row[0]:
            raise ValueError
        upper = con.execute("SELECT COALESCE(MAX(sequence),0) FROM mcp_changes").fetchone()[0]
        if type(upper) is not int or upper < 0:
            raise ValueError
        return row[0], upper
    except (sqlite3.OperationalError, TypeError, ValueError):
        raise ValueError(
            "history_index_unavailable: 缺少索引变更 journal，请先在新版桌面应用中刷新该资料库。"
        ) from None


def _snapshot_changed(
    con: sqlite3.Connection,
    account: str,
    chats_json: str,
    journal_upper: int,
    current_upper: int,
) -> bool:
    if current_upper < journal_upper:
        return True
    row = con.execute(
        """SELECT 1
           FROM mcp_changes e
           WHERE e.account=?
             AND e.chat_table IN (SELECT value FROM json_each(?))
             AND e.sequence>? AND e.sequence<=?
             AND EXISTS (
                 SELECT 1 FROM mcp_changes old
                 WHERE old.account=e.account AND old.chat_table=e.chat_table
                   AND old.uid=e.uid AND old.sequence<=?
             )
           LIMIT 1""",
        (account, chats_json, journal_upper, current_upper, journal_upper),
    ).fetchone()
    return row is not None


def _raise_reset() -> None:
    raise ValueError(
        "cursor_reset_required: 快照内已有消息被编辑或删除、索引已重建或授权/选入范围发生变化，请从头重新读取。"
    )


def read_history(
    repo,
    library_id: str,
    conversations: list[str],
    *,
    cursor: str | None = None,
    conversation_id: str | None = None,
    query: str | None = None,
    start_timestamp: int | None = None,
    end_timestamp: int | None = None,
    limit: int = 80,
    scan_budget: int = 2000,
    include_unselected: bool = False,
) -> dict[str, Any]:
    """Read one bounded page in stable SQLite insertion (``rowid``) order.

    ``conversations`` is the caller's authorization allow-list. Only entries
    also present in ``conversation_selection`` as ``following`` or ``paused``
    participate. A continuation is invalidated by edits/deletes to snapshot
    messages, a journal epoch change, or an authorization/selection change.
    Inserts after the first call are outside the snapshot and do not disturb it.
    """
    if not isinstance(library_id, str) or not 1 <= len(library_id) <= 512:
        raise ValueError("invalid_library_id")
    authorized = _normalize_conversations(conversations, all_indexed=include_unselected)
    if conversation_id is not None and (
        not isinstance(conversation_id, str) or not 1 <= len(conversation_id) <= 512
    ):
        raise ValueError("invalid_conversation_id")
    if query is not None and (not isinstance(query, str) or len(query) > 1000):
        raise ValueError("invalid_query: 查询词最多 1000 字符。")
    needle = (query or "").strip().casefold()
    start = _optional_timestamp("start_timestamp", start_timestamp)
    end = _optional_timestamp("end_timestamp", end_timestamp)
    if start is not None and end is not None and start > end:
        raise ValueError("invalid_timestamp_range: start_timestamp 不得晚于 end_timestamp。")
    limit = _bounded_int("limit", limit, 1, 200)
    scan_budget = _bounded_int("scan_budget", scan_budget, 1, 20_000)

    filter_value = {
        "conversation_id": conversation_id,
        "query": needle,
        "start_timestamp": start,
        "end_timestamp": end,
    }
    filters_hash = _digest(filter_value)

    with repo._connect() as con:
        con.execute("BEGIN")
        scope = _selected_scope(con, repo.account, authorized, include_unselected)
        if conversation_id is not None and conversation_id not in {item[0] for item in scope}:
            raise ValueError("conversation_not_authorized_or_selected")
        scan_scope = [item for item in scope if conversation_id is None or item[0] == conversation_id]
        chats = [item[1] for item in scan_scope]
        chats_json = json.dumps(chats, ensure_ascii=False)
        scope_hash = _digest({"authorized": authorized, "selected": scope})
        epoch, current_journal_upper = _stream_state(con)

        if cursor is None:
            upper_rowid = int(con.execute(
                """SELECT COALESCE(MAX(rowid),0) FROM messages
                   WHERE account=? AND chat_table IN (SELECT value FROM json_each(?))""",
                (repo.account, chats_json),
            ).fetchone()[0])
            journal_upper = current_journal_upper
            after_rowid = 0
            total_messages = int(con.execute(
                """SELECT COUNT(*) FROM messages m
                   WHERE m.account=? AND m.chat_table IN (SELECT value FROM json_each(?))
                     AND m.rowid<=?""",
                (repo.account, chats_json, upper_rowid),
            ).fetchone()[0])
        else:
            value = _decode_cursor(cursor)
            if (
                value["library"] != library_id
                or value["epoch"] != epoch
                or value["scope"] != scope_hash
                or value["filters"] != filters_hash
                or _snapshot_changed(
                    con, repo.account, chats_json, value["journal_upper"], current_journal_upper
                )
            ):
                _raise_reset()
            upper_rowid = value["upper_rowid"]
            journal_upper = value["journal_upper"]
            after_rowid = value["after_rowid"]
            total_messages = value["total_messages"]

        rows = con.execute(
            """SELECT m.rowid,m.uid,m.local_type,m.create_time,m.message_content,
                      m.compress_content,m.packed_info_data,c.is_group,c.display_name,
                      sm.username AS sender_username,sc.display_name AS sender_name
               FROM messages m
               JOIN conversations c ON c.account=m.account AND c.chat_table=m.chat_table
               LEFT JOIN sender_map sm ON sm.account=m.account
                 AND sm.source_db=m.source_db
                 AND sm.sender_rowid=CAST(m.real_sender_id AS INTEGER)
               LEFT JOIN contacts sc ON sc.account=m.account AND sc.username=sm.username
               WHERE m.account=? AND m.chat_table IN (SELECT value FROM json_each(?))
                 AND m.rowid>? AND m.rowid<=?
               ORDER BY m.rowid LIMIT ?""",
            (repo.account, chats_json, after_rowid, upper_rowid, scan_budget),
        ).fetchall()

        matched_uids: list[str] = []
        scanned = 0
        checkpoint = after_rowid
        for row in rows:
            scanned += 1
            checkpoint = int(row["rowid"])
            timestamp = int(row["create_time"] or 0)
            if start is not None and timestamp < start:
                continue
            if end is not None and timestamp >= end:
                continue
            if needle:
                decoded = decode_message(
                    row["local_type"], row["message_content"], row["compress_content"],
                    row["packed_info_data"], bool(row["is_group"]),
                )
                haystack = " ".join(
                    str(value or "") for value in (
                        decoded.text, decoded.title, row["display_name"],
                        row["sender_name"], row["sender_username"],
                    )
                ).casefold()
                if needle not in haystack:
                    continue
            matched_uids.append(str(row["uid"]))
            if len(matched_uids) == limit:
                break

        has_more = con.execute(
            """SELECT 1 FROM messages m
               WHERE m.account=? AND m.chat_table IN (SELECT value FROM json_each(?))
                 AND m.rowid>? AND m.rowid<=? LIMIT 1""",
            (repo.account, chats_json, checkpoint, upper_rowid),
        ).fetchone() is not None

    messages = []
    allowed_return_scope = {item[0] for item in scan_scope}
    for uid in matched_uids:
        item = repo.message_by_uid(uid)
        if item is None or item.get("conversation", {}).get("username") not in allowed_return_scope:
            _raise_reset()
        messages.append(item)

    # Recheck the journal and selected scope after materializing messages. This
    # closes the practical race between the read transaction and N<=limit full
    # message reads while still ignoring post-snapshot inserts.
    with repo._connect() as con:
        con.execute("BEGIN")
        latest_scope = _selected_scope(con, repo.account, authorized, include_unselected)
        latest_epoch, latest_upper = _stream_state(con)
        if (
            _digest({"authorized": authorized, "selected": latest_scope}) != scope_hash
            or latest_epoch != epoch
            or _snapshot_changed(con, repo.account, chats_json, journal_upper, latest_upper)
        ):
            _raise_reset()

    next_cursor = None
    if has_more:
        next_cursor = _encode_cursor({
            "v": 1,
            "library": library_id,
            "epoch": epoch,
            "scope": scope_hash,
            "filters": filters_hash,
            "upper_rowid": upper_rowid,
            "journal_upper": journal_upper,
            "after_rowid": checkpoint,
            "total_messages": total_messages,
        })
    return {
        "library_id": library_id,
        "messages": messages,
        "cursor": next_cursor,
        "has_more": has_more,
        "scanned": scanned,
        "total_messages": total_messages,
        "matched_count_this_page": len(messages),
        "coverage": (
            ("遍历已授权账号的全部已索引消息；顺序是稳定的 SQLite " if include_unselected else
             "仅遍历调用方授权且当前为 following/paused 的已索引消息；顺序是稳定的 SQLite ") +
            "入库 rowid 顺序，不代表发送时间顺序。首次调用后的新增消息不属于本快照；快照内消息"
            "发生编辑、删除或索引重建时必须从头开始。原始链接和媒体仍仅按 message_by_uid 的元数据返回。"
        ),
        "progress": {"after_rowid": checkpoint, "upper_rowid": upper_rowid},
    }
