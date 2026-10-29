from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import urllib.request
from urllib.parse import urlsplit, urlunsplit
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .image_crypto import image_extension
from .https_context import download_context


def latest_decrypted_database(
    project_root: Path,
    account: str,
    category: str,
    stem: str,
) -> Path | None:
    state = project_root / 'data' / 'state' / account / category / f'{stem}.json'
    if not state.is_file():
        return None
    try:
        payload = json.loads(state.read_text(encoding='utf-8'))
        path = Path(str(payload.get('decrypted') or '')).resolve()
    except (OSError, ValueError, TypeError):
        return None
    return path if path.is_file() else None


class AvatarResolver:
    def __init__(
        self,
        project_root: Path,
        account: str,
        cache_root: Path,
    ):
        self.project_root = project_root.resolve()
        self.account = account
        self.cache_root = cache_root.resolve()
        self.head_image_db = latest_decrypted_database(
            self.project_root,
            self.account,
            'head_image',
            'head_image',
        )

    @staticmethod
    def _safe_name(username: str) -> str:
        return hashlib.sha256(username.encode('utf-8', 'surrogatepass')).hexdigest()[:24]

    def export_many(self, usernames: Iterable[str | None]) -> dict[str, str]:
        wanted = sorted({str(value) for value in usernames if value})
        if not wanted:
            return {}
        cached = {}
        avatar_root = self.cache_root / 'avatars'
        for name in wanted:
            matches = sorted(avatar_root.glob(f'{self._safe_name(name)}-*'), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in matches:
                if path.suffix in {'.jpg', '.png', '.gif', '.webp'} and path.stat().st_size and image_extension(path.read_bytes()) in {'jpg', 'png', 'gif', 'webp'}:
                    cached[name] = str(path.resolve())
                    break
        if not self.head_image_db:
            return cached

        rows: list[tuple[str, str | None, bytes]] = []
        con = sqlite3.connect(
            f'file:{self.head_image_db.as_posix()}?mode=ro',
            uri=True,
        )
        try:
            for start in range(0, len(wanted), 400):
                chunk = wanted[start : start + 400]
                placeholders = ','.join('?' for _ in chunk)
                query = (
                    'SELECT username, md5, image_buffer '
                    f'FROM head_image WHERE username IN ({placeholders})'
                )
                rows.extend(con.execute(query, chunk).fetchall())
        finally:
            con.close()

        out: dict[str, str] = cached
        avatar_root = self.cache_root / 'avatars'
        avatar_root.mkdir(parents=True, exist_ok=True)

        for username, md5, blob in rows:
            if not blob:
                continue
            data = bytes(blob)
            ext = image_extension(data)
            if ext not in {'jpg', 'png', 'gif', 'webp'}:
                continue
            version = hashlib.sha256(data).hexdigest()[:12]
            destination = avatar_root / f'{self._safe_name(username)}-{version}.{ext}'
            if not destination.is_file() or destination.read_bytes() != data:
                temp_path: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=avatar_root,
                        prefix=destination.name + '.',
                        suffix='.tmp',
                        delete=False,
                    ) as handle:
                        handle.write(data)
                        temp_path = Path(handle.name)
                    temp_path.replace(destination)
                finally:
                    if temp_path is not None:
                        temp_path.unlink(missing_ok=True)
            out[str(username)] = str(destination.resolve())
        return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_avatar(url: str) -> bytes:
    # Only the known WeChat avatar hosts, no redirects, credentials or arbitrary URLs.
    parsed = urlsplit(url)
    if (parsed.scheme not in {'http', 'https'} or parsed.hostname not in {
        'wx.qlogo.cn', 'mmhead.c2c.wechat.com', 'mmhead.hk.wechat.com'
    } or parsed.username or parsed.password or parsed.port not in {None, 80, 443}):
        raise ValueError('unsupported_avatar_url')
    secure = urlunsplit(('https', parsed.hostname, parsed.path, parsed.query, ''))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                         urllib.request.HTTPSHandler(context=download_context()))
    with opener.open(secure, timeout=5) as response:
        data = response.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024 or image_extension(data) not in {'jpg', 'png', 'gif', 'webp'}:
        raise ValueError('invalid_avatar_image')
    return data


def sync_followed_avatars(index_path: Path, resolver: AvatarResolver, *, fetch=download_avatar) -> dict:
    con = sqlite3.connect(f'file:{index_path.resolve().as_posix()}?mode=ro', uri=True)
    try:
        names = {row[0] for row in con.execute("""
            SELECT username FROM conversation_selection WHERE account=? AND state='following'
            UNION
            SELECT sm.username FROM messages m
            JOIN conversation_selection s ON s.account=m.account AND s.chat_table=m.chat_table
            JOIN sender_map sm ON sm.account=m.account AND sm.source_db=m.source_db
              AND sm.sender_rowid=CAST(m.real_sender_id AS INTEGER)
            WHERE m.account=? AND s.state='following'
        """, (resolver.account, resolver.account)) if row[0] and row[0] != 'system'}
    finally:
        con.close()
    available = resolver.export_many(names)
    missing = names - available.keys()
    contact = latest_decrypted_database(resolver.project_root, resolver.account, 'contact', 'contact')
    urls = {}
    if missing and contact:
        con = sqlite3.connect(f'file:{contact.as_posix()}?mode=ro', uri=True)
        try:
            columns = {r[1] for r in con.execute('PRAGMA table_info(contact)')}
            if {'small_head_url', 'big_head_url'} <= columns:
                wanted = sorted(missing)
                for start in range(0, len(wanted), 400):
                    chunk = wanted[start:start + 400]
                    for name, small, big in con.execute('SELECT username,small_head_url,big_head_url FROM contact WHERE username IN (' + ','.join('?' for _ in chunk) + ')', chunk):
                        if small or big:
                            urls[name] = small or big
        finally:
            con.close()

    def recover(item):
        name, url = item
        temporary = None
        try:
            data = fetch(url)
            ext = image_extension(data)
            if ext not in {'jpg', 'png', 'gif', 'webp'}:
                return False
            root = resolver.cache_root / 'avatars'
            root.mkdir(parents=True, exist_ok=True)
            destination = root / f'{resolver._safe_name(name)}-{hashlib.sha256(data).hexdigest()[:12]}.{ext}'
            with tempfile.NamedTemporaryFile(dir=root, suffix='.tmp', delete=False) as handle:
                handle.write(data)
                temporary = Path(handle.name)
            temporary.replace(destination)
            return True
        except (OSError, ValueError):
            return False
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    with ThreadPoolExecutor(max_workers=4) as pool:
        recovered = sum(pool.map(recover, urls.items()))
    result = {'checked': len(names), 'available': len(available) + recovered,
              'recovered': recovered, 'missing': len(missing) - recovered,
              'completed_at': datetime.now(timezone.utc).isoformat()}
    state = resolver.project_root / 'data' / 'state' / 'avatar_sync.json'
    state.parent.mkdir(parents=True, exist_ok=True)
    temporary = state.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(result), encoding='utf-8')
    temporary.replace(state)
    return result
