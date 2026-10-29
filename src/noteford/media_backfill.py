from __future__ import annotations

import json
import sqlite3
import time as clock
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .decode import decode_message
from .image_crypto import (
    V2_MAGIC,
    derive_xor_key,
    find_recent_thumbnail_templates,
    load_image_keys,
    save_image_keys,
    scan_image_aes_key,
)
from .media import MediaResolver


def local_day_window(days: int = 14, *, today: date | None = None) -> tuple[int, int]:
    """Return an exact, half-open window of local calendar days."""
    if days < 1:
        raise ValueError('days must be positive')
    current = today or date.today()
    start = datetime.combine(current - timedelta(days=days - 1), time.min)
    end = datetime.combine(current + timedelta(days=1), time.min)
    return int(start.timestamp()), int(end.timestamp())


def recover_image_key(
    *,
    account_root: Path,
    key_path: Path,
    state_path: Path,
    force: bool = False,
) -> str:
    if load_image_keys(key_path) is not None:
        return 'available'
    today = date.today().isoformat()
    if not force and state_path.is_file():
        try:
            previous = json.loads(state_path.read_text(encoding='utf-8'))
            if previous.get('attempt_day') == today:
                return str(previous.get('status') or 'unavailable')
        except (OSError, ValueError, TypeError):
            pass

    status = 'no_image_templates'
    templates = find_recent_thumbnail_templates(account_root, keep=24)
    sample = None
    for path in templates:
        try:
            with path.open('rb') as handle:
                if handle.read(6) == V2_MAGIC:
                    sample = path
                    break
        except OSError:
            continue
    if sample is not None:
        xor_key = derive_xor_key(templates)
        if xor_key is None:
            status = 'xor_key_unavailable'
        else:
            from .winmem import UnsupportedPlatform

            try:
                aes_key = scan_image_aes_key(sample, timeout_seconds=15)
            except UnsupportedPlatform:
                status = 'unsupported_platform'
            else:
                if aes_key is None:
                    status = 'aes_key_unavailable'
                else:
                    save_image_keys(key_path, aes_key, xor_key)
                    status = 'available'
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = state_path.with_suffix('.json.tmp')
    temporary.write_text(
        json.dumps({'attempt_day': today, 'status': status}), encoding='utf-8'
    )
    temporary.replace(state_path)
    return status


def prefetch_recent_media(
    *,
    index_path: Path,
    account: str,
    resolver: MediaResolver,
    destination_root: Path,
    username: str | None = None,
    days: int = 14,
    today: date | None = None,
    ranges=None,
    max_attempts: int = 100,
    now: float | None = None,
) -> dict:
    if max_attempts < 1:
        raise ValueError('max_attempts must be positive')
    now = clock.time() if now is None else now
    since, until = ranges.envelope() if ranges else local_day_window(days, today=today)
    from .media_loading import allows_original
    resolver.original_allowed = lambda uid: allows_original(index_path, uid)
    writer = sqlite3.connect(index_path)
    try:
        writer.execute('''
            CREATE TABLE IF NOT EXISTS media_exports (
                uid TEXT PRIMARY KEY,
                row_hash TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                path TEXT,
                attempted_at TEXT NOT NULL
            )
        ''')
        columns = {row[1] for row in writer.execute('PRAGMA table_info(media_exports)')}
        for name, definition in [('failures', 'INTEGER NOT NULL DEFAULT 0'),
                                 ('retry_at', 'REAL NOT NULL DEFAULT 0')]:
            if name not in columns:
                writer.execute(f'ALTER TABLE media_exports ADD COLUMN {name} {definition}')
        writer.commit()
        reader = sqlite3.connect(f'file:{index_path.resolve().as_posix()}?mode=ro', uri=True)
        reader.row_factory = sqlite3.Row
        counts: Counter[str] = Counter()
        attempted = 0
        batch_limited = False
        try:
            query = '''
                SELECT m.uid, m.row_hash, m.chat_table, m.server_id, m.local_type,
                       m.create_time, m.message_content, m.compress_content,
                       m.packed_info_data, c.username, c.is_group,
                       e.row_hash AS exported_hash, e.status AS exported_status,
                       e.path AS exported_path, e.failures, e.retry_at
                FROM messages m
                JOIN conversations c ON c.account=m.account AND c.chat_table=m.chat_table
                JOIN conversation_selection s ON s.account=c.account AND s.username=c.username
                LEFT JOIN media_exports e ON e.uid=m.uid
                WHERE m.account=? AND s.state='following'
                  AND m.create_time>=? AND m.create_time<?
                  AND (m.local_type & 4294967295) IN (3,34,43,49)
            '''
            if ranges is not None:
                reader.create_function('media_in_range', 2, ranges.contains)
                query += ' AND media_in_range(c.username, m.create_time)'
            params: list[object] = [account, since, until]
            if username is not None:
                query += ' AND c.username=?'
                params.append(username)
            query += ' ORDER BY m.create_time, m.uid'
            for row in reader.execute(query, params):
                if (
                    row['exported_hash'] == row['row_hash']
                    and row['exported_status'] in {'ready', 'ready_audio'}
                    and row['exported_path']
                    and Path(row['exported_path']).is_file()
                ):
                    continue
                if (row['exported_hash'] == row['row_hash']
                        and row['exported_status'] not in {'ready', 'ready_audio'}
                        and (row['retry_at'] or 0) > now):
                    continue
                if attempted >= max_attempts:
                    batch_limited = True
                    break
                timestamp = int(row['create_time'])
                uid = str(row['uid'])
                kind = 'unknown'
                try:
                    decoded = decode_message(
                        row['local_type'], row['message_content'], row['compress_content'],
                        row['packed_info_data'], bool(row['is_group']),
                    )
                    kind = decoded.kind
                    if kind == 'image':
                        artifact = resolver.export_image(
                            str(row['chat_table']), timestamp, decoded.media_md5,
                            uid, destination_root,
                        )
                    elif kind == 'video':
                        artifact = resolver.export_video(
                            timestamp, decoded.media_md5, uid, destination_root,
                        )
                    elif kind == 'voice':
                        artifact = resolver.export_voice(
                            str(row['username']), row['server_id'], uid, destination_root,
                        )
                    elif kind == 'file':
                        artifact = resolver.export_file(
                            timestamp, decoded.title, uid, destination_root,
                        )
                    else:
                        counts['not_local_media'] += 1
                        continue
                    status = artifact.status
                    path = artifact.path
                except Exception as exc:
                    # One damaged media message must not block the remaining day window.
                    status = f'error:{exc.__class__.__name__}'
                    path = None
                counts[status] += 1
                attempted += 1
                ready = status in {'ready', 'ready_audio'} and bool(path)
                failures = (int(row['failures'] or 0) if row['exported_hash'] == row['row_hash'] else 0) + 1
                retry_at = 0 if ready else now + min(21600, 60 * 2 ** min(failures, 9))
                writer.execute(
                    '''INSERT INTO media_exports(uid,row_hash,kind,status,path,attempted_at,failures,retry_at)
                       VALUES(?,?,?,?,?,?,?,?)
                       ON CONFLICT(uid) DO UPDATE SET
                         row_hash=excluded.row_hash, kind=excluded.kind,
                         status=excluded.status, path=excluded.path,
                         attempted_at=excluded.attempted_at,
                         failures=excluded.failures, retry_at=excluded.retry_at''',
                    (uid, row['row_hash'], kind, status, path,
                     datetime.now(timezone.utc).isoformat(), 0 if ready else failures, retry_at),
                )
                if attempted % 100 == 0:
                    writer.commit()
            writer.commit()
        finally:
            reader.close()
        return {
            'since': since,
            'until_exclusive': until,
            'days': days,
            'attempted': attempted,
            'batch_limited': batch_limited,
            'status_counts': dict(counts),
        }
    finally:
        writer.close()
