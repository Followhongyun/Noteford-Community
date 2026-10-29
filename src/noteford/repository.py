from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .decode import decode_message as shared_decode_message, DecodedContent
from .solitaire import read_memberships


def decode_message(*args, **kwargs):
    import sys
    if sys.platform == 'darwin':
        from .mac_decode import decode_message as decode_mac_message
        return decode_mac_message(*args, **kwargs)
    return shared_decode_message(*args, **kwargs)


def _payment_metadata(decoded: DecodedContent, self_username: str | None) -> dict | None:
    if decoded.kind not in {'transfer', 'red_envelope'}:
        return None
    return {
        'recipient_is_self': decoded.payment_receiver == self_username
        if decoded.payment_receiver and self_username else None,
        'expires_at': decoded.payment_expires_at,
    }


INTERNAL_SESSION_USERNAMES = (
    "brandsessionholder",
    "brandservicesessionholder",
    "@opencustomerservicemsg",
)


def _empty_conversation_preview(username: str, is_group: bool) -> str:
    if username.startswith("gh_"):
        return "[公众号]"
    if username.endswith("@openim"):
        return "[企业微信]"
    if username.endswith("@weclaw"):
        return "[微信助手]"
    if is_group:
        return "暂无本地消息"
    return "暂无本地消息"


class WeChatRepository:
    """Read-only query facade over the normalized local WeChat index."""

    def __init__(self, index_path: Path, account: str):
        self.index_path = index_path.resolve()
        self.account = account
        if not self.index_path.is_file():
            raise FileNotFoundError(self.index_path)

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(
            f"file:{self.index_path.as_posix()}?mode=ro",
            uri=True,
            timeout=5,
        )
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only=ON")
            con.execute("PRAGMA busy_timeout=5000")
            yield con
        finally:
            con.close()

    def _mac_group_names(self, room: str | None = None) -> dict:
        from .platform_seam import library_source_kind
        if library_source_kind() != 'decrypted_archive':
            return {}
        from .mac_metadata import group_members
        return {(r['room'], r['username']): r['name']
                for r in (group_members(self.index_path, self.account, room) or [])}

    def red_envelope_ids(self, username: str) -> dict[str, str]:
        with self._connect() as con:
            return {
                row['uid']: str(row['server_id'])
                for row in con.execute(
                    'SELECT m.uid,m.server_id FROM messages m JOIN conversations c '
                    'ON c.account=m.account AND c.chat_table=m.chat_table '
                    'WHERE m.account=? AND c.username=? AND m.local_type=?',
                    (self.account, username, (2001 << 32) | 49),
                )
            }

    @staticmethod
    def _time_iso(timestamp: int | None) -> str | None:
        if not timestamp:
            return None
        return datetime.fromtimestamp(int(timestamp), timezone.utc).astimezone().isoformat()

    @staticmethod
    def _bounded(value: int, *, minimum: int, maximum: int) -> int:
        return max(minimum, min(maximum, int(value)))

    def status(self) -> dict[str, Any]:
        with self._connect() as con:
            row = con.execute(
                """
                SELECT
                    COUNT(*) AS messages,
                    COALESCE(MAX(create_time), 0) AS latest
                FROM messages
                WHERE account=?
                """,
                (self.account,),
            ).fetchone()
            conversations = con.execute(
                """
                SELECT COUNT(*)
                FROM conversations
                WHERE account=? AND username NOT IN (?,?,?)
                """,
                (self.account, *INTERNAL_SESSION_USERNAMES),
            ).fetchone()[0]
            contacts = con.execute(
                "SELECT COUNT(*) FROM contacts WHERE account=?",
                (self.account,),
            ).fetchone()[0]
            profile = con.execute(
                """
                SELECT self_username, self_display_name, inference_confidence
                FROM account_profile
                WHERE account=?
                """,
                (self.account,),
            ).fetchone()
            following = con.execute(
                """
                SELECT COUNT(*) FROM conversation_selection
                WHERE account=? AND state='following'
                """,
                (self.account,),
            ).fetchone()[0]
            paused = con.execute(
                """
                SELECT COUNT(*) FROM conversation_selection
                WHERE account=? AND state='paused'
                """,
                (self.account,),
            ).fetchone()[0]
            managed_messages = con.execute(
                """
                SELECT COUNT(*)
                FROM messages m
                JOIN conversations c
                  ON c.account=m.account AND c.chat_table=m.chat_table
                JOIN conversation_selection s
                  ON s.account=c.account AND s.username=c.username
                WHERE m.account=?
                """,
                (self.account,),
            ).fetchone()[0]
        return {
            "account": self.account,
            "messages": int(row["messages"]),
            "conversations": int(conversations),
            "contacts": int(contacts),
            "following_conversations": int(following),
            "paused_conversations": int(paused),
            "managed_messages": int(managed_messages),
            "legacy_unselected_messages": max(0, int(row["messages"]) - int(managed_messages)),
            "latest_timestamp": int(row["latest"] or 0),
            "latest_time": self._time_iso(row["latest"]),
            "self_identity_resolved": bool(profile and profile["self_username"]),
            "self_username": profile["self_username"] if profile else None,
            "self_display_name": profile["self_display_name"] if profile else None,
            "self_confidence": float(profile["inference_confidence"] or 0) if profile else 0.0,
        }

    def _self_username(self, con: sqlite3.Connection) -> str | None:
        row = con.execute(
            "SELECT self_username FROM account_profile WHERE account=?",
            (self.account,),
        ).fetchone()
        return str(row[0]) if row and row[0] else None

    def list_conversations(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        query: str | None = None,
        selection_state: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = self._bounded(limit, minimum=1, maximum=500)
        offset = max(0, int(offset))
        needle = (query or "").strip()
        params: list[Any] = [self.account, *INTERNAL_SESSION_USERNAMES]
        where = "WHERE c.account=? AND c.username NOT IN (?,?,?)"
        if needle:
            where += " AND (c.display_name LIKE ? OR c.username LIKE ?)"
            pattern = f"%{needle}%"
            params.extend([pattern, pattern])
        if selection_state:
            if selection_state not in {"following", "paused", "unselected"}:
                raise ValueError("invalid_selection_state")
            if selection_state == "unselected":
                where += " AND s.state IS NULL"
            else:
                where += " AND s.state=?"
                params.append(selection_state)
        params.extend([limit, offset])

        sql = f"""
        SELECT
            c.username,
            c.chat_table,
            c.display_name,
            c.is_group,
            c.sort_timestamp,
            c.last_timestamp,
            (
                SELECT COUNT(*)
                FROM messages mc
                WHERE mc.account=c.account AND mc.chat_table=c.chat_table
            ) AS message_count,
            r.create_time AS latest_create_time,
            r.local_type AS latest_local_type,
            r.message_content AS latest_message_content,
            r.compress_content AS latest_compress_content,
            r.packed_info_data AS latest_packed_info_data,
            s.state AS selection_state
        FROM conversations c
        LEFT JOIN conversation_selection s
          ON s.account=c.account AND s.username=c.username
        LEFT JOIN messages r
          ON r.uid=(
              SELECT ml.uid
              FROM messages ml
              WHERE ml.account=c.account AND ml.chat_table=c.chat_table
              ORDER BY ml.create_time DESC, ml.sort_seq DESC, ml.server_id DESC, ml.uid DESC
              LIMIT 1
          )
        {where}
        ORDER BY COALESCE(r.create_time, c.sort_timestamp, 0) DESC, c.display_name COLLATE NOCASE
        LIMIT ? OFFSET ?
        """
        with self._connect() as con:
            rows = con.execute(sql, params).fetchall()

        result: list[dict[str, Any]] = []
        for row in rows:
            if row["latest_local_type"] is None:
                decoded = None
                preview = _empty_conversation_preview(
                    str(row["username"]),
                    bool(row["is_group"]),
                )
            else:
                decoded = decode_message(
                    row["latest_local_type"],
                    row["latest_message_content"],
                    row["latest_compress_content"],
                    row["latest_packed_info_data"],
                    bool(row["is_group"]),
                )
                preview = decoded.text.strip()
                if not preview:
                    preview = f"[{decoded.kind}]"
                if len(preview) > 120:
                    preview = preview[:120] + "…"
            result.append(
                {
                    "username": row["username"],
                    "chat_table": row["chat_table"],
                    "name": row["display_name"],
                    "is_group": bool(row["is_group"]),
                    "message_count": int(row["message_count"] or 0),
                    "latest_timestamp": int(row["latest_create_time"] or row["last_timestamp"] or 0),
                    "latest_time": self._time_iso(row["latest_create_time"] or row["last_timestamp"]),
                    "latest_kind": decoded.kind if decoded is not None else None,
                    "preview": preview,
                    "selection_state": row["selection_state"] or "unselected",
                    "has_local_messages": bool(row["message_count"]),
                }
            )
        from .platform_seam import library_source_kind
        if library_source_kind() == 'decrypted_archive':
            from .mac_metadata import contact_kinds
            kinds = contact_kinds(self.index_path, self.account)
            for item in result:
                item['contact_kind'] = kinds.get(item['username'])
        return result

    def conversation(self, username: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(
                """
                SELECT username, chat_table, display_name, is_group, sort_timestamp, last_timestamp
                FROM conversations
                WHERE account=? AND username=?
                """,
                (self.account, username),
            ).fetchone()
            if not row:
                return None
            count = con.execute(
                "SELECT COUNT(*) FROM messages WHERE account=? AND chat_table=?",
                (self.account, row["chat_table"]),
            ).fetchone()[0]
        return {
            "username": row["username"],
            "chat_table": row["chat_table"],
            "name": row["display_name"],
            "is_group": bool(row["is_group"]),
            "message_count": int(count),
            "sort_timestamp": int(row["sort_timestamp"] or 0),
            "last_timestamp": int(row["last_timestamp"] or 0),
        }

    def list_messages(
        self,
        username: str,
        *,
        limit: int = 80,
        before: int | None = None,
        before_uid: str | None = None,
        around_uid: str | None = None,
        after_uid: str | None = None,
        at_time: int | None = None,
        category: str | None = None,
        query: str | None = None,
        from_day: int | None = None,
        to_day: int | None = None,
        sender: str | None = None,
        filter_offset: int = 0,
    ) -> dict[str, Any]:
        limit = self._bounded(limit, minimum=1, maximum=300)
        categories = {
            'solitaire': {'group_chat_reference'},
            'file': {'file'},
            'media': {'image', 'video'},
            'image': {'image'},
            'video': {'video'},
            'link': {'link', 'open_link', 'webview'},
        }
        selected_categories = category.split(',') if category else []
        if any(value not in categories for value in selected_categories):
            raise ValueError('invalid_message_category')
        selected_kinds = set().union(*(categories[value] for value in selected_categories))
        selected_senders = set(sender.split(',')) if sender else set()
        if from_day is not None and to_day is not None and from_day >= to_day:
            raise ValueError('invalid_message_dates')
        if filter_offset < 0:
            raise ValueError('invalid_filter_offset')
        member_names = self._mac_group_names(username)
        needle = (query or '').strip().casefold()
        filtered = bool(category or needle or from_day is not None
                        or to_day is not None or sender)
        with self._connect() as con:
            conversation = con.execute(
                """
                SELECT username, chat_table, display_name, is_group
                FROM conversations
                WHERE account=? AND username=?
                """,
                (self.account, username),
            ).fetchone()
            if not conversation:
                raise KeyError("conversation_not_found")
            self_username = self._self_username(con)

            where_before = ""
            params: list[Any] = [self.account, conversation["chat_table"]]
            if at_time is not None:
                if filtered or before_uid or before is not None or around_uid or after_uid or filter_offset:
                    raise ValueError('incompatible_message_cursor')
                target = con.execute(
                    'SELECT uid FROM messages WHERE account=? AND chat_table=? AND create_time>=? '
                    'ORDER BY create_time,sort_seq,server_id,uid LIMIT 1',
                    (self.account, conversation['chat_table'], int(at_time)),
                ).fetchone()
                if target:
                    around_uid = target['uid']
                else:
                    where_before = ' AND 0'
            if (around_uid or after_uid) and (filtered or before_uid or before is not None or (around_uid and after_uid)):
                raise ValueError('incompatible_message_cursor')
            if around_uid or after_uid:
                anchor = con.execute(
                    "SELECT COALESCE(create_time,0), COALESCE(sort_seq,0), "
                    "COALESCE(server_id,''), uid FROM messages "
                    "WHERE account=? AND chat_table=? AND uid=?",
                    (self.account, conversation['chat_table'], around_uid or after_uid),
                ).fetchone()
                if anchor is None:
                    raise ValueError('message_cursor_unavailable')
                identity = (self.account, conversation['chat_table'])
                base = ("SELECT uid FROM messages WHERE account=? AND chat_table=? AND "
                        "(COALESCE(create_time,0),COALESCE(sort_seq,0),COALESCE(server_id,''),uid) ")
                if around_uid:
                    earlier = con.execute(base + "<=(?,?,?,?) ORDER BY COALESCE(create_time,0) DESC,COALESCE(sort_seq,0) DESC,COALESCE(server_id,'') DESC,uid DESC LIMIT ?", (*identity, *anchor, (limit+1)//2)).fetchall()
                    later = con.execute(base + ">(?,?,?,?) ORDER BY COALESCE(create_time,0),COALESCE(sort_seq,0),COALESCE(server_id,''),uid LIMIT ?", (*identity, *anchor, limit//2)).fetchall()
                    ids = [row['uid'] for row in [*earlier, *later]]
                else:
                    ids = [row['uid'] for row in con.execute(base + ">(?,?,?,?) ORDER BY COALESCE(create_time,0),COALESCE(sort_seq,0),COALESCE(server_id,''),uid LIMIT ?", (*identity, *anchor, limit))]
                where_before = ' AND m.uid IN (' + ','.join('?' for _ in ids) + ')' if ids else ' AND 0'
                params.extend(ids)
            if before_uid:
                anchor = con.execute(
                    "SELECT COALESCE(create_time,0), COALESCE(sort_seq,0), "
                    "COALESCE(server_id,''), uid FROM messages "
                    "WHERE account=? AND chat_table=? AND uid=?",
                    (self.account, conversation['chat_table'], before_uid),
                ).fetchone()
                if anchor is None:
                    raise ValueError('message_cursor_unavailable')
                where_before = (" AND (COALESCE(m.create_time,0), COALESCE(m.sort_seq,0), "
                                "COALESCE(m.server_id,''), m.uid) < (?, ?, ?, ?)")
                params.extend(anchor)
            elif before is not None:
                where_before = " AND m.create_time < ?"
                params.append(int(before))
            if from_day is not None:
                where_before += ' AND m.create_time >= ?'
                params.append(int(from_day))
            if to_day is not None:
                where_before += ' AND m.create_time < ?'
                params.append(int(to_day))
            if not filtered:
                params.append(limit)
            if filtered:
                rows = []
                matched = 0
                for row in con.execute(
                    f"""SELECT m.uid, m.source_db, m.chat_table, m.local_id,
                               m.server_id, m.local_type, m.sort_seq, m.create_time,
                               m.status, m.message_content, m.compress_content,
                               m.packed_info_data, sm.username AS sender_username,
                               sc.display_name AS sender_display_name
                        FROM messages m
                        LEFT JOIN sender_map sm ON sm.account=m.account
                          AND sm.source_db=m.source_db
                          AND sm.sender_rowid=CAST(m.real_sender_id AS INTEGER)
                        LEFT JOIN contacts sc ON sc.account=m.account
                          AND sc.username=sm.username
                        WHERE m.account=? AND m.chat_table=? {where_before}
                        ORDER BY COALESCE(m.create_time,0) DESC, COALESCE(m.sort_seq,0) DESC,
                                 COALESCE(m.server_id,'') DESC, m.uid DESC""", params):
                    decoded = decode_message(
                        row['local_type'], row['message_content'],
                        row['compress_content'], row['packed_info_data'],
                        bool(conversation['is_group']),
                    )
                    if selected_kinds and decoded.kind not in selected_kinds:
                        continue
                    if selected_senders and (decoded.sender_override or row['sender_username']) not in selected_senders:
                        continue
                    if needle and needle not in ' '.join(
                        str(value or '') for value in
                        (decoded.text, decoded.title, member_names.get((username, decoded.sender_override or row['sender_username'])) or row['sender_display_name'],
                         row['sender_username'])
                    ).casefold():
                        continue
                    matched += 1
                    if matched <= filter_offset:
                        continue
                    rows.append(row)
                    if len(rows) > limit:
                        break
                has_more = len(rows) > limit
                rows = rows[:limit]
            else:
                rows = con.execute(
                    f"""SELECT m.uid, m.source_db, m.chat_table, m.local_id,
                               m.server_id, m.local_type, m.sort_seq, m.create_time,
                               m.status, m.message_content, m.compress_content,
                               m.packed_info_data, sm.username AS sender_username,
                               sc.display_name AS sender_display_name
                        FROM messages m
                        LEFT JOIN sender_map sm ON sm.account=m.account
                          AND sm.source_db=m.source_db
                          AND sm.sender_rowid=CAST(m.real_sender_id AS INTEGER)
                        LEFT JOIN contacts sc ON sc.account=m.account
                          AND sc.username=sm.username
                        WHERE m.account=? AND m.chat_table=? {where_before}
                        ORDER BY COALESCE(m.create_time,0) DESC, COALESCE(m.sort_seq,0) DESC,
                                 COALESCE(m.server_id,'') DESC, m.uid DESC LIMIT ?""", params,
                ).fetchall()

        solitaire_links = read_memberships(self.index_path, self.account)
        items: list[dict[str, Any]] = []
        is_group = bool(conversation["is_group"])
        for row in reversed(rows):
            decoded = decode_message(
                row["local_type"],
                row["message_content"],
                row["compress_content"],
                row["packed_info_data"],
                is_group,
            )
            sender_username = decoded.sender_override or row["sender_username"]
            if decoded.kind in {"system", "pat"} and not sender_username:
                sender_username = "system"
            sender_name = member_names.get((username, sender_username)) or row["sender_display_name"] or sender_username
            items.append(
                {
                    "uid": row["uid"],
                    "timestamp": int(row["create_time"] or 0),
                    "time": self._time_iso(row["create_time"]),
                    "sender": {
                        "username": sender_username,
                        "name": sender_name,
                        "from_me": bool(
                            sender_username and self_username and sender_username == self_username
                        ),
                    },
                    "type": {
                        "msg_type": decoded.msg_type,
                        "sub_type": decoded.sub_type,
                        "kind": decoded.kind,
                    },
                    "text": decoded.text,
                    "title": decoded.title,
                "solitaire_key": decoded.solitaire_key or (solitaire_links.get(row["uid"]) if decoded.kind == "group_chat_reference" else None),
                    "url": decoded.url,
                "payment": _payment_metadata(decoded, self_username),
                    "quote": {
                        "kind": decoded.quote_kind,
                        "text": decoded.quote_text,
                        "sender_name": decoded.quote_sender_name,
                        "media_md5": decoded.quote_media_md5,
                        "timestamp": decoded.quote_timestamp,
                        "server_id": decoded.quote_server_id,
                    }
                    if any(
                        (
                            decoded.quote_kind,
                            decoded.quote_text,
                            decoded.quote_sender_name,
                            decoded.quote_media_md5,
                            decoded.quote_timestamp,
                        )
                    )
                    else None,
                    "card": {
                        "thumbnail_url": decoded.thumbnail_url,
                        "cover_url": decoded.cover_url,
                        "avatar_url": decoded.avatar_url,
                        "author_username": decoded.author_username,
                        "source_display_name": decoded.source_display_name,
                    }
                    if any(
                        (
                            decoded.thumbnail_url,
                            decoded.cover_url,
                            decoded.avatar_url,
                            decoded.author_username,
                            decoded.source_display_name,
                        )
                    )
                    else None,
                    "media": {
                        "md5": decoded.media_md5,
                        "state": "indexed_only",
                    }
                    if decoded.kind in {"image", "video", "voice", "file"}
                    else None,
                    "status": row["status"],
                    "source": {
                        "database": row["source_db"],
                        "table": row["chat_table"],
                        "local_id": row["local_id"],
                        "server_id": row["server_id"],
                        "sort_seq": row["sort_seq"],
                    },
                }
            )

        self._resolve_quotes(items, conversation["chat_table"])
        next_before = items[0]["timestamp"] if not filtered and len(rows) == limit and items else None
        return {
            "conversation": {
                "username": conversation["username"],
                "name": conversation["display_name"],
                "is_group": is_group,
            },
            "messages": items,
            "next_before": next_before,
            "next_before_uid": items[0]["uid"] if next_before is not None else None,
            "next_offset": filter_offset + len(rows) if filtered and has_more else None,
        }

    def _resolve_quotes(self, items: list[dict], chat_table: str) -> None:
        """Match native IDs inside one account and conversation; never guess by text/time."""
        references = {str(item['quote']['server_id']) for item in items
                      if item.get('quote') and item['quote'].get('server_id') not in (None, '', '0')}
        matches: dict[str, list[str]] = {}
        if references:
            with self._connect() as con:
                for row in con.execute(
                    'SELECT uid,server_id FROM messages WHERE account=? AND chat_table=? AND server_id IN (' + ','.join('?' for _ in references) + ')',
                    (self.account, chat_table, *references),
                ):
                    matches.setdefault(str(row['server_id']), []).append(row['uid'])
        targets = {}
        for item in items:
            quote = item.get('quote')
            if not quote:
                continue
            candidates = matches.get(str(quote.get('server_id')), [])
            quote['state'] = 'unavailable'
            quote['target'] = None
            if len(candidates) != 1 or candidates[0] == item['uid']:
                continue
            uid = candidates[0]
            if uid not in targets:
                found = self.message_by_uid(uid)
                target = found['message'] if found else None
                # Preserve a nested quote snapshot without recursively resolving its target.
                targets[uid] = target
            if targets[uid]:
                quote['target'] = targets[uid]
                quote['state'] = 'available'

    def group_members(self, username: str, contact_db: Path) -> list[dict[str, str]]:
        with self._connect() as index:
            conversation = index.execute(
                'SELECT is_group FROM conversations WHERE account=? AND username=?',
                (self.account, username),
            ).fetchone()
        if conversation is None:
            raise KeyError('conversation_not_found')
        if not conversation['is_group']:
            raise ValueError('conversation_is_not_group')

        from .platform_seam import library_source_kind
        if library_source_kind() == 'decrypted_archive':
            from .mac_metadata import group_members
            members = group_members(self.index_path, self.account, username)
            if members is not None:
                return [{key: value for key, value in member.items() if key != 'room'}
                        for member in members]

        contact = sqlite3.connect(
            f'file:{contact_db.resolve().as_posix()}?mode=ro', uri=True,
        )
        try:
            rows = contact.execute(
                '''SELECT c.username, c.remark, c.nick_name
                   FROM chat_room r
                   JOIN chatroom_member cm ON cm.room_id=r.id
                   JOIN contact c ON c.id=cm.member_id
                   WHERE r.username=? AND c.username IS NOT NULL AND c.username<>''
                   ORDER BY c.username''',
                (username,),
            ).fetchall()
        finally:
            contact.close()
        return [
            {
                'username': str(member),
                'name': str(remark or nick_name or member).strip(),
            }
            for member, remark, nick_name in rows
        ]


    def message_by_uid(self, uid: str) -> dict[str, Any] | None:
        with self._connect() as con:
            row = con.execute(
                """
                SELECT
                    m.uid, m.source_db, m.chat_table, m.local_id, m.server_id,
                    m.local_type, m.sort_seq, m.create_time, m.status,
                    m.message_content, m.compress_content, m.packed_info_data,
                    c.username AS chat_username,
                    c.display_name AS chat_name,
                    c.is_group,
                    sm.username AS sender_username,
                    sc.display_name AS sender_display_name
                FROM messages m
                JOIN conversations c
                  ON c.account=m.account AND c.chat_table=m.chat_table
                LEFT JOIN sender_map sm
                  ON sm.account=m.account
                 AND sm.source_db=m.source_db
                 AND sm.sender_rowid=CAST(m.real_sender_id AS INTEGER)
                LEFT JOIN contacts sc
                  ON sc.account=m.account AND sc.username=sm.username
                WHERE m.account=? AND m.uid=?
                LIMIT 1
                """,
                (self.account, uid),
            ).fetchone()
            if not row:
                return None
            self_username = self._self_username(con)

        solitaire_links = read_memberships(self.index_path, self.account)
        is_group = bool(row["is_group"])
        decoded = decode_message(
            row["local_type"],
            row["message_content"],
            row["compress_content"],
            row["packed_info_data"],
            is_group,
        )
        sender_username = decoded.sender_override or row["sender_username"]
        if decoded.kind in {"system", "pat"} and not sender_username:
            sender_username = "system"
        member_names = self._mac_group_names(row["chat_username"]) if is_group else {}
        sender_name = member_names.get((row["chat_username"], sender_username)) or row["sender_display_name"] or sender_username

        return {
            "conversation": {
                "username": row["chat_username"],
                "name": row["chat_name"],
                "is_group": is_group,
            },
            "message": {
                "uid": row["uid"],
                "timestamp": int(row["create_time"] or 0),
                "time": self._time_iso(row["create_time"]),
                "sender": {
                    "username": sender_username,
                    "name": sender_name,
                    "from_me": bool(
                        sender_username and self_username and sender_username == self_username
                    ),
                },
                "type": {
                    "msg_type": decoded.msg_type,
                    "sub_type": decoded.sub_type,
                    "kind": decoded.kind,
                },
                "text": decoded.text,
                "title": decoded.title,
                "solitaire_key": decoded.solitaire_key or (solitaire_links.get(row["uid"]) if decoded.kind == "group_chat_reference" else None),
                "url": decoded.url,
                "payment": _payment_metadata(decoded, self_username),
                "quote": {
                    "kind": decoded.quote_kind,
                    "text": decoded.quote_text,
                    "sender_name": decoded.quote_sender_name,
                    "media_md5": decoded.quote_media_md5,
                    "timestamp": decoded.quote_timestamp,
                    "server_id": decoded.quote_server_id,
                }
                if any(
                    (
                        decoded.quote_kind,
                        decoded.quote_text,
                        decoded.quote_sender_name,
                        decoded.quote_media_md5,
                        decoded.quote_timestamp,
                    )
                )
                else None,
                "card": {
                    "thumbnail_url": decoded.thumbnail_url,
                    "cover_url": decoded.cover_url,
                    "avatar_url": decoded.avatar_url,
                    "author_username": decoded.author_username,
                    "source_display_name": decoded.source_display_name,
                }
                if any(
                    (
                        decoded.thumbnail_url,
                        decoded.cover_url,
                        decoded.avatar_url,
                        decoded.author_username,
                        decoded.source_display_name,
                    )
                )
                else None,
                "media": {
                    "md5": decoded.media_md5,
                    "state": "indexed_only",
                }
                if decoded.kind in {"image", "video", "voice", "file"}
                else None,
                "status": row["status"],
                "source": {
                    "database": row["source_db"],
                    "table": row["chat_table"],
                    "local_id": row["local_id"],
                    "server_id": row["server_id"],
                    "sort_seq": row["sort_seq"],
                },
            },
        }

    def list_contacts(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        query: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = self._bounded(limit, minimum=1, maximum=1000)
        offset = max(0, int(offset))
        needle = (query or "").strip()
        where = "WHERE ct.account=?"
        params: list[Any] = [self.account]
        if needle:
            where += " AND (ct.display_name LIKE ? OR ct.username LIKE ? OR ct.remark LIKE ? OR ct.nick_name LIKE ?)"
            pattern = f"%{needle}%"
            params.extend([pattern, pattern, pattern, pattern])
        params.extend([limit, offset])
        with self._connect() as con:
            rows = con.execute(
                f"""
                SELECT
                    ct.username,
                    ct.alias,
                    ct.remark,
                    ct.nick_name,
                    ct.display_name,
                    ct.is_group,
                    c.chat_table,
                    s.state AS selection_state
                FROM contacts ct
                LEFT JOIN conversations c
                  ON c.account=ct.account AND c.username=ct.username
                LEFT JOIN conversation_selection s
                  ON s.account=ct.account AND s.username=ct.username
                {where}
                ORDER BY ct.is_group ASC, ct.display_name COLLATE NOCASE
                LIMIT ? OFFSET ?
                """,
                params,
            ).fetchall()
        return [
            {
                "username": row["username"],
                "alias": row["alias"],
                "remark": row["remark"],
                "nick_name": row["nick_name"],
                "name": row["display_name"],
                "is_group": bool(row["is_group"]),
                "chat_table": row["chat_table"],
                "can_follow": bool(row["chat_table"]),
                "selection_state": row["selection_state"] or "unselected",
            }
            for row in rows
        ]

    def search_messages(
        self,
        query: str,
        *,
        limit: int = 50,
        scan_limit: int = 20000,
        allowed_conversations: list[str] | None = None,
    ) -> dict[str, Any]:
        needle = query.strip().casefold()
        if not needle:
            return {"query": query, "items": [], "scanned": 0, "truncated_scan": False}
        limit = self._bounded(limit, minimum=1, maximum=200)
        scan_limit = self._bounded(scan_limit, minimum=100, maximum=100000)

        with self._connect() as con:
            rows = con.execute(
                """
                SELECT
                    m.uid, m.local_type, m.create_time, m.message_content,
                    m.compress_content, m.packed_info_data,
                    c.username AS chat_username, c.display_name AS chat_name, c.is_group,
                    sm.username AS sender_username, sc.display_name AS sender_name
                FROM messages m
                JOIN conversations c
                  ON c.account=m.account AND c.chat_table=m.chat_table
                LEFT JOIN sender_map sm
                  ON sm.account=m.account
                 AND sm.source_db=m.source_db
                 AND sm.sender_rowid=CAST(m.real_sender_id AS INTEGER)
                LEFT JOIN contacts sc
                  ON sc.account=m.account AND sc.username=sm.username
                WHERE m.account=?
                  AND (? IS NULL OR c.username IN (SELECT value FROM json_each(?)))
                ORDER BY m.create_time DESC
                LIMIT ?
                """,
                (self.account, json.dumps(allowed_conversations) if allowed_conversations is not None else None,
                 json.dumps(allowed_conversations or []), scan_limit),
            ).fetchall()

        solitaire_links = read_memberships(self.index_path, self.account)
        hits: list[dict[str, Any]] = []
        member_names = self._mac_group_names()
        for row in rows:
            decoded = decode_message(
                row["local_type"],
                row["message_content"],
                row["compress_content"],
                row["packed_info_data"],
                bool(row["is_group"]),
            )
            sender_username = decoded.sender_override or row['sender_username']
            sender_name = member_names.get((row['chat_username'], sender_username)) or row['sender_name'] or sender_username
            haystack = " ".join(
                str(value or "")
                for value in (
                    decoded.text,
                    decoded.title,
                    row["chat_name"],
                    member_names.get((row['chat_username'], sender_username)) or row["sender_name"],
                    row["sender_username"],
                )
            ).casefold()
            if needle not in haystack:
                continue
            hits.append(
                {
                    "uid": row["uid"],
                    "timestamp": int(row["create_time"] or 0),
                    "time": self._time_iso(row["create_time"]),
                    "chat": {
                        "username": row["chat_username"],
                        "name": row["chat_name"],
                        "is_group": bool(row["is_group"]),
                    },
                    "sender": {
                        "username": sender_username,
                        "name": sender_name,
                    },
                    "kind": decoded.kind,
                    "text": decoded.text,
                    "title": decoded.title,
                "solitaire_key": decoded.solitaire_key or (solitaire_links.get(row["uid"]) if decoded.kind == "group_chat_reference" else None),
                }
            )
            if len(hits) >= limit:
                break

        return {
            "query": query,
            "items": hits,
            "scanned": len(rows),
            "truncated_scan": len(rows) >= scan_limit,
        }
