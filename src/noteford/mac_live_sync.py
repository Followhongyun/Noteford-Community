"""Refresh managed Mac archives from read-only DB/WAL snapshots.

Only Noteford's managed archive is connected to live WeChat. Imported plaintext
folders keep their offline behavior. No source file is opened for writing.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from .crypto import decrypt_database, merge_encrypted_wal, quick_check, verify_key
from .file_copy import copy_snapshot
from .runtime_lock import RuntimeLock
from .mac_capture import default_db_roots
from .mac_resign import default_state_dir, keys_path

CORE_DATABASE = re.compile(r"^(?:session|contact|(?:biz_)?message_\d+)\.db$")
LIVE_DATABASE = re.compile(r"^(?:session|contact|head_image|hardlink|general|solitaire|emoticon|message_resource|media_\d+|(?:biz_)?message_\d+)\.db$")


class SourcePermissionRequired(PermissionError):
    code = "source_permission_required"


def managed_archive() -> Path:
    return default_state_dir().parent / "data" / "decrypted"


def _storage(account: str) -> Path:
    if not account or Path(account).name != account or account in {".", ".."}:
        raise ValueError("invalid Mac account")
    for root in default_db_roots():
        storage = root / account / "db_storage"
        if (storage / "session" / "session.db").is_file():
            return storage
    raise FileNotFoundError("Mac WeChat source unavailable")


def _databases(storage: Path) -> list[Path]:
    # Path.rglob silently skips unreadable directories. On macOS a revoked
    # container permission must be reported, not mistaken for a missing catalog.
    def on_error(error):
        raise error

    files = []
    for current, directories, names in os.walk(storage, followlinks=False, onerror=on_error):
        directory = Path(current)
        directories[:] = [name for name in directories if not (directory / name).is_symlink()]
        for name in names:
            file = directory / name
            if (LIVE_DATABASE.fullmatch(name) and not file.is_symlink()
                    and not any(parent.is_symlink() for parent in file.parents if parent != storage.parent)):
                files.append(file)
    return sorted(files)


def _stamp(database: Path) -> list:
    result = []
    for path in (database, Path(str(database) + "-wal")):
        try:
            stat = path.stat()
            result.append([stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns])
        except FileNotFoundError:
            result.append(None)
    return result


def _read_manifest(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _publish_snapshot(source: Path, destination: Path, key: bytes, *,
                      previous_base: Path | None = None, previous_stamp=None) -> tuple[Path, list, int]:
    destination.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        staging = Path(tempfile.mkdtemp(prefix=".live-", dir=destination))
        try:
            before = _stamp(source)
            copied = staging / "encrypted"
            copied.mkdir()
            encrypted = copied / source.name
            base = staging / '.source-base'
            reuse = (previous_base is not None and previous_base.is_file()
                     and previous_stamp and before[0] == previous_stamp[0])
            if reuse:
                # This base is immutable: only the separate plain clone is
                # merged. Share the base across generations even without APFS,
                # so retaining recovery generations does not copy it again.
                try:
                    os.link(previous_base, base)
                except OSError:
                    copy_snapshot(previous_base, base)
            else:
                shutil.copyfile(source, encrypted)
            wal = Path(str(source) + "-wal")
            copied_wal = copied / (source.name + "-wal")
            if before[1] is not None:
                try:
                    shutil.copyfile(wal, copied_wal)
                except FileNotFoundError:
                    continue  # A checkpoint raced the copy; retry the pair.
            if _stamp(source) != before:
                continue
            if not reuse:
                with encrypted.open("rb") as handle:
                    page = handle.read(4096)
                if not verify_key(key, page):
                    from .refresh import KeyUnavailable
                    raise KeyUnavailable("mac_live_key_unavailable")
                decrypt_database(encrypted, base, key)
            os.chmod(base, 0o600)
            plain = staging / source.name
            copy_snapshot(base, plain)
            frames = merge_encrypted_wal(plain, copied_wal, key) if copied_wal.exists() else 0
            if not quick_check(plain):
                if reuse:
                    previous_base = None  # Discard a damaged/missing cached base.
                    continue
                raise RuntimeError("mac_live_snapshot_invalid")
            shutil.rmtree(copied)
            os.chmod(plain, 0o600)
            # Hidden staging is never considered by the archive reader. Publish
            # only a verified snapshot; failed refreshes retain the last good one.
            final = destination / f"{source.stem}-live-{time.time_ns()}"
            staging.replace(final)
            return final / source.name, before, frames
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    raise RuntimeError("mac_live_source_busy")


def refresh_managed_archive(archive: Path, account: str) -> dict | None:
    archive = Path(archive)
    if archive.resolve() != managed_archive().resolve():
        return None
    # Also protect direct CLI/MCP callers, not just the desktop refresh lock.
    # OS ownership is released on crash; concurrent calls do not queue work.
    lock_root = archive.parent / 'state' / 'mac-live-locks' / hashlib.sha256(account.encode()).hexdigest()
    with RuntimeLock(lock_root):
        return _refresh_locked(archive, account)


def _refresh_locked(archive: Path, account: str) -> dict:
    try:
        storage = _storage(account)
        files = _databases(storage)
    except PermissionError as exc:
        raise SourcePermissionRequired("mac_wechat_read_permission_required") from exc
    if not {"session.db", "contact.db"} <= {p.name for p in files}:
        raise FileNotFoundError("Mac WeChat catalog unavailable")
    from .mac_key_storage import database_payload, cleanup_hook_files, cleanup_stale_capture_files
    keys = database_payload(keys_path(default_state_dir()))["keys"]
    cleanup_stale_capture_files(default_state_dir())
    cleanup_hook_files(storage.parent.parent)
    by_path = {item.get("relative_path"): item for item in keys}
    state_dir = archive.parent / "state" / "mac-live"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / (hashlib.sha256(account.encode()).hexdigest() + ".json")
    previous = _read_manifest(state_file)
    current = {}
    changed = 0
    frames = 0
    media_errors = []
    published = []
    for source in files:
        relative = f"{account}/db_storage/{source.relative_to(storage).as_posix()}"
        item = by_path.get(relative)
        if not item or not item.get("key_hex"):
            if not CORE_DATABASE.fullmatch(source.name):
                media_errors.append({'database': source.name, 'error': 'key_unavailable'})
                continue
            from .refresh import KeyUnavailable
            raise KeyUnavailable("mac_live_key_unavailable")
        key = bytes.fromhex(item["key_hex"])
        key_revision = hashlib.sha256(key).hexdigest()
        old = previous.get(relative, {})
        output = (archive / str(old.get("output", ""))).resolve()
        if (old.get("stamp") == _stamp(source) and old.get("key_revision") == key_revision
                and output.is_file() and output.is_relative_to(archive.resolve())
                and not output.is_symlink()):
            current[relative] = old
            continue
        destination = archive / account / source.parent.name
        # The writer lease proves these hidden staging directories belong to
        # interrupted refreshes. Never touch imported or published snapshots.
        if destination.is_dir():
            for abandoned in destination.glob('.live-*'):
                if abandoned.is_dir() and not abandoned.is_symlink():
                    shutil.rmtree(abandoned)
        base = output.parent / '.source-base'
        reusable = (old.get('key_revision') == key_revision
                    and output.is_file() and output.is_relative_to(archive.resolve())
                    and base.is_file() and not base.is_symlink())
        try:
            output, stamp, wal_frames = _publish_snapshot(
                source, destination, key, previous_base=base if reusable else None,
                previous_stamp=old.get('stamp'))
        except (OSError, ValueError, RuntimeError):
            if CORE_DATABASE.fullmatch(source.name):
                raise
            media_errors.append({'database': source.name, 'error': 'snapshot_unavailable'})
            continue
        current[relative] = {"stamp": stamp, "key_revision": key_revision,
                             "output": output.relative_to(archive).as_posix()}
        changed += 1
        frames += wal_frames
        published.append((source, destination))
    if current != previous:
        temporary = state_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(current), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(state_file)
    # Only prune after the manifest is committed. A later database failure or
    # process crash must not delete the generation referenced by the manifest.
    for source, destination in published:
        generations = sorted(destination.glob(source.stem + '-live-*'), key=lambda p: p.name)
        for older in generations[:-2]:
            if older.is_dir() and not older.is_symlink():
                shutil.rmtree(older)
    return {"databases": len(files), "changed": changed, "wal_frames": frames,
            "media_errors": media_errors}


class MacSourceChanges:
    """Cheap source stat polling inside the existing desktop scheduler."""

    def __init__(self, *, clock=time.monotonic, poll_seconds=2.0):
        self.clock = clock
        self.poll_seconds = poll_seconds
        self.next_check = 0.0
        self.previous = None

    def poll(self, records) -> bool:
        now = self.clock()
        if now < self.next_check:
            return False
        self.next_check = now + self.poll_seconds
        signatures = []
        for record in records:
            root = getattr(record, "wechat_root", None)
            if not root or Path(root).resolve() != managed_archive().resolve():
                continue
            try:
                files = _databases(_storage(record.account))
                signatures.append((record.id, [(str(p), _stamp(p)) for p in files]))
            except OSError:
                signatures.append((record.id, "unavailable"))
        previous, self.previous = self.previous, signatures
        return previous is not None and previous != signatures
