"""Read account-bound Mac emoticon ciphertext; never modify WeChat files."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from itertools import islice
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .assets import latest_decrypted_database
from .image_crypto import image_extension
from .libraries import account_username
from .mac_media import kvcomm_roots

MAX_BYTES = 64 * 1024 * 1024
_MD5 = re.compile(r'[a-f0-9]{32}')


def _read(path: Path, root: Path) -> bytes | None:
    try:
        if (not path.resolve().is_relative_to(root.resolve()) or path.is_symlink()
                or any(p.is_symlink() for p in path.parents if p != root)
                or not 0 < path.stat().st_size <= MAX_BYTES):
            return None
        with path.open('rb') as handle:
            data = handle.read(MAX_BYTES + 1)
        return data if len(data) <= MAX_BYTES else None
    except OSError:
        return None


def _keys(account: str):
    """UIN filenames are candidates only; acceptance requires the full message hash."""
    suffix = account.rsplit('_', 1)[-1]
    if not re.fullmatch(r'[a-fA-F0-9]{4}', suffix):
        return
    seen = set()
    for root in kvcomm_roots():
        if not root.is_dir() or root.is_symlink():
            continue
        for candidate in islice(root.glob('key_*.statistic'), 4096):
            match = re.fullmatch(r'key_(\d{1,10})_.*\.statistic', candidate.name)
            if not match or candidate.is_symlink():
                continue
            uin = match[1]
            if (not 0 < int(uin) < 2**32 or uin in seen
                    or hashlib.md5(uin.encode()).hexdigest()[:4] != suffix.lower()):
                continue
            seen.add(uin)
            # The local sticker key differs from both DAT and CDN encryption.
            yield hashlib.md5((uin + account_username(account) + 'EMOTICON').encode()).digest()
            if len(seen) >= 32:
                return


def _decrypt(data: bytes, key: bytes) -> bytes | None:
    if not data or len(data) % 16:
        return None
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    pad = plain[-1]
    if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad]) * pad:
        return None
    return plain[:-pad]


def _package(config, digest):
    database = latest_decrypted_database(Path(config['project_root']), config['account'], 'emoticon', 'emoticon')
    if database is None:
        return None
    try:
        with sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as db:
            row = db.execute('''SELECT package_id_,emoticon_offset_,emoticon_size_
                FROM kStoreEmoticonFilesTable WHERE md5_=? LIMIT 1''', (digest,)).fetchone()
        if row:
            product, offset, size = row
            if (isinstance(product, str) and 0 < len(product) <= 512
                    and isinstance(offset, int) and isinstance(size, int)
                    and 0 <= offset < MAX_BYTES and 0 < size <= MAX_BYTES - offset):
                return hashlib.md5(product.encode()).hexdigest(), offset, size
    except (OSError, sqlite3.Error):
        pass
    return None


def read_local(config, source: Path, digest: str, *, should_continue=None):
    """Return exact original bytes from an individual cache or a store package."""
    from .cdn_download import DownloadError
    def allowed():
        if should_continue is not None and not should_continue():
            raise DownloadError('download_cancelled')
    def verified(data):
        return (data is not None and hashlib.md5(data).hexdigest() == digest
                and image_extension(data) in {'png', 'jpg', 'gif', 'webp', 'wxgf'})
    allowed()
    if not _MD5.fullmatch(digest):
        return None
    keys = None
    candidates = list(islice((source / 'cache').glob(f'*/Emoticon/{digest[:2]}/{digest}'), 48))
    candidates.append(source / 'business/emoticon/Persist' / digest[:2] / digest)
    package = _package(config, digest)
    if package:
        name, offset, size = package
        candidates.append(source / 'business/emoticon/PersistStore' / name[:2] / name)
    for index, path in enumerate(candidates):
        allowed()
        data = _read(path, source)
        if data is None:
            continue
        packaged = package is not None and index == len(candidates) - 1
        def content(plain):
            return plain[offset:offset + size] if packaged and plain is not None else plain
        if verified(content(data)):
            return content(data)
        if keys is None:
            keys = list(_keys(config['account']))
        for key in keys:
            allowed()
            plain = content(_decrypt(data, key))
            if verified(plain):
                return plain
    return None
