"""Append-only index change metadata and read-only resumable MCP queries."""
from __future__ import annotations
import base64
import json
import sqlite3


def ensure_change_journal(con: sqlite3.Connection) -> None:
    # All initialization is atomic. Existing clients keep using the old schema.
    con.executescript('''
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS mcp_stream (id INTEGER PRIMARY KEY CHECK(id=1), epoch TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS mcp_changes (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            uid TEXT NOT NULL, account TEXT NOT NULL, chat_table TEXT NOT NULL,
            kind TEXT NOT NULL, row_hash TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_mcp_changes_scope ON mcp_changes(account, chat_table, sequence);
        INSERT INTO mcp_changes(uid,account,chat_table,kind,row_hash)
            SELECT uid,account,chat_table,'upsert',row_hash FROM messages
            WHERE NOT EXISTS (SELECT 1 FROM mcp_stream) ORDER BY rowid;
        INSERT OR IGNORE INTO mcp_stream VALUES(1,lower(hex(randomblob(16))));
        CREATE TRIGGER IF NOT EXISTS mcp_message_insert AFTER INSERT ON messages BEGIN
            INSERT INTO mcp_changes(uid,account,chat_table,kind,row_hash)
                VALUES(NEW.uid,NEW.account,NEW.chat_table,'upsert',NEW.row_hash);
        END;
        CREATE TRIGGER IF NOT EXISTS mcp_message_update AFTER UPDATE ON messages
        WHEN OLD.row_hash IS NOT NEW.row_hash BEGIN
            INSERT INTO mcp_changes(uid,account,chat_table,kind,row_hash)
                VALUES(NEW.uid,NEW.account,NEW.chat_table,'upsert',NEW.row_hash);
        END;
        CREATE TRIGGER IF NOT EXISTS mcp_message_delete AFTER DELETE ON messages BEGIN
            INSERT INTO mcp_changes(uid,account,chat_table,kind,row_hash)
                VALUES(OLD.uid,OLD.account,OLD.chat_table,'delete',OLD.row_hash);
        END;
        COMMIT;
    ''')


def read_changes(repo, library_id: str, conversations: list[str], cursor: str | None,
                 start: str, limit: int, *, scope_mode: str = 'selected') -> dict:
    import hashlib
    scope = hashlib.sha256(json.dumps('all_synced' if scope_mode == 'all_synced' else sorted(set(conversations)), ensure_ascii=False).encode()).hexdigest()
    with repo._connect() as con:
        con.execute('BEGIN')
        try:
            epoch = con.execute('SELECT epoch FROM mcp_stream WHERE id=1').fetchone()[0]
        except (sqlite3.OperationalError, TypeError):
            raise ValueError('incremental_index_unavailable: 请先在新版桌面应用中刷新该资料库。') from None
        upper = con.execute('SELECT COALESCE(MAX(sequence),0) FROM mcp_changes').fetchone()[0]
        if cursor is None:
            after = upper if start == 'now' else 0
        else:
            try:
                if len(cursor) > 2048:
                    raise ValueError()
                value = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                after = value['after']
                if (value['v'] != 1 or value['library'] != library_id or value['epoch'] != epoch
                        or value['scope'] != scope or type(after) is not int or not 0 <= after <= upper):
                    raise ValueError()
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise ValueError('cursor_reset_required: 游标无效、资料库已重建或授权范围发生变化。') from None
        rows = con.execute('''SELECT e.sequence,e.uid,e.kind,e.row_hash,c.username
            FROM mcp_changes e JOIN conversations c ON c.account=e.account AND c.chat_table=e.chat_table
            WHERE e.account=? AND c.username IN (SELECT value FROM json_each(?))
              AND e.sequence>? AND e.sequence<=? ORDER BY e.sequence LIMIT ?''',
            (repo.account, json.dumps(conversations), after, upper, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        checkpoint = rows[-1]['sequence'] if more else upper
    events = []
    for row in rows:
        # Current message state is returned, not a historical snapshot. A later
        # update has its own event and is never hidden by advancing this cursor.
        message = repo.message_by_uid(row['uid']) if row['kind'] != 'delete' else None
        if message and message['conversation']['username'] not in conversations:
            message = None
        events.append({'sequence': row['sequence'], 'uid': row['uid'], 'kind': row['kind'],
                       'conversation_id': row['username'], 'event_row_hash': row['row_hash'],
                       'current': message})
    value = {'v': 1, 'library': library_id, 'epoch': epoch, 'scope': scope, 'after': checkpoint}
    next_cursor = base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).decode()
    return {'library_id': library_id, 'events': events, 'cursor': next_cursor, 'has_more': more,
            'coverage': '仅已索引消息；current 是查询时的当前内容，不是事件发生时的历史快照。',
            'checkpoint_rule': '处理成功后保存 cursor；失败重用原游标，按资料库、sequence 去重。'}
