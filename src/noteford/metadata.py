from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .libraries import account_username


def _connect(path: Path, readonly: bool = True) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f'file:{path.resolve().as_posix()}?mode=ro', uri=True)
    else:
        con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def _display_name(remark: str | None, nick_name: str | None, fallback: str) -> str:
    remark = (remark or '').strip()
    nick_name = (nick_name or '').strip()
    return remark or nick_name or fallback


def ensure_metadata_schema(index: sqlite3.Connection) -> None:
    index.executescript(
        '''
        CREATE TABLE IF NOT EXISTS conversations (
            account TEXT NOT NULL,
            chat_table TEXT NOT NULL,
            username TEXT NOT NULL,
            display_name TEXT NOT NULL,
            is_group INTEGER NOT NULL,
            session_type INTEGER,
            last_timestamp INTEGER,
            sort_timestamp INTEGER,
            PRIMARY KEY(account, chat_table)
        );
        CREATE INDEX IF NOT EXISTS idx_conversations_username
            ON conversations(account, username);

        CREATE TABLE IF NOT EXISTS contacts (
            account TEXT NOT NULL,
            username TEXT NOT NULL,
            alias TEXT,
            remark TEXT,
            nick_name TEXT,
            display_name TEXT NOT NULL,
            is_group INTEGER NOT NULL,
            PRIMARY KEY(account, username)
        );

        CREATE TABLE IF NOT EXISTS sender_map (
            account TEXT NOT NULL,
            source_db TEXT NOT NULL,
            sender_rowid INTEGER NOT NULL,
            username TEXT NOT NULL,
            PRIMARY KEY(account, source_db, sender_rowid)
        );
        CREATE INDEX IF NOT EXISTS idx_sender_map_username
            ON sender_map(account, username);

        CREATE TABLE IF NOT EXISTS account_profile (
            account TEXT PRIMARY KEY,
            self_username TEXT,
            self_display_name TEXT,
            inferred_direct_count INTEGER NOT NULL DEFAULT 0,
            inference_confidence REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversation_selection (
            account TEXT NOT NULL,
            username TEXT NOT NULL,
            chat_table TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('following','paused')),
            source_dbs TEXT NOT NULL DEFAULT '[]',
            selected_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_refresh_at TEXT,
            last_error TEXT,
            PRIMARY KEY(account, username)
        );
        CREATE INDEX IF NOT EXISTS idx_conversation_selection_state
            ON conversation_selection(account, state);
        '''
    )


def sync_metadata(
    index_path: Path,
    account: str,
    session_db: Path,
    contact_db: Path,
    message_dbs: list[Path],
    preserve_sender_map: bool = False,
) -> dict[str, int]:
    index = sqlite3.connect(index_path)
    index.row_factory = sqlite3.Row
    ensure_metadata_schema(index)
    session = _connect(session_db)
    contact = _connect(contact_db)

    try:
        contact_rows = contact.execute(
            '''
            SELECT username, alias, remark, nick_name
            FROM contact
            WHERE username IS NOT NULL AND username <> ''
            '''
        ).fetchall()
        contact_map: dict[str, dict[str, Any]] = {}
        for row in contact_rows:
            username = str(row['username'])
            contact_map[username] = {
                'alias': row['alias'] or '',
                'remark': row['remark'] or '',
                'nick_name': row['nick_name'] or '',
                'display_name': _display_name(row['remark'], row['nick_name'], username),
                'is_group': int(username.endswith('@chatroom')),
            }

        # Some sessions may not have a contact row. Keep their explicit local
        # session title when WeChat has one.
        no_contact_titles = {
            str(row['username']): str(row['session_title'] or '')
            for row in session.execute(
                "SELECT username, session_title FROM SessionNoContactInfoTable"
            )
        }

        sessions = session.execute(
            '''
            SELECT username, type, last_timestamp, sort_timestamp
            FROM SessionTable
            WHERE username IS NOT NULL AND username <> ''
            '''
        ).fetchall()

        with index:
            index.execute('DELETE FROM conversations WHERE account=?', (account,))
            index.execute('DELETE FROM contacts WHERE account=?', (account,))
            if not preserve_sender_map:
                index.execute('DELETE FROM sender_map WHERE account=?', (account,))

            index.executemany(
                '''
                INSERT INTO contacts(account, username, alias, remark, nick_name, display_name, is_group)
                VALUES(?,?,?,?,?,?,?)
                ''',
                [
                    (
                        account, username, item['alias'], item['remark'], item['nick_name'],
                        item['display_name'], item['is_group'],
                    )
                    for username, item in contact_map.items()
                ],
            )

            conversation_rows = []
            for row in sessions:
                username = str(row['username'])
                table_name = 'Msg_' + hashlib.md5(username.encode('utf-8')).hexdigest()
                info = contact_map.get(username)
                display = (info or {}).get('display_name') or no_contact_titles.get(username) or username
                conversation_rows.append(
                    (
                        account, table_name, username, display, int(username.endswith('@chatroom')),
                        row['type'], row['last_timestamp'], row['sort_timestamp'],
                    )
                )
            index.executemany(
                '''
                INSERT INTO conversations(
                    account, chat_table, username, display_name, is_group,
                    session_type, last_timestamp, sort_timestamp
                ) VALUES(?,?,?,?,?,?,?,?)
                ''',
                conversation_rows,
            )

            sender_count = 0
            for message_db in message_dbs:
                source_name = message_db.name
                con = _connect(message_db)
                try:
                    rows = con.execute('SELECT rowid, user_name FROM Name2Id').fetchall()
                finally:
                    con.close()
                payload = [
                    (account, source_name, int(row['rowid']), str(row['user_name']))
                    for row in rows
                    if row['user_name']
                ]
                index.executemany(
                    '''
                    INSERT INTO sender_map(account, source_db, sender_rowid, username)
                    VALUES(?,?,?,?)
                    ''',
                    payload,
                )
                sender_count += len(payload)

            # Infer the user's actual message-layer username without touching
            # WeChat UI. In a direct chat, one participant is the conversation
            # username (the other person); the recurring different sender is us.
            inferred = index.execute(
                '''
                SELECT sm.username, COUNT(*) AS n
                FROM messages m
                JOIN conversations c
                  ON c.account=m.account AND c.chat_table=m.chat_table
                JOIN sender_map sm
                  ON sm.account=m.account AND sm.source_db=m.source_db
                 AND sm.sender_rowid=CAST(m.real_sender_id AS INTEGER)
                WHERE m.account=? AND c.is_group=0
                  AND sm.username<>c.username
                GROUP BY sm.username
                ORDER BY n DESC
                ''',
                (account,),
            ).fetchall()
            total_inferred = sum(int(row['n']) for row in inferred)
            self_username = None
            self_count = 0
            confidence = 0.0
            if inferred:
                top = inferred[0]
                second = int(inferred[1]['n']) if len(inferred) > 1 else 0
                top_count = int(top['n'])
                # Refuse an ambiguous identity instead of inventing one.
                if top_count >= 10 and (second == 0 or top_count >= second * 5):
                    self_username = str(top['username'])
                    self_count = top_count
                    confidence = top_count / max(1, total_inferred)

            # Weixin 4.x account directories append a short instance suffix.
            # A matching contact row identifies the account before any messages
            # have been selected or indexed.
            directory_username = account_username(account)
            if self_username is None and directory_username in contact_map:
                self_username = directory_username
                confidence = 1.0

            self_display = (
                contact_map.get(self_username, {}).get('display_name')
                if self_username
                else None
            )
            now = datetime.now(timezone.utc).isoformat()
            index.execute(
                '''
                INSERT INTO account_profile(
                    account, self_username, self_display_name,
                    inferred_direct_count, inference_confidence, updated_at
                ) VALUES(?,?,?,?,?,?)
                ON CONFLICT(account) DO UPDATE SET
                    self_username=excluded.self_username,
                    self_display_name=excluded.self_display_name,
                    inferred_direct_count=excluded.inferred_direct_count,
                    inference_confidence=excluded.inference_confidence,
                    updated_at=excluded.updated_at
                ''',
                (account, self_username, self_display, self_count, confidence, now),
            )

        return {
            'contacts': len(contact_map),
            'conversations': len(sessions),
            'sender_mappings': sender_count,
            'self_inferred': int(self_username is not None),
            'self_inferred_direct_count': self_count,
        }
    finally:
        session.close()
        contact.close()
        index.close()
