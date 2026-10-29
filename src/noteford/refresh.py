from __future__ import annotations

import json
import sys
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .crypto import decrypt_database, find_matching_key, merge_encrypted_wal, quick_check
from .indexer import IndexStats, index_messages
from .metadata import sync_metadata
from .snapshot import copy_database_snapshot


MESSAGE_DB_RE = re.compile(r'^(?:biz_)?message_\d+\.db$')
MEDIA_DB_RE = re.compile(r'^media_\d+\.db$')


class KeyUnavailable(RuntimeError):
    """No usable local key; login or a supported WeChat version may be needed."""


@dataclass
class RefreshResult:
    source: str
    snapshot: str
    decrypted: str
    key_fingerprint: str
    wal_frames_applied: int
    quick_check: bool
    skipped: bool
    index: dict[str, int] | None


@dataclass
class RawSnapshotResult:
    source: str
    snapshot: str
    skipped: bool


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding='utf-8'))


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def _source_state(database: Path) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for path in (database, Path(str(database) + '-wal'), Path(str(database) + '-shm')):
        if path.is_file():
            st = path.stat()
            out[path.name] = {'size': int(st.st_size), 'mtime_ns': int(st.st_mtime_ns)}
    return out


def _load_candidates(path: Path) -> list[dict]:
    if sys.platform == 'darwin':
        from .mac_key_storage import cached_candidates
        return cached_candidates(path)
    payload = _read_json(path)
    return list(payload.get('candidates') or [])


def _match_or_refresh_key(
    snapshot_database: Path,
    candidates_path: Path,
    allow_memory_rescan: bool,
) -> tuple[bytes, str]:
    match = find_matching_key(snapshot_database, _load_candidates(candidates_path))
    if match:
        return match
    if not allow_memory_rescan:
        raise KeyUnavailable('no cached key matches this database')

    from .platform_seam import PlatformCapabilityError, acquire_database_key_candidates

    try:
        candidates = acquire_database_key_candidates()
    except PlatformCapabilityError as exc:
        raise KeyUnavailable(str(exc)) from exc
    if not candidates:
        raise KeyUnavailable('read-only Weixin.exe key scan returned no candidates')
    if isinstance(candidates[0], dict):
        records = list(candidates)
    else:
        from .winmem import save_candidates

        save_candidates(candidates_path, candidates)
        records = _load_candidates(candidates_path)
    match = find_matching_key(snapshot_database, records)
    if not match:
        raise KeyUnavailable('fresh key scan still did not match this database')
    return match


def refresh_database(
    live_database: Path,
    account: str,
    project_root: Path,
    candidates_path: Path,
    index_path: Path,
    overlap_local_ids: int = 200,
    allow_memory_rescan: bool = True,
    index_all_messages: bool = True,
) -> RefreshResult:
    live_database = live_database.resolve()
    project_root = project_root.resolve()
    source_state = _source_state(live_database)

    category = live_database.parent.name
    stem = live_database.stem
    state_path = project_root / 'data' / 'state' / account / category / f'{stem}.json'
    previous = _read_json(state_path)

    previous_decrypted = Path(previous.get('decrypted', '')) if previous.get('decrypted') else None
    if (
        previous.get('source_state') == source_state
        and previous_decrypted is not None
        and previous_decrypted.is_file()
        and quick_check(previous_decrypted)
        and (
            not MESSAGE_DB_RE.match(live_database.name)
            or not index_all_messages
            or previous.get('index') is not None
        )
    ):
        return RefreshResult(
            source=str(live_database),
            snapshot=str(previous.get('snapshot', '')),
            decrypted=str(previous_decrypted),
            key_fingerprint=str(previous.get('key_fingerprint', '')),
            wal_frames_applied=int(previous.get('wal_frames_applied', 0)),
            quick_check=True,
            skipped=True,
            index=None,
        )

    encrypted_root = project_root / 'data' / 'encrypted' / account / category
    snapshot_dir = copy_database_snapshot(live_database, encrypted_root, retries=8, settle_seconds=0.25)
    snapshot_db = snapshot_dir / live_database.name

    key, fingerprint = _match_or_refresh_key(
        snapshot_db, candidates_path, allow_memory_rescan=allow_memory_rescan
    )

    decrypted_dir = project_root / 'data' / 'decrypted' / account / category / snapshot_dir.name
    decrypted_dir.mkdir(parents=True, exist_ok=True)
    decrypted_db = decrypted_dir / live_database.name
    decrypt_database(snapshot_db, decrypted_db, key)

    encrypted_wal = Path(str(snapshot_db) + '-wal')
    wal_frames = merge_encrypted_wal(decrypted_db, encrypted_wal, key) if encrypted_wal.is_file() else 0
    ok = quick_check(decrypted_db)
    if not ok:
        raise RuntimeError(f'decrypted snapshot failed PRAGMA quick_check: {decrypted_db}')

    stats: IndexStats | None = None
    if index_all_messages and MESSAGE_DB_RE.match(live_database.name):
        stats = index_messages(
            source_db_path=decrypted_db,
            index_path=index_path,
            account=account,
            overlap_local_ids=overlap_local_ids,
        )

    state = {
        'schema': 1,
        'source': str(live_database),
        'source_state': source_state,
        'snapshot': str(snapshot_dir),
        'decrypted': str(decrypted_db),
        'key_fingerprint': fingerprint,
        'wal_frames_applied': wal_frames,
        'quick_check': True,
        'last_success_at': datetime.now(timezone.utc).isoformat(),
        'index': asdict(stats) if stats else None,
    }
    _write_json_atomic(state_path, state)

    return RefreshResult(
        source=str(live_database),
        snapshot=str(snapshot_dir),
        decrypted=str(decrypted_db),
        key_fingerprint=fingerprint,
        wal_frames_applied=wal_frames,
        quick_check=True,
        skipped=False,
        index=asdict(stats) if stats else None,
    )


def snapshot_raw_database(
    live_database: Path,
    account: str,
    project_root: Path,
) -> RawSnapshotResult:
    """Preserve an auxiliary database without assuming its decryption format."""
    live_database = live_database.resolve(strict=True)
    project_root = project_root.resolve()
    category = live_database.parent.name
    stem = live_database.stem
    source_state = _source_state(live_database)
    state_path = project_root / 'data' / 'state' / account / category / f'{stem}.json'
    previous = _read_json(state_path)
    previous_snapshot = Path(str(previous.get('snapshot') or ''))
    if previous.get('source_state') == source_state and previous_snapshot.is_dir():
        return RawSnapshotResult(str(live_database), str(previous_snapshot), True)

    encrypted_root = project_root / 'data' / 'encrypted' / account / category
    snapshot_dir = copy_database_snapshot(live_database, encrypted_root, retries=8, settle_seconds=0.25)
    _write_json_atomic(state_path, {
        'schema': 1,
        'raw_only': True,
        'source': str(live_database),
        'source_state': source_state,
        'snapshot': str(snapshot_dir),
        'last_success_at': datetime.now(timezone.utc).isoformat(),
    })
    return RawSnapshotResult(str(live_database), str(snapshot_dir), False)


def discover_message_databases(wechat_root: Path, account: str) -> list[Path]:
    message_dir = wechat_root / account / 'db_storage' / 'message'
    if not message_dir.is_dir():
        raise FileNotFoundError(message_dir)
    return sorted(
        path for path in message_dir.iterdir()
        if path.is_file() and MESSAGE_DB_RE.match(path.name)
    )


def discover_media_databases(wechat_root: Path, account: str) -> list[Path]:
    message_dir = wechat_root / account / 'db_storage' / 'message'
    if not message_dir.is_dir():
        raise FileNotFoundError(message_dir)
    return sorted(
        path for path in message_dir.iterdir()
        if path.is_file() and MEDIA_DB_RE.match(path.name)
    )


def discover_auxiliary_databases(wechat_root: Path, account: str) -> list[Path]:
    storage = wechat_root / account / 'db_storage'
    if not storage.is_dir():
        raise FileNotFoundError(storage)
    out: list[Path] = []
    primary = {('session', 'session.db'), ('contact', 'contact.db'),
               ('head_image', 'head_image.db')}
    for category in storage.iterdir():
        if not category.is_dir() or category.is_symlink():
            continue
        for path in category.iterdir():
            if not path.is_file() or path.is_symlink() or path.suffix.lower() != '.db':
                continue
            if (category.name, path.name) in primary:
                continue
            if category.name == 'message' and (
                MESSAGE_DB_RE.match(path.name) or MEDIA_DB_RE.match(path.name)
            ):
                continue
            out.append(path)
    return sorted(out)


def refresh_account(
    wechat_root: Path,
    account: str,
    project_root: Path,
    candidates_path: Path,
    index_path: Path,
    overlap_local_ids: int = 200,
    allow_memory_rescan: bool = True,
) -> list[RefreshResult | RawSnapshotResult]:
    storage = wechat_root / account / 'db_storage'
    session_live = storage / 'session' / 'session.db'
    contact_live = storage / 'contact' / 'contact.db'

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

    message_results: list[RefreshResult] = []
    for database in discover_message_databases(wechat_root, account):
        message_results.append(
            refresh_database(
                live_database=database,
                account=account,
                project_root=project_root,
                candidates_path=candidates_path,
                index_path=index_path,
                overlap_local_ids=overlap_local_ids,
                allow_memory_rescan=allow_memory_rescan,
            )
        )

    media_results: list[RefreshResult] = []
    for database in discover_media_databases(wechat_root, account):
        media_results.append(
            refresh_database(
                live_database=database,
                account=account,
                project_root=project_root,
                candidates_path=candidates_path,
                index_path=index_path,
                overlap_local_ids=overlap_local_ids,
                allow_memory_rescan=allow_memory_rescan,
            )
        )

    auxiliary_results: list[RawSnapshotResult] = []
    for database in discover_auxiliary_databases(wechat_root, account):
        auxiliary_results.append(
            snapshot_raw_database(database, account, project_root)
        )

    sync_metadata(
        index_path=index_path,
        account=account,
        session_db=Path(session_result.decrypted),
        contact_db=Path(contact_result.decrypted),
        message_dbs=[Path(result.decrypted) for result in message_results],
    )
    from .solitaire import refresh_native_memberships
    refresh_native_memberships(
        wechat_root=wechat_root, account=account, project_root=project_root,
        candidates_path=candidates_path, index_path=index_path,
        allow_memory_rescan=allow_memory_rescan,
    )
    return [
        session_result,
        contact_result,
        *message_results,
        *media_results,
        *auxiliary_results,
    ]
