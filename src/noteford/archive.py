from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@dataclass(frozen=True)
class ArchiveResult:
    source_db: str
    archive: str
    tables: int
    rows_seen: int
    rows_changed: int
    skipped: bool


def message_archives(project_root: Path, account: str) -> list[Path]:
    root = project_root / 'data' / 'archive' / account
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.iterdir()
        if path.is_file() and path.name.endswith('.db')
        and (path.name.startswith('message_') or path.name.startswith('biz_message_'))
    )


def merge_message_archive(
    source_db: Path,
    archive_db: Path,
) -> ArchiveResult:
    """Keep every observed raw message row, including rows later removed by Weixin.

    Source databases are decrypted, immutable snapshots. The merge scans each
    changed snapshot for late inserts and edits, but writes only changed rows.
    No message content is decoded or added to the searchable index here.
    """
    source_db = source_db.resolve(strict=True)
    archive_db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(archive_db, uri=True)
    try:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA synchronous=FULL')
        con.execute('CREATE TABLE IF NOT EXISTS archive_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        generation = str(source_db)
        previous = con.execute(
            "SELECT value FROM archive_meta WHERE key='source_generation'"
        ).fetchone()
        if previous and previous[0] == generation:
            tables = con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'"
            ).fetchone()[0]
            return ArchiveResult(source_db.name, str(archive_db), int(tables), 0, 0, True)

        con.execute('ATTACH DATABASE ? AS incoming', (f'file:{source_db.as_posix()}?mode=ro',))
        tables = [
            row[0] for row in con.execute(
                "SELECT name FROM incoming.sqlite_master WHERE type='table' AND name LIKE 'Msg_%' ORDER BY name"
            ) if str(row[0]).startswith('Msg_')
        ]
        rows_seen = 0
        rows_changed = 0
        with con:
            for table in tables:
                quoted = _quote(table)
                source_columns = [
                    row[1] for row in con.execute(f'PRAGMA incoming.table_info({quoted})')
                ]
                if 'local_id' not in source_columns:
                    raise ValueError(f'message table lacks local_id: {table}')
                exists = con.execute(
                    "SELECT 1 FROM main.sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not exists:
                    con.execute(
                        f'CREATE TABLE {quoted} AS SELECT * FROM incoming.{quoted} WHERE 0'
                    )
                    suffix = hashlib.sha256(table.encode()).hexdigest()[:16]
                    con.execute(
                        f'CREATE UNIQUE INDEX {_quote("archive_local_" + suffix)} '
                        f'ON {quoted}(local_id)'
                    )
                else:
                    archive_columns = [row[1] for row in con.execute(f'PRAGMA main.table_info({quoted})')]
                    if archive_columns != source_columns:
                        raise ValueError(f'message table schema changed: {table}')

                columns = ', '.join(_quote(column) for column in source_columns)
                updates = [column for column in source_columns if column != 'local_id']
                if updates:
                    assignment = ', '.join(
                        f'{_quote(column)}=excluded.{_quote(column)}' for column in updates
                    )
                    changed = ' OR '.join(
                        f'{quoted}.{_quote(column)} IS NOT excluded.{_quote(column)}'
                        for column in updates
                    )
                    conflict = f'DO UPDATE SET {assignment} WHERE {changed}'
                else:
                    conflict = 'DO NOTHING'
                rows_seen += int(con.execute(
                    f'SELECT COUNT(*) FROM incoming.{quoted}'
                ).fetchone()[0])
                before_changes = con.total_changes
                con.execute(
                    f'INSERT INTO main.{quoted} ({columns}) '
                    f'SELECT {columns} FROM incoming.{quoted} WHERE true '
                    f'ON CONFLICT(local_id) {conflict}'
                )
                rows_changed += con.total_changes - before_changes

            has_names = con.execute(
                "SELECT 1 FROM incoming.sqlite_master WHERE type='table' AND name='Name2Id'"
            ).fetchone()
            if has_names:
                con.execute(
                    'CREATE TABLE IF NOT EXISTS Name2Id '
                    '(source_rowid INTEGER PRIMARY KEY, user_name TEXT)'
                )
                con.execute(
                    'INSERT INTO Name2Id(source_rowid, user_name) '
                    'SELECT rowid, user_name FROM incoming.Name2Id WHERE true '
                    'ON CONFLICT(source_rowid) DO UPDATE SET user_name=excluded.user_name '
                    'WHERE Name2Id.user_name IS NOT excluded.user_name'
                )
            con.execute(
                "INSERT INTO archive_meta(key,value) VALUES('source_generation',?) "
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (generation,),
            )
        return ArchiveResult(source_db.name, str(archive_db), len(tables), rows_seen, rows_changed, False)
    finally:
        con.close()
