"""Read-only account-local image/video resource lookup for per-message file aliases."""
from __future__ import annotations

import re
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from .decode import decode_text_blob, _safe_xml_root
from .crypto import decrypt_database, merge_encrypted_wal, quick_check
from .snapshot import copy_database_snapshot


def read_image_resources(database: Path, account_root: Path) -> dict[str, list[Path]]:
    root = account_root.resolve()
    con = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        rows = con.execute('''SELECT r.md5,r.file_name,a.username,b.username
            FROM image_hardlink_info_v4 r
            JOIN dir2id a ON a.rowid=r.dir1 JOIN dir2id b ON b.rowid=r.dir2
            LIMIT 100001''').fetchall()
        if len(rows) > 100000:
            raise ValueError('image_resource_limit')
        result: dict[str, list[Path]] = {}
        for digest, name, chat, month in rows:
            if not all(isinstance(value, str) for value in (digest, name, chat, month)):
                continue
            if (not re.fullmatch(r'[0-9a-fA-F]{32}', digest)
                or not re.fullmatch(r'[0-9a-fA-F]{32}(?:_h)?\.dat', name)
                or not re.fullmatch(r'[0-9a-fA-F]{32}', chat)
                or not re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])', month)):
                continue
            path = root / 'msg' / 'attach' / chat / month / 'Img' / name
            if path.resolve().is_relative_to(root):
                result.setdefault(digest.lower(), []).append(path)
        return result
    finally:
        con.close()


def read_video_resources(database: Path, account_root: Path) -> dict[str, list[Path]]:
    root = account_root.resolve()
    con = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='video_hardlink_info_v4'").fetchone():
            return {}
        rows = con.execute('''SELECT r.md5,r.file_name,a.username FROM video_hardlink_info_v4 r
            JOIN dir2id a ON a.rowid=r.dir1 LIMIT 100001''').fetchall()
        if len(rows) > 100000:
            raise ValueError('video_resource_limit')
        result: dict[str, list[Path]] = {}
        for digest, name, month in rows:
            if not all(isinstance(v, str) for v in (digest, name, month)):
                continue
            if (not re.fullmatch(r'[0-9a-fA-F]{32}', digest)
                    or not re.fullmatch(r'[0-9a-fA-F]{32}\.mp4', name)
                    or not re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])', month)):
                continue
            path = root / 'msg' / 'video' / month / name
            if path.resolve().is_relative_to(root):
                result.setdefault(digest.lower(), []).append(path)
        return result
    finally:
        con.close()


class ImageResourceLookup:
    def __init__(self, account_root: Path, index_path: Path, runtime: Path, candidates: Path):
        self.account_root = account_root.resolve()
        self.index_path = index_path.resolve()
        self.runtime = runtime.resolve()
        self.candidates = candidates
        self.source = self.account_root / 'db_storage' / 'hardlink' / 'hardlink.db'
        self._lock = threading.Lock()
        self._checked = float('-inf')
        self._fingerprint = None
        self._resources: dict[str, list[Path]] = {}
        self._video_resources: dict[str, list[Path]] = {}

    def find_image(self, uid: str, *, force: bool = False) -> tuple[str | None, list[Path]]:
        return self._find(uid, 'image', force=force)

    def find_video(self, uid: str, *, force: bool = False) -> tuple[str | None, list[Path]]:
        return self._find(uid, 'video', force=force)

    def _find(self, uid: str, kind: str, *, force: bool) -> tuple[str | None, list[Path]]:
        try:
            con = sqlite3.connect(self.index_path.as_uri() + '?mode=ro', uri=True)
            try:
                row = con.execute('''SELECT message_content FROM messages
                    WHERE uid=? AND account=? AND local_type=?''',
                    (uid, self.account_root.name, 3 if kind == 'image' else 43)).fetchone()
            finally:
                con.close()
            if row is None:
                return None, []
            text, error = decode_text_blob(row[0])
            # Group sender prefixes are outside the XML. Never search nested
            # quotes for a digest or interpret arbitrary metadata as a path.
            root = _safe_xml_root(text[text.find('<'):]) if not error and '<' in text else None
            node = root.find('img' if kind == 'image' else 'videomsg') if root is not None and root.tag == 'msg' else None
            digest = node.get('md5', '').lower() if node is not None else ''
            if not re.fullmatch(r'[0-9a-f]{32}', digest):
                return None, []
        except (OSError, ValueError, sqlite3.Error):
            return None, []
        with self._lock:
            if force or time.monotonic() - self._checked >= 10:
                self._refresh()
            resources = self._resources if kind == 'image' else self._video_resources
            return digest, list(resources.get(digest, []))

    def _refresh(self) -> None:
        self._checked = time.monotonic()
        try:
            paths = [self.source, Path(str(self.source) + '-wal')]
            fingerprint = tuple((p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else None for p in paths)
            if fingerprint[0] is None:
                raise FileNotFoundError()
            if fingerprint == self._fingerprint:
                return
            if sum(item[0] for item in fingerprint if item) > 128 * 1024 * 1024:
                raise ValueError('image_resource_snapshot_limit')
            self.runtime.mkdir(parents=True, exist_ok=True)
            # Lazy import avoids the refresh -> batch -> media import cycle.
            from .refresh import _match_or_refresh_key
            with tempfile.TemporaryDirectory(prefix='image-resource-', dir=self.runtime) as folder:
                work = Path(folder)
                snapshot = copy_database_snapshot(self.source, work / 'encrypted', retries=3)
                encrypted = snapshot / self.source.name
                key, _ = _match_or_refresh_key(encrypted, self.candidates, allow_memory_rescan=False)
                database = work / 'resources.db'
                decrypt_database(encrypted, database, key)
                merge_encrypted_wal(database, Path(str(encrypted) + '-wal'), key)
                if not quick_check(database):
                    raise ValueError('image_resource_snapshot_invalid')
                resources = read_image_resources(database, self.account_root)
                videos = read_video_resources(database, self.account_root)
            self._resources = resources
            self._video_resources = videos
            self._fingerprint = fingerprint
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            self._resources = {}
            self._video_resources = {}
            self._fingerprint = None
