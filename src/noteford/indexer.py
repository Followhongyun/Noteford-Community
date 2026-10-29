from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass
class IndexStats:
    tables: int = 0
    scanned: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _scalar(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _hash_row(values: Iterable[Any]) -> str:
    digest = hashlib.sha256()
    for value in values:
        if value is None:
            raw = b'<null>'
        elif isinstance(value, bytes):
            raw = value
        else:
            raw = str(value).encode('utf-8', 'surrogatepass')
        digest.update(len(raw).to_bytes(8, 'big'))
        digest.update(raw)
    return digest.hexdigest()


def _message_uid(account: str, source_db: str, table: str, local_id: Any, server_id: Any) -> str:
    server = _scalar(server_id)
    if server and server != '0':
        material = f'{account}|server|{server}'
    else:
        material = f'{account}|local|{source_db}|{table}|{_scalar(local_id)}'
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def _connect_index(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=FULL')
    con.executescript(
        '''
        CREATE TABLE IF NOT EXISTS messages (
            uid TEXT PRIMARY KEY,
            account TEXT NOT NULL,
            source_db TEXT NOT NULL,
            chat_table TEXT NOT NULL,
            local_id INTEGER,
            server_id TEXT,
            local_type INTEGER,
            sort_seq INTEGER,
            real_sender_id TEXT,
            create_time INTEGER,
            status INTEGER,
            message_content BLOB,
            compress_content BLOB,
            packed_info_data BLOB,
            wcdb_ct INTEGER,
            source BLOB,
            row_hash TEXT NOT NULL,
            indexed_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_local
            ON messages(account, source_db, chat_table, local_id);
        CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(account, create_time);
        CREATE INDEX IF NOT EXISTS idx_messages_server ON messages(account, server_id);
        CREATE INDEX IF NOT EXISTS idx_messages_chat_time
            ON messages(account, chat_table, create_time DESC, sort_seq DESC);

        CREATE TABLE IF NOT EXISTS cursors (
            account TEXT NOT NULL,
            source_db TEXT NOT NULL,
            chat_table TEXT NOT NULL,
            last_local_id INTEGER NOT NULL DEFAULT 0,
            last_create_time INTEGER NOT NULL DEFAULT 0,
            last_indexed_at TEXT NOT NULL,
            PRIMARY KEY(account, source_db, chat_table)
        );

        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account TEXT NOT NULL,
            source_db TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            tables INTEGER NOT NULL,
            scanned INTEGER NOT NULL,
            inserted INTEGER NOT NULL,
            updated INTEGER NOT NULL,
            unchanged INTEGER NOT NULL
        );
        '''
    )
    # Lightweight forward-only schema migration for indexes created before
    # media metadata was persisted. Never rebuild the index just to add columns.
    existing = {row[1] for row in con.execute('PRAGMA table_info(messages)')}
    if 'packed_info_data' not in existing:
        con.execute('ALTER TABLE messages ADD COLUMN packed_info_data BLOB')
    if 'wcdb_ct' not in existing:
        con.execute('ALTER TABLE messages ADD COLUMN wcdb_ct INTEGER')
    from .nexus_outbox import ensure_outbox
    ensure_outbox(con)
    con.commit()
    from .mcp_changes import ensure_change_journal
    ensure_change_journal(con)
    from .media_loading import ensure_policy
    ensure_policy(con)
    return con




def ensure_index_schema(path: Path) -> None:
    con = _connect_index(path)
    con.close()

def _message_tables(source: sqlite3.Connection) -> list[str]:
    names = [
        row[0]
        for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%' ORDER BY name"
        )
    ]
    valid: list[str] = []
    for name in names:
        cols = {row[1] for row in source.execute(f'PRAGMA table_info({_quote(name)})')}
        if {'local_id', 'server_id', 'create_time'}.issubset(cols):
            valid.append(name)
    return valid


def _cursor(index: sqlite3.Connection, account: str, source_db: str, table: str) -> tuple[int, int]:
    row = index.execute(
        'SELECT last_local_id, last_create_time FROM cursors WHERE account=? AND source_db=? AND chat_table=?',
        (account, source_db, table),
    ).fetchone()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def index_messages(
    source_db_path: Path,
    index_path: Path,
    account: str,
    overlap_local_ids: int = 200,
    force_full: bool = False,
    table_names: Iterable[str] | None = None,
) -> IndexStats:
    source_db_path = source_db_path.resolve()
    started = datetime.now(timezone.utc).isoformat()
    source = sqlite3.connect(f'file:{source_db_path.as_posix()}?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    index = _connect_index(index_path)
    stats = IndexStats()
    from .nexus_outbox import owner_scope, enqueue_message, notify_committed
    try:
        delivery_scope = owner_scope(index_path)
    except BaseException:
        source.close()
        index.close()
        raise
    source_name = source_db_path.name

    column_specs = [
        ('local_id', 'local_id'),
        ('server_id', 'server_id'),
        ('local_type', 'local_type'),
        ('sort_seq', 'sort_seq'),
        ('real_sender_id', 'real_sender_id'),
        ('create_time', 'create_time'),
        ('status', 'status'),
        ('message_content', 'message_content'),
        ('compress_content', 'compress_content'),
        ('packed_info_data', 'packed_info_data'),
        ('wcdb_ct', 'WCDB_CT_message_content'),
        ('source', 'source'),
    ]
    logical_columns = [item[0] for item in column_specs]

    first_run_for_account = index.execute(
        'SELECT 1 FROM messages WHERE account=? LIMIT 1', (account,)
    ).fetchone() is None
    available_tables = _message_tables(source)
    if table_names is None:
        selected_tables = available_tables
    else:
        requested = {str(name) for name in table_names}
        selected_tables = [name for name in available_tables if name in requested]

    try:
        with index:
            for table in selected_tables:
                stats.tables += 1
                last_local_id, _last_time = _cursor(index, account, source_name, table)
                floor = 0 if force_full else (
                    max(0, last_local_id - max(0, overlap_local_ids)) if last_local_id else 0
                )
                source_columns = {
                    row[1] for row in source.execute(f'PRAGMA table_info({_quote(table)})')
                }
                select_columns = []
                for logical, physical in column_specs:
                    if physical in source_columns:
                        select_columns.append(
                            f'{_quote(physical)} AS {_quote(logical)}'
                        )
                    else:
                        select_columns.append(f'NULL AS {_quote(logical)}')
                sql = (
                    'SELECT ' + ','.join(select_columns)
                    + f' FROM {_quote(table)}'
                    + (' WHERE local_id >= ?' if floor else '')
                    + ' ORDER BY local_id'
                )
                cur = source.execute(sql, (floor,)) if floor else source.execute(sql)
                max_local = last_local_id
                max_time = 0

                for row in cur:
                    stats.scanned += 1
                    values = [row[col] for col in logical_columns]
                    row_hash = _hash_row(values)
                    uid = _message_uid(account, source_name, table, row['local_id'], row['server_id'])
                    existing = None if first_run_for_account else index.execute(
                        'SELECT row_hash FROM messages WHERE uid=?', (uid,)
                    ).fetchone()
                    if existing is None and not first_run_for_account:
                        # WeChat can replace a message's server ID/type in place
                        # (and assign a server ID after a local send). Keep the
                        # indexed UID so media, policies and MCP references stay
                        # attached. A reused local ID with another creation time
                        # is ambiguous: leave the unique constraint fail-closed.
                        local = index.execute(
                            '''SELECT uid, row_hash, create_time FROM messages
                               WHERE account=? AND source_db=? AND chat_table=? AND local_id=?''',
                            (account, source_name, table, row['local_id']),
                        ).fetchone()
                        if local is not None and local[2] == row['create_time']:
                            uid = local[0]
                            existing = (local[1],)
                    if existing is None:
                        state = 'inserted'
                    elif existing[0] != row_hash:
                        state = 'updated'
                    else:
                        state = 'unchanged'

                    if state != 'unchanged':
                        now = datetime.now(timezone.utc).isoformat()
                        index.execute(
                            '''
                            INSERT INTO messages(
                                uid, account, source_db, chat_table, local_id, server_id,
                                local_type, sort_seq, real_sender_id, create_time, status,
                                message_content, compress_content, packed_info_data, wcdb_ct,
                                source, row_hash, indexed_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(uid) DO UPDATE SET
                                source_db=excluded.source_db,
                                chat_table=excluded.chat_table,
                                local_id=excluded.local_id,
                                server_id=excluded.server_id,
                                local_type=excluded.local_type,
                                sort_seq=excluded.sort_seq,
                                real_sender_id=excluded.real_sender_id,
                                create_time=excluded.create_time,
                                status=excluded.status,
                                message_content=excluded.message_content,
                                compress_content=excluded.compress_content,
                                packed_info_data=excluded.packed_info_data,
                                wcdb_ct=excluded.wcdb_ct,
                                source=excluded.source,
                                row_hash=excluded.row_hash,
                                indexed_at=excluded.indexed_at
                            ''',
                            (
                                uid, account, source_name, table, row['local_id'],
                                _scalar(row['server_id']), row['local_type'], row['sort_seq'],
                                _scalar(row['real_sender_id']), row['create_time'], row['status'],
                                row['message_content'], row['compress_content'],
                                row['packed_info_data'], row['wcdb_ct'], row['source'],
                                row_hash, now,
                            ),
                        )
                        enqueue_message(index, delivery_scope, account, table,
                            {**dict(row), 'uid': uid})
                        if state == 'inserted':
                            stats.inserted += 1
                        else:
                            stats.updated += 1
                    else:
                        stats.unchanged += 1

                    try:
                        max_local = max(max_local, int(row['local_id'] or 0))
                    except (TypeError, ValueError):
                        pass
                    try:
                        max_time = max(max_time, int(row['create_time'] or 0))
                    except (TypeError, ValueError):
                        pass

                now = datetime.now(timezone.utc).isoformat()
                index.execute(
                    '''
                    INSERT INTO cursors(account, source_db, chat_table, last_local_id, last_create_time, last_indexed_at)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(account, source_db, chat_table) DO UPDATE SET
                        last_local_id=excluded.last_local_id,
                        last_create_time=MAX(cursors.last_create_time, excluded.last_create_time),
                        last_indexed_at=excluded.last_indexed_at
                    ''',
                    (account, source_name, table, max_local, max_time, now),
                )

            if delivery_scope:
                from .product_workspace import require_active
                require_active(delivery_scope[0], delivery_scope[2])
            finished = datetime.now(timezone.utc).isoformat()
            index.execute(
                '''
                INSERT INTO runs(account, source_db, started_at, finished_at, tables, scanned, inserted, updated, unchanged)
                VALUES(?,?,?,?,?,?,?,?,?)
                ''',
                (account, source_name, started, finished, stats.tables, stats.scanned,
                 stats.inserted, stats.updated, stats.unchanged),
            )
    finally:
        source.close()
        index.close()
    if delivery_scope:
        notify_committed(delivery_scope)
    return stats


def index_status(index_path: Path, account: str | None = None) -> dict[str, Any]:
    if not index_path.is_file():
        return {'exists': False, 'messages': 0, 'tables': 0, 'latest_create_time': 0}
    con = sqlite3.connect(index_path)
    try:
        where = ' WHERE account=?' if account else ''
        params = (account,) if account else ()
        messages = con.execute('SELECT COUNT(*) FROM messages' + where, params).fetchone()[0]
        latest = con.execute('SELECT COALESCE(MAX(create_time),0) FROM messages' + where, params).fetchone()[0]
        tables = con.execute('SELECT COUNT(*) FROM cursors' + where, params).fetchone()[0]
        return {'exists': True, 'messages': messages, 'tables': tables, 'latest_create_time': latest}
    finally:
        con.close()
