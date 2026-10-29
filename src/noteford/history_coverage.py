"""Read-only coverage summaries for owner-authorized selected conversations."""
from __future__ import annotations

import json
from typing import Any, Iterable


COVERAGE_NOTE = "已选会话的已索引历史；本机/微信缺失的旧记录未知"


def _authorized(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if isinstance(value, str) and value))


def history_overview(
    repo,
    allowed_conversations,
    *,
    limit: int = 50,
    offset: int = 0,
    conversation_id: str | None = None,
    include_unselected: bool = False,
) -> dict[str, Any]:
    """Summarize indexed history for authorized following and paused chats.

    This query does not claim that the local index contains every message that
    ever existed in WeChat. Raw source row totals are not persisted, so only
    counts and time bounds from the current normalized index are reported.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("invalid_limit")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("invalid_offset")
    if conversation_id is not None and (not isinstance(conversation_id, str) or not conversation_id):
        raise ValueError("invalid_conversation_id")

    allowed = _authorized(allowed_conversations)
    allowed_json = json.dumps(allowed, ensure_ascii=False)
    conversation_filter = " AND c.username=?" if conversation_id is not None else ""
    base_params: list[Any] = [repo.account, allowed_json]
    if conversation_id is not None:
        base_params.append(conversation_id)

    selected_sql = f"""
        FROM conversation_selection s
        JOIN conversations c
          ON c.account=s.account AND c.username=s.username
        LEFT JOIN messages m
          ON m.account=c.account AND m.chat_table=c.chat_table
        WHERE s.account=?
          AND s.state IN ('following','paused')
          AND c.username IN (SELECT value FROM json_each(?))
          {conversation_filter}
    """
    if include_unselected:
        selected_sql = f"""
            FROM conversations c
            LEFT JOIN conversation_selection s
              ON c.account=s.account AND c.username=s.username AND c.chat_table=s.chat_table
            LEFT JOIN messages m ON m.account=c.account AND m.chat_table=c.chat_table
            WHERE c.account=? AND c.username IN (SELECT value FROM json_each(?))
            {conversation_filter}
        """

    with repo._connect() as con:
        rows = con.execute(
            f"""
            SELECT c.username AS id, c.display_name AS name, c.is_group,
                   s.state, COUNT(m.uid) AS indexed_count,
                   MIN(m.create_time) AS earliest_message_timestamp,
                   MAX(m.create_time) AS latest_message_timestamp,
                   s.last_refresh_at,
                   CASE WHEN s.last_error IS NOT NULL AND s.last_error<>''
                        THEN 1 ELSE 0 END AS has_refresh_error,
                   COUNT(DISTINCT m.source_db) AS source_database_count
            {selected_sql}
            GROUP BY c.username, c.display_name, c.is_group, s.state,
                     s.last_refresh_at, s.last_error
            ORDER BY COALESCE(MAX(m.create_time),0) DESC, c.username
            LIMIT ? OFFSET ?
            """,
            (*base_params, limit, offset),
        ).fetchall()
        totals = con.execute(
            f"""
            SELECT COUNT(*) AS conversation_count,
                   COALESCE(SUM(indexed_count),0) AS indexed_count,
                   COALESCE(SUM(state='following'),0) AS following_count,
                   COALESCE(SUM(state='paused'),0) AS paused_count,
                   COALESCE(SUM(has_refresh_error),0) AS refresh_error_count,
                   COALESCE(SUM(source_database_count),0) AS source_database_count
            FROM (
                SELECT c.username, s.state, COUNT(m.uid) AS indexed_count,
                       CASE WHEN s.last_error IS NOT NULL AND s.last_error<>''
                            THEN 1 ELSE 0 END AS has_refresh_error,
                       COUNT(DISTINCT m.source_db) AS source_database_count
                {selected_sql}
                GROUP BY c.username, s.state, s.last_error
            ) selected
            """,
            base_params,
        ).fetchone()

    conversations = [
        {
            "id": row["id"],
            "name": row["name"] or row["id"],
            "is_group": bool(row["is_group"]),
            "state": row["state"] or "unselected",
            "indexed_count": int(row["indexed_count"]),
            "earliest_message_timestamp": (
                int(row["earliest_message_timestamp"])
                if row["earliest_message_timestamp"] is not None else None
            ),
            "latest_message_timestamp": (
                int(row["latest_message_timestamp"])
                if row["latest_message_timestamp"] is not None else None
            ),
            "last_refresh_at": row["last_refresh_at"],
            "has_refresh_error": bool(row["has_refresh_error"]),
            "source_database_count": int(row["source_database_count"]),
            "source_total_known": False,
        }
        for row in rows
    ]
    total_count = int(totals["conversation_count"])
    return {
        "conversations": conversations,
        "totals": {
            "conversation_count": total_count,
            "indexed_count": int(totals["indexed_count"]),
            "following_count": int(totals["following_count"]),
            "paused_count": int(totals["paused_count"]),
            "refresh_error_count": int(totals["refresh_error_count"]),
            "source_database_count": int(totals["source_database_count"]),
            "source_total_known": False,
        },
        "next_offset": offset + len(conversations)
        if offset + len(conversations) < total_count else None,
        "coverage": "已授权账号的全部已索引历史；本机/微信缺失的旧记录未知" if include_unselected else COVERAGE_NOTE,
    }
