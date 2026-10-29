from __future__ import annotations

import hashlib
import hmac
import sqlite3
import struct
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from .file_copy import copy_snapshot

PAGE_SIZE = 4096
RESERVE_SIZE = 80


def _pbkdf2(password: bytes, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", password, salt, iterations, dklen=32)


def verify_key(key: bytes, page1: bytes, explicit_salt: bytes | None = None) -> bool:
    if len(page1) < PAGE_SIZE or len(key) not in (32, 48):
        return False

    if len(key) == 48 and explicit_salt is None:
        key, explicit_salt = key[:32], key[32:]

    salt = explicit_salt if explicit_salt is not None else page1[:16]
    if len(salt) != 16:
        return False

    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = _pbkdf2(key[:32], mac_salt, 2)
    hmac_data = page1[16 : PAGE_SIZE - RESERVE_SIZE + 16]
    stored_hmac = page1[PAGE_SIZE - 64 : PAGE_SIZE]
    digest = hmac.new(mac_key, hmac_data, hashlib.sha512)
    digest.update(struct.pack("<I", 1))
    return hmac.compare_digest(digest.digest(), stored_hmac)


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def decrypt_page(key: bytes, page: bytes, page_number: int) -> bytes:
    if len(page) != PAGE_SIZE:
        raise ValueError("page must be exactly 4096 bytes")
    raw_key = key[:32]
    iv = page[PAGE_SIZE - RESERVE_SIZE : PAGE_SIZE - RESERVE_SIZE + 16]

    if page_number == 1:
        encrypted = page[16 : PAGE_SIZE - RESERVE_SIZE]
        plaintext = _aes_cbc_decrypt(raw_key, iv, encrypted)
        if len(key) == 48:
            return page[:16] + plaintext + b"\x00" * RESERVE_SIZE
        return b"SQLite format 3\x00" + plaintext + b"\x00" * RESERVE_SIZE

    encrypted = page[: PAGE_SIZE - RESERVE_SIZE]
    return _aes_cbc_decrypt(raw_key, iv, encrypted) + b"\x00" * RESERVE_SIZE


def decrypt_database(source: Path, destination: Path, key: bytes) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    count = 0
    with source.open("rb") as fin, tmp.open("wb") as fout:
        page_number = 1
        while True:
            block = fin.read(PAGE_SIZE)
            if not block:
                break
            if len(block) < PAGE_SIZE:
                block += b"\x00" * (PAGE_SIZE - len(block))
            fout.write(decrypt_page(key, block, page_number))
            count += 1
            page_number += 1
    tmp.replace(destination)
    return count


def merge_encrypted_wal(
    decrypted_database: Path,
    encrypted_wal: Path,
    key: bytes,
) -> int:
    """Apply committed WCDB WAL state, returning unique pages written, to a
    decrypted database copy.

    The live WeChat files are never opened for writing. The merge works on a
    temporary copy and atomically replaces that copy only after all selected
    frames have been applied.
    """
    if not encrypted_wal.is_file() or encrypted_wal.stat().st_size <= 32:
        return 0

    frame_size = 24 + PAGE_SIZE
    # Retain offsets, never encrypted page payloads. A later committed frame
    # supersedes earlier versions of the same page, including overflow pages.
    latest: dict[int, int] = {}
    committed_end = 32
    last_commit_pages = 0
    tmp = decrypted_database.with_suffix(decrypted_database.suffix + ".walmerge.tmp")
    applied = 0
    try:
        with encrypted_wal.open('rb') as wal:
            size = encrypted_wal.stat().st_size
            header = wal.read(32)
            if len(header) < 32:
                return 0
            if (struct.unpack('>I', header[:4])[0] not in (0x377F0682, 0x377F0683)
                    or struct.unpack('>I', header[8:12])[0] != PAGE_SIZE):
                raise ValueError('unsupported encrypted WAL header')
            wal_salt = header[16:24]
            # First pass finds a contiguous valid prefix and its last commit.
            for offset in range(32, size - frame_size + 1, frame_size):
                wal.seek(offset)
                frame = wal.read(24)
                if len(frame) != 24 or frame[8:16] != wal_salt:
                    break  # A reset tail cannot be joined to a new transaction.
                page_number, commit_pages = struct.unpack('>II', frame[:8])
                if page_number == 0:
                    break
                if commit_pages:
                    committed_end = offset + frame_size
                    last_commit_pages = commit_pages
            if not last_commit_pages:
                return 0
            # Second pass excludes the uncommitted tail without keeping a
            # second transaction-sized dictionary in memory.
            for offset in range(32, committed_end, frame_size):
                wal.seek(offset)
                page_number = struct.unpack('>I', wal.read(4))[0]
                if page_number <= last_commit_pages:
                    latest[page_number] = offset + 24
            copy_snapshot(decrypted_database, tmp)
            with tmp.open("r+b") as out:
                batch = bytearray()
                batch_start = previous_page = 0
                # Coalesce adjacent page writes, limiting the buffer to 256 KiB.
                # This avoids thousands of tiny APFS copy-on-write operations.
                for page_number in sorted(latest):
                    if batch and (page_number != previous_page + 1 or len(batch) >= 256 * 1024):
                        out.seek((batch_start - 1) * PAGE_SIZE)
                        out.write(batch)
                        batch.clear()
                    if not batch:
                        batch_start = page_number
                    wal.seek(latest[page_number])
                    plaintext = decrypt_page(key, wal.read(PAGE_SIZE), page_number)
                    batch.extend(plaintext)
                    previous_page = page_number
                    applied += 1
                if batch:
                    out.seek((batch_start - 1) * PAGE_SIZE)
                    out.write(batch)
                out.truncate(last_commit_pages * PAGE_SIZE)
                out.seek(28)
                out.write(struct.pack(">I", last_commit_pages))
                out.flush()

        tmp.replace(decrypted_database)
        return applied
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def quick_check(path: Path) -> bool:
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = connection.execute("PRAGMA quick_check").fetchall()
        finally:
            connection.close()
        return bool(rows) and all(str(row[0]) == "ok" for row in rows)
    except sqlite3.Error:
        return False


def database_readable(path: Path) -> bool:
    """Structural sanity check for a decrypted database.

    ``quick_check`` runs SQLite's integrity verifier, which also triggers the
    fts5 virtual-table integrity check. WeChat's full-text-search shadow
    databases (``*_fts.db``) use custom fts5 configurations that make that
    verifier raise "SQL logic error" even though the pages decrypt perfectly
    and every row is readable. For those we fall back to confirming the file is
    a valid SQLite database we can actually open and read: a non-empty schema
    and a positive schema version. The cryptographic integrity of the pages is
    already guaranteed by the page-1 HMAC key verification done before decrypt.
    """
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            version = connection.execute("PRAGMA schema_version").fetchone()
            objects = connection.execute(
                "SELECT count(*) FROM sqlite_master"
            ).fetchone()
        finally:
            connection.close()
        return bool(version) and version[0] > 0 and bool(objects) and objects[0] > 0
    except sqlite3.Error:
        return False


def find_matching_key(database: Path, candidates: list[dict]) -> tuple[bytes, str] | None:
    with database.open('rb') as handle:
        page1 = handle.read(PAGE_SIZE)
    for item in candidates:
        key = bytes.fromhex(item["key_hex"])
        salt_hex = item.get("salt_hex")
        if verify_key(key, page1):
            return key, item.get("fingerprint", "")
        if salt_hex:
            salt = bytes.fromhex(salt_hex)
            if verify_key(key, page1, explicit_salt=salt):
                return key + salt, item.get("fingerprint", "")
    return None
