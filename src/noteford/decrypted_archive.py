"""Read accounts that are already decrypted on disk.

macOS onboarding uses this instead of the Windows WeChat file scan. The
databases are plaintext SQLite under ``<data>/data/decrypted/<wxid>/``. This
module does not import ``winmem`` and does not decrypt anything.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path

from .libraries import account_username, library_id_for

SQLITE_MAGIC = b"SQLite format 3\x00"
MESSAGE_DB_RE = re.compile(r"^(?:biz_)?message_\d+\.db$")
_MAX_DEPTH = 6


def _plaintext_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def _newest_named(root: Path, name: str) -> Path | None:
    found: list[Path] = []
    if not root.is_dir():
        return None
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        try:
            depth = len(current_path.relative_to(root).parts)
        except ValueError:
            dirnames.clear()
            continue
        if depth > _MAX_DEPTH:
            dirnames.clear()
            continue
        dirnames[:] = [
            item
            for item in dirnames
            if item not in {"__pycache__", ".git"} and not item.startswith(".")
        ]
        if name not in filenames:
            continue
        candidate = current_path / name
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if _plaintext_sqlite(candidate):
            found.append(candidate)
    if not found:
        return None
    return max(found, key=lambda item: item.stat().st_mtime_ns)


def _message_databases(account_dir: Path) -> list[Path]:
    found: list[Path] = []
    if not account_dir.is_dir():
        return found
    for current, dirnames, filenames in os.walk(account_dir, followlinks=False):
        current_path = Path(current)
        try:
            depth = len(current_path.relative_to(account_dir).parts)
        except ValueError:
            dirnames.clear()
            continue
        if depth > _MAX_DEPTH:
            dirnames.clear()
            continue
        dirnames[:] = [item for item in dirnames if not item.startswith(".")]
        for name in filenames:
            if not MESSAGE_DB_RE.match(name):
                continue
            candidate = current_path / name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if _plaintext_sqlite(candidate):
                found.append(candidate)
    newest: dict[str, Path] = {}
    for item in found:
        if item.name not in newest or item.stat().st_mtime_ns > newest[item.name].stat().st_mtime_ns:
            newest[item.name] = item
    return sorted(newest.values(), key=lambda item: item.name)


def _nickname(contact_db: Path, account: str) -> str | None:
    username = account_username(account)
    try:
        con = sqlite3.connect(f"file:{contact_db.resolve().as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            "SELECT nick_name, alias, remark FROM contact WHERE username=? LIMIT 1",
            (username,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if not row:
        return None
    for value in row:
        if isinstance(value, str) and value.strip():
            return value.strip()[:100]
    return None


def _account_dirs(root: Path) -> tuple[Path, list[Path]]:
    """Return ``(archive root, account directories)``."""

    if not root.is_dir():
        return root, []
    children: list[Path] = []
    try:
        entries = sorted(
            (item for item in root.iterdir() if item.is_dir() and not item.is_symlink()),
            key=lambda item: item.name.casefold(),
        )
    except OSError:
        return root, []
    for child in entries:
        if _newest_named(child, "session.db") and _newest_named(child, "contact.db"):
            children.append(child)
    if children:
        return root, children
    if _newest_named(root, "session.db") and _newest_named(root, "contact.db"):
        return root.parent, [root]
    return root, []


def scan_decrypted_archives(roots: list[Path]) -> list[dict]:
    """Candidate rows in the same shape as Windows library discovery."""

    rows: list[dict] = []
    seen: set[str] = set()
    for root in roots:
        base, accounts = _account_dirs(Path(root))
        for account_dir in accounts:
            account = account_dir.name
            library_id = library_id_for(base, account)
            if library_id in seen:
                continue
            seen.add(library_id)
            contact = _newest_named(account_dir, "contact.db")
            display = _nickname(contact, account) if contact is not None else None
            from .mac_metadata import account_avatar
            avatar = account_avatar(_newest_named(account_dir, "head_image.db"), account_username(account))
            rows.append(
                {
                    "id": library_id,
                    "account": account,
                    "source_root": str(base),
                    "source_kind": "decrypted_archive",
                    "display_name": display or account_username(account),
                    "avatar_data_url": avatar,
                    "preview_status": "ready" if avatar and display else "name" if display else "unavailable",
                }
            )
    return rows


def refresh_decrypted_library(
    *,
    wechat_root: Path,
    account: str,
    project_root: Path,
    candidates_path: Path,
    index_path: Path,
    overlap_local_ids: int = 200,
    allow_memory_rescan: bool = True,
    refresh_selected: bool = True,
    message_database_names: set[str] | None = None,
    write_archive_status: bool = True,
) -> dict:
    """Index plaintext session, contact, and message databases.

    ``candidates_path`` and ``allow_memory_rescan`` are accepted so the caller
    can use the same keyword arguments as the Windows catalog refresh. They
    are not used: these files are already decrypted.
    """

    del candidates_path, allow_memory_rescan, refresh_selected
    account_dir = Path(wechat_root) / account
    session_db = _newest_named(account_dir, "session.db")
    contact_db = _newest_named(account_dir, "contact.db")
    if session_db is None or contact_db is None:
        raise FileNotFoundError(account_dir)

    from .indexer import ensure_index_schema, index_messages
    from .metadata import sync_metadata
    from .source_cache import cached_source_stage

    ensure_index_schema(Path(index_path))
    databases = _message_databases(account_dir)
    from .mac_metadata import register_sources, sync_group_members, sender_databases, sync_directory
    def cached(stage, sources, action, *, token=None):
        return cached_source_stage(index_path, account, stage, sources, action,
                                   revision_token=token)[0]

    def index_revision():
        with closing(sqlite3.connect(index_path)) as con:
            return con.execute('SELECT MAX(rowid) FROM runs WHERE account=?', (account,)).fetchone()[0]

    def directory():
        # sync_metadata rebuilds conversations, and newly indexed tables may
        # provide evidence for contacts absent from the session catalog.
        return cached('directory-v1', [session_db, contact_db, *databases],
            lambda: sync_directory(Path(index_path), account, contact_db), token=index_revision())

    metadata = cached('metadata-v1', [session_db, contact_db, *databases], lambda: sync_metadata(
        index_path=Path(index_path), account=account,
        session_db=session_db, contact_db=contact_db,
        message_dbs=sender_databases(databases),
    ))
    metadata["group_members"] = cached('group-members-v1', [contact_db],
        lambda: sync_group_members(Path(index_path), account, contact_db))
    metadata["directory_added"] = directory()
    head_image = _newest_named(account_dir, "head_image.db")
    auxiliary_names = {p.name for p in account_dir.rglob('*.db')
                       if re.fullmatch(r'(?:media_\d+|hardlink|message_resource|general|solitaire|emoticon)\.db', p.name)}
    auxiliary = [path for name in sorted(auxiliary_names) if (path := _newest_named(account_dir, name))]
    register_sources(Path(project_root), account, session_db, contact_db, head_image, auxiliary)
    from .mac_special import refresh_solitaire
    solitaire = _newest_named(account_dir, 'solitaire.db')
    metadata['solitaire'] = cached('solitaire-v1', [solitaire, *databases],
        lambda: refresh_solitaire(solitaire, Path(index_path), account))

    message_results = []
    for database in databases:
        if message_database_names is not None and database.name not in message_database_names:
            continue
        def index_source():
            stats = index_messages(
                source_db_path=database, index_path=Path(index_path),
                account=account, overlap_local_ids=overlap_local_ids)
            return stats.inserted + stats.updated
        indexed, skipped = cached_source_stage(index_path, account,
            f'messages-v1:{database.name}', [database], index_source,
            revision_token=overlap_local_ids)
        message_results.append({"source": str(database), "indexed": 0 if skipped else indexed,
                                "skipped": skipped})

    indexed_changed = any(not item['skipped'] for item in message_results)
    if indexed_changed and not metadata.get('self_inferred'):
        # The catalog is usable before messages; retry identity inference once
        # message evidence exists when this account lacks a self contact row.
        sync_metadata(index_path=Path(index_path), account=account,
                      session_db=session_db, contact_db=contact_db,
                      message_dbs=sender_databases(databases))
    if indexed_changed:
        metadata['directory_added'] = directory()

    project_root = Path(project_root)
    status_path = project_root / "data" / "state" / "hourly_refresh.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status = {
        "last_success_at": datetime.now(timezone.utc).isoformat(),
        "message_databases": len(message_results),
        "media_databases": sum(p.stem.startswith('media_') for p in auxiliary),
        "auxiliary_databases": sum(not p.stem.startswith('media_') for p in auxiliary),
        "source": "decrypted_archive",
    }
    if write_archive_status:
        temporary = status_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
        temporary.replace(status_path)
    return {
        "catalog": {
            "session": {"decrypted": str(session_db), "skipped": True},
            "contact": {"decrypted": str(contact_db), "skipped": True},
            "head_image": {"decrypted": str(head_image), "skipped": True} if head_image else None,
            "metadata": metadata,
        },
        "selected_refresh": [],
        "raw_archives": message_results,
        "media_databases": [str(p) for p in auxiliary if p.stem.startswith('media_')],
        "auxiliary_databases": [str(p) for p in auxiliary if not p.stem.startswith('media_')],
        "archive_status": status,
    }


def follow_decrypted_conversation(
    *, wechat_root: Path, account: str, project_root: Path,
    candidates_path: Path, index_path: Path, username: str,
    overlap_local_ids: int = 200, allow_memory_rescan: bool = True,
) -> dict:
    """Follow a Mac conversation using only the existing plaintext archive."""
    from .indexer import IndexStats, index_messages
    from .selective import _conversation, _record_selection, selection_status, SelectiveRefreshResult

    del project_root, candidates_path, allow_memory_rescan
    conversation = _conversation(index_path, account, username)
    chat_table = str(conversation["chat_table"])
    total = IndexStats()
    sources = []
    for database in _message_databases(Path(wechat_root) / account):
        stats = index_messages(
            source_db_path=database, index_path=index_path, account=account,
            overlap_local_ids=overlap_local_ids, force_full=True,
            table_names=[chat_table],
        )
        if not stats.tables:
            continue
        if database.name not in sources:
            sources.append(database.name)
        for field in ("tables", "scanned", "inserted", "updated", "unchanged"):
            setattr(total, field, getattr(total, field) + getattr(stats, field))
    # A known conversation may have no messages yet. Following it still makes
    # later archive updates visible without inventing a message count.
    _record_selection(index_path, account, username, chat_table,
                      state="following", source_dbs=sources, refreshed=True)
    result = SelectiveRefreshResult(username, chat_table, sources, total.tables,
                                    total.scanned, total.inserted, total.updated, total.unchanged)
    return {"selection": selection_status(index_path, account, username), "refresh": result.to_dict()}
