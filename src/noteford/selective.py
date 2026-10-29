from __future__ import annotations

import json
import sys
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .archive import merge_message_archive, message_archives
from .crypto import decrypt_database, find_matching_key, merge_encrypted_wal, quick_check
from .indexer import IndexStats, index_messages
from .metadata import ensure_metadata_schema, sync_metadata
from .refresh import (
    discover_auxiliary_databases,
    discover_media_databases,
    discover_message_databases,
    refresh_database,
    snapshot_raw_database,
)
from .snapshot import copy_database_snapshot


@dataclass
class SelectiveRefreshResult:
    username: str
    chat_table: str
    source_dbs: list[str]
    tables_found: int
    scanned: int
    inserted: int
    updated: int
    unchanged: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect_index(index_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(index_path)
    con.row_factory = sqlite3.Row
    ensure_metadata_schema(con)
    return con


def _read_candidates(path: Path) -> list[dict]:
    if sys.platform == 'darwin':
        from .mac_key_storage import cached_candidates
        return cached_candidates(path)
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding='utf-8'))
    return list(payload.get('candidates') or [])


def _match_or_scan(
    snapshot_database: Path,
    candidates_path: Path,
    *,
    allow_memory_rescan: bool,
) -> tuple[bytes, str]:
    match = find_matching_key(snapshot_database, _read_candidates(candidates_path))
    if match:
        return match
    if not allow_memory_rescan:
        raise RuntimeError(f'no cached key matches {snapshot_database.name}')

    from .platform_seam import PlatformCapabilityError, acquire_database_key_candidates

    try:
        candidates = acquire_database_key_candidates()
    except PlatformCapabilityError as exc:
        raise RuntimeError(str(exc)) from exc
    if not candidates:
        raise RuntimeError('read-only Weixin.exe key scan returned no candidates')
    if isinstance(candidates[0], dict):
        records = list(candidates)
    else:
        from .winmem import save_candidates

        save_candidates(candidates_path, candidates)
        records = _read_candidates(candidates_path)
    match = find_matching_key(snapshot_database, records)
    if not match:
        raise RuntimeError(f'fresh key scan still did not match {snapshot_database.name}')
    return match


def _conversation(index_path: Path, account: str, username: str) -> sqlite3.Row:
    con = _connect_index(index_path)
    try:
        row = con.execute(
            '''
            SELECT username, chat_table, display_name, is_group
            FROM conversations
            WHERE account=? AND username=?
            ''',
            (account, username),
        ).fetchone()
        if not row:
            raise KeyError('conversation_not_found')
        return row
    finally:
        con.close()


def _ordered_message_databases(
    wechat_root: Path,
    account: str,
    username: str,
    project_root: Path | None = None,
) -> list[Path]:
    archived = message_archives(project_root, account) if project_root else []
    rows = archived if archived else discover_message_databases(wechat_root, account)
    prefer_biz = username.startswith('gh_') or 'brand' in username.lower()
    return sorted(
        rows,
        key=lambda path: (
            not (path.name.startswith('biz_message_') if prefer_biz else path.name.startswith('message_')),
            path.name,
        ),
    )


def _sync_sender_map(index_path: Path, account: str, source_db: Path) -> int:
    source = sqlite3.connect(f'file:{source_db.resolve().as_posix()}?mode=ro', uri=True)
    source.row_factory = sqlite3.Row
    index = _connect_index(index_path)
    source_name = source_db.name
    try:
        exists = source.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='Name2Id'"
        ).fetchone()
        if not exists:
            return 0
        rows = source.execute('SELECT rowid AS sender_rowid, user_name FROM Name2Id').fetchall()
        payload = [
            (account, source_name, int(row['sender_rowid']), str(row['user_name']))
            for row in rows
            if row['user_name']
        ]
        with index:
            index.execute(
                'DELETE FROM sender_map WHERE account=? AND source_db=?',
                (account, source_name),
            )
            index.executemany(
                '''
                INSERT INTO sender_map(account, source_db, sender_rowid, username)
                VALUES(?,?,?,?)
                ''',
                payload,
            )
        return len(payload)
    finally:
        source.close()
        index.close()


def _refresh_self_profile(index_path: Path, account: str) -> None:
    con = _connect_index(index_path)
    try:
        inferred = con.execute(
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
        if not inferred:
            return
        total = sum(int(row['n']) for row in inferred)
        top = inferred[0]
        second = int(inferred[1]['n']) if len(inferred) > 1 else 0
        top_count = int(top['n'])
        if top_count < 10 or (second and top_count < second * 5):
            return

        self_username = str(top['username'])
        display = con.execute(
            'SELECT display_name FROM contacts WHERE account=? AND username=?',
            (account, self_username),
        ).fetchone()
        self_display = str(display[0]) if display and display[0] else None
        confidence = top_count / max(1, total)
        with con:
            con.execute(
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
                (
                    account,
                    self_username,
                    self_display,
                    top_count,
                    confidence,
                    _utcnow(),
                ),
            )
    finally:
        con.close()


def selection_status(index_path: Path, account: str, username: str) -> dict[str, Any]:
    con = _connect_index(index_path)
    try:
        row = con.execute(
            '''
            SELECT state, source_dbs, selected_at, updated_at, last_refresh_at, last_error
            FROM conversation_selection
            WHERE account=? AND username=?
            ''',
            (account, username),
        ).fetchone()
        count = con.execute(
            '''
            SELECT COUNT(*)
            FROM messages m
            JOIN conversations c
              ON c.account=m.account AND c.chat_table=m.chat_table
            WHERE m.account=? AND c.username=?
            ''',
            (account, username),
        ).fetchone()[0]
        if not row:
            return {
                'state': 'unselected',
                'has_local_messages': bool(count),
                'local_message_count': int(count),
                'source_dbs': [],
                'selected_at': None,
                'updated_at': None,
                'last_refresh_at': None,
                'last_error': None,
            }
        try:
            source_dbs = list(json.loads(row['source_dbs'] or '[]'))
        except (ValueError, TypeError):
            source_dbs = []
        return {
            'state': str(row['state']),
            'has_local_messages': bool(count),
            'local_message_count': int(count),
            'source_dbs': source_dbs,
            'selected_at': row['selected_at'],
            'updated_at': row['updated_at'],
            'last_refresh_at': row['last_refresh_at'],
            'last_error': row['last_error'],
        }
    finally:
        con.close()


def list_selections(index_path: Path, account: str) -> list[dict[str, Any]]:
    con = _connect_index(index_path)
    try:
        rows = con.execute(
            '''
            SELECT s.username, c.display_name, c.is_group, s.chat_table, s.state,
                   s.source_dbs, s.selected_at, s.updated_at,
                   s.last_refresh_at, s.last_error
            FROM conversation_selection s
            LEFT JOIN conversations c
              ON c.account=s.account AND c.username=s.username
            WHERE s.account=?
            ORDER BY s.updated_at DESC
            ''',
            (account,),
        ).fetchall()
        out = []
        for row in rows:
            try:
                source_dbs = list(json.loads(row['source_dbs'] or '[]'))
            except (ValueError, TypeError):
                source_dbs = []
            out.append(
                {
                    'username': row['username'],
                    'name': row['display_name'] or row['username'],
                    'is_group': bool(row['is_group']),
                    'chat_table': row['chat_table'],
                    'state': row['state'],
                    'source_dbs': source_dbs,
                    'selected_at': row['selected_at'],
                    'updated_at': row['updated_at'],
                    'last_refresh_at': row['last_refresh_at'],
                    'last_error': row['last_error'],
                }
            )
        return out
    finally:
        con.close()


def _record_selection(
    index_path: Path,
    account: str,
    username: str,
    chat_table: str,
    *,
    state: str,
    source_dbs: list[str] | None = None,
    last_error: str | None = None,
    refreshed: bool = False,
) -> None:
    if state not in {'following', 'paused'}:
        raise ValueError('invalid selection state')
    con = _connect_index(index_path)
    now = _utcnow()
    try:
        existing = con.execute(
            'SELECT selected_at, source_dbs FROM conversation_selection WHERE account=? AND username=?',
            (account, username),
        ).fetchone()
        selected_at = existing['selected_at'] if existing else now
        if source_dbs is None and existing:
            source_dbs_json = existing['source_dbs']
        else:
            source_dbs_json = json.dumps(source_dbs or [], ensure_ascii=False)
        with con:
            con.execute(
                '''
                INSERT INTO conversation_selection(
                    account, username, chat_table, state, source_dbs,
                    selected_at, updated_at, last_refresh_at, last_error
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(account, username) DO UPDATE SET
                    chat_table=excluded.chat_table,
                    state=excluded.state,
                    source_dbs=excluded.source_dbs,
                    updated_at=excluded.updated_at,
                    last_refresh_at=excluded.last_refresh_at,
                    last_error=excluded.last_error
                ''',
                (
                    account,
                    username,
                    chat_table,
                    state,
                    source_dbs_json,
                    selected_at,
                    now,
                    now if refreshed else None,
                    last_error,
                ),
            )
    finally:
        con.close()


def pause_conversation(index_path: Path, account: str, username: str) -> dict[str, Any]:
    row = _conversation(index_path, account, username)
    _record_selection(
        index_path,
        account,
        username,
        str(row['chat_table']),
        state='paused',
    )
    return selection_status(index_path, account, username)


def selective_refresh_conversation(
    *,
    wechat_root: Path,
    account: str,
    project_root: Path,
    candidates_path: Path,
    index_path: Path,
    username: str,
    overlap_local_ids: int = 200,
    allow_memory_rescan: bool = True,
) -> SelectiveRefreshResult:
    conversation = _conversation(index_path, account, username)
    chat_table = str(conversation['chat_table'])
    source_dbs: list[str] = []
    total = IndexStats()

    work_parent = project_root / 'data' / 'runtime' / 'selective'
    work_parent.mkdir(parents=True, exist_ok=True)

    found_family: str | None = None

    for live_database in _ordered_message_databases(wechat_root, account, username, project_root):
        current_family = 'biz' if live_database.name.startswith('biz_message_') else 'regular'
        if found_family is not None and current_family != found_family:
            break

        if live_database.parent == project_root / 'data' / 'archive' / account:
            source_db = live_database
            con = sqlite3.connect(f'file:{source_db.resolve().as_posix()}?mode=ro', uri=True)
            try:
                exists = con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (chat_table,),
                ).fetchone()
            finally:
                con.close()
            if not exists:
                continue
            stats = index_messages(
                source_db_path=source_db,
                index_path=index_path,
                account=account,
                overlap_local_ids=overlap_local_ids,
                force_full=True,
                table_names=[chat_table],
            )
            _sync_sender_map(index_path, account, source_db)
            if stats.tables:
                if found_family is None:
                    found_family = current_family
                source_dbs.append(live_database.name)
                total.tables += stats.tables
                total.scanned += stats.scanned
                total.inserted += stats.inserted
                total.updated += stats.updated
                total.unchanged += stats.unchanged
            continue

        with tempfile.TemporaryDirectory(
            prefix=live_database.stem + '-',
            dir=work_parent,
        ) as temp_name:
            temp_root = Path(temp_name)
            snapshot_root = temp_root / 'encrypted'
            snapshot_root.mkdir(parents=True, exist_ok=True)
            snapshot_dir = copy_database_snapshot(
                live_database,
                snapshot_root,
                retries=8,
                settle_seconds=0.20,
            )
            snapshot_db = snapshot_dir / live_database.name
            key, _fingerprint = _match_or_scan(
                snapshot_db,
                candidates_path,
                allow_memory_rescan=allow_memory_rescan,
            )
            decrypted_db = temp_root / live_database.name
            decrypt_database(snapshot_db, decrypted_db, key)
            encrypted_wal = Path(str(snapshot_db) + '-wal')
            if encrypted_wal.is_file():
                merge_encrypted_wal(decrypted_db, encrypted_wal, key)
            if not quick_check(decrypted_db):
                raise RuntimeError(f'temporary decrypted database failed quick_check: {live_database.name}')

            con = sqlite3.connect(f'file:{decrypted_db.as_posix()}?mode=ro', uri=True)
            try:
                exists = con.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (chat_table,),
                ).fetchone()
            finally:
                con.close()
            if not exists:
                continue

            stats = index_messages(
                source_db_path=decrypted_db,
                index_path=index_path,
                account=account,
                overlap_local_ids=overlap_local_ids,
                table_names=[chat_table],
            )
            _sync_sender_map(index_path, account, decrypted_db)
            if stats.tables:
                if found_family is None:
                    found_family = current_family
                source_dbs.append(live_database.name)
                total.tables += stats.tables
                total.scanned += stats.scanned
                total.inserted += stats.inserted
                total.updated += stats.updated
                total.unchanged += stats.unchanged

    if not source_dbs:
        raise RuntimeError('conversation table was not found in current message databases')

    _refresh_self_profile(index_path, account)
    return SelectiveRefreshResult(
        username=username,
        chat_table=chat_table,
        source_dbs=source_dbs,
        tables_found=total.tables,
        scanned=total.scanned,
        inserted=total.inserted,
        updated=total.updated,
        unchanged=total.unchanged,
    )


def follow_conversation(
    *,
    wechat_root: Path,
    account: str,
    project_root: Path,
    candidates_path: Path,
    index_path: Path,
    username: str,
    overlap_local_ids: int = 200,
    allow_memory_rescan: bool = True,
) -> dict[str, Any]:
    conversation = _conversation(index_path, account, username)
    result = selective_refresh_conversation(
        wechat_root=wechat_root,
        account=account,
        project_root=project_root,
        candidates_path=candidates_path,
        index_path=index_path,
        username=username,
        overlap_local_ids=overlap_local_ids,
        allow_memory_rescan=allow_memory_rescan,
    )
    _record_selection(
        index_path,
        account,
        username,
        str(conversation['chat_table']),
        state='following',
        source_dbs=result.source_dbs,
        refreshed=True,
    )
    return {
        'selection': selection_status(index_path, account, username),
        'refresh': result.to_dict(),
    }


def refresh_following(
    *,
    wechat_root: Path,
    account: str,
    project_root: Path,
    candidates_path: Path,
    index_path: Path,
    overlap_local_ids: int = 200,
    allow_memory_rescan: bool = True,
) -> list[dict[str, Any]]:
    rows = [
        row for row in list_selections(index_path, account)
        if row['state'] == 'following'
    ]
    results: list[dict[str, Any]] = []
    for row in rows:
        username = str(row['username'])
        try:
            refreshed = selective_refresh_conversation(
                wechat_root=wechat_root,
                account=account,
                project_root=project_root,
                candidates_path=candidates_path,
                index_path=index_path,
                username=username,
                overlap_local_ids=overlap_local_ids,
                allow_memory_rescan=allow_memory_rescan,
            )
            _record_selection(
                index_path,
                account,
                username,
                refreshed.chat_table,
                state='following',
                source_dbs=refreshed.source_dbs,
                refreshed=True,
            )
            results.append({'ok': True, **refreshed.to_dict()})
        except Exception as exc:
            _record_selection(
                index_path,
                account,
                username,
                str(row['chat_table']),
                state='following',
                last_error=exc.__class__.__name__,
            )
            results.append(
                {
                    'ok': False,
                    'username': username,
                    'error': exc.__class__.__name__,
                }
            )
    return results


def refresh_catalog(
    *,
    wechat_root: Path,
    account: str,
    project_root: Path,
    candidates_path: Path,
    index_path: Path,
    overlap_local_ids: int = 200,
    allow_memory_rescan: bool = True,
    refresh_selected: bool = True,
) -> dict[str, Any]:
    storage = wechat_root / account / 'db_storage'
    session_live = storage / 'session' / 'session.db'
    contact_live = storage / 'contact' / 'contact.db'
    head_image_live = storage / 'head_image' / 'head_image.db'

    session_result = refresh_database(
        live_database=session_live,
        account=account,
        project_root=project_root,
        candidates_path=candidates_path,
        index_path=index_path,
        overlap_local_ids=overlap_local_ids,
        allow_memory_rescan=allow_memory_rescan,
    )
    contact_result = refresh_database(
        live_database=contact_live,
        account=account,
        project_root=project_root,
        candidates_path=candidates_path,
        index_path=index_path,
        overlap_local_ids=overlap_local_ids,
        allow_memory_rescan=allow_memory_rescan,
    )
    head_result = None
    if head_image_live.is_file():
        head_result = refresh_database(
            live_database=head_image_live,
            account=account,
            project_root=project_root,
            candidates_path=candidates_path,
            index_path=index_path,
            overlap_local_ids=overlap_local_ids,
            allow_memory_rescan=allow_memory_rescan,
        )

    metadata = sync_metadata(
        index_path=index_path,
        account=account,
        session_db=Path(session_result.decrypted),
        contact_db=Path(contact_result.decrypted),
        message_dbs=[],
        preserve_sender_map=True,
    )

    raw_archives = []
    for database in discover_message_databases(wechat_root, account):
        result = refresh_database(
            live_database=database,
            account=account,
            project_root=project_root,
            candidates_path=candidates_path,
            index_path=index_path,
            overlap_local_ids=overlap_local_ids,
            allow_memory_rescan=allow_memory_rescan,
            index_all_messages=False,
        )
        archive_path = project_root / 'data' / 'archive' / account / database.name
        merged = merge_message_archive(Path(result.decrypted), archive_path)
        raw_archives.append({'refresh': result.__dict__, 'archive': merged.__dict__})

    media_results = []
    for database in discover_media_databases(wechat_root, account):
        result = refresh_database(
            live_database=database,
            account=account,
            project_root=project_root,
            candidates_path=candidates_path,
            index_path=index_path,
            overlap_local_ids=overlap_local_ids,
            allow_memory_rescan=allow_memory_rescan,
            index_all_messages=False,
        )
        media_results.append(result.__dict__)

    auxiliary_results = [
        snapshot_raw_database(database, account, project_root).__dict__
        for database in discover_auxiliary_databases(wechat_root, account)
    ]

    selected = []
    if refresh_selected:
        selected = refresh_following(
            wechat_root=wechat_root,
            account=account,
            project_root=project_root,
            candidates_path=candidates_path,
            index_path=index_path,
            overlap_local_ids=overlap_local_ids,
            allow_memory_rescan=allow_memory_rescan,
        )

    from .solitaire import refresh_native_memberships
    solitaire_status = refresh_native_memberships(
        wechat_root=wechat_root, account=account, project_root=project_root,
        candidates_path=candidates_path, index_path=index_path,
        allow_memory_rescan=allow_memory_rescan,
    )

    status_path = project_root / 'data' / 'state' / 'hourly_refresh.json'
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status = {
        'last_success_at': _utcnow(),
        'message_databases': len(raw_archives),
        'media_databases': len(media_results),
        'auxiliary_databases': len(auxiliary_results),
        'solitaire': solitaire_status,
    }
    temporary = status_path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(status, ensure_ascii=False), encoding='utf-8')
    temporary.replace(status_path)

    return {
        'catalog': {
            'session': session_result.__dict__,
            'contact': contact_result.__dict__,
            'head_image': head_result.__dict__ if head_result else None,
            'metadata': metadata,
        },
        'selected_refresh': selected,
        'raw_archives': raw_archives,
        'media_databases': media_results,
        'auxiliary_databases': auxiliary_results,
        'archive_status': status,
    }
