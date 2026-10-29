"""Pure header classifiers for the macOS WeChat probe.

These functions only look at bytes the caller already read. They do not open
files, attach to processes, or decide that a database can be decrypted.
"""

from __future__ import annotations

import hashlib

SQLITE_MAGIC = b"SQLite format 3\x00"
WAL_MAGICS = (b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83")
V2_MAGIC = b"\x07\x08V2\x08\x07"
V1_MAGIC = b"\x07\x08V1\x08\x07"
PAGE_SIZE = 4096
SALT_LENGTH = 16


def _salt_fields(header: bytes) -> dict:
    if len(header) < SALT_LENGTH:
        return {"salt_hex": None, "salt_sha256_16": None}
    salt = header[:SALT_LENGTH]
    return {
        "salt_hex": salt.hex(),
        "salt_sha256_16": hashlib.sha256(salt).hexdigest()[:16],
    }


def classify_database_header(header: bytes, size: int) -> dict:
    """Classify a database prefix.

    ``possible_sqlcipher4_page4096`` means the file is large enough and aligned
    like the Windows 4.x / SQLCipher 4 layout in ``noteford.crypto``. It is not
    proof of the cipher. ``salt_hex`` is the first 16 bytes of an encrypted
    candidate; it is not an encryption key.
    """

    multiple_4096 = size > 0 and size % PAGE_SIZE == 0
    multiple_1024 = size > 0 and size % 1024 == 0
    plaintext = header.startswith(SQLITE_MAGIC)
    if plaintext:
        kind = "sqlite_plaintext"
    elif len(header) >= SALT_LENGTH and size >= PAGE_SIZE and multiple_4096:
        kind = "possible_sqlcipher4_page4096"
    elif len(header) >= SALT_LENGTH and size >= 1024 and multiple_1024:
        kind = "possible_sqlcipher3_page1024"
    elif size < SALT_LENGTH:
        kind = "too_small"
    else:
        kind = "unaligned_or_unknown"
    result = {
        "classification": kind,
        "size": size,
        "multiple_of_4096": multiple_4096,
        "multiple_of_1024": multiple_1024,
        "starts_with_sqlite_magic": plaintext,
    }
    if plaintext:
        result["salt_hex"] = None
        result["salt_sha256_16"] = None
    else:
        result.update(_salt_fields(header))
    return result


def classify_wal_header(header: bytes, size: int) -> dict:
    magic = header[:4]
    return {
        "classification": "sqlite_wal_header" if magic in WAL_MAGICS else "other",
        "size": size,
        "magic_hex": magic.hex() if magic else "",
        "multiple_of_frame": size >= 32 and (size - 32) % (24 + PAGE_SIZE) == 0,
    }


def classify_dat_header(header: bytes, size: int) -> dict:
    if header.startswith(V2_MAGIC):
        kind = "wechat_v2"
    elif header.startswith(V1_MAGIC):
        kind = "wechat_v1"
    else:
        kind = "other"
    return {
        "classification": kind,
        "size": size,
        "magic_hex": header[:6].hex() if len(header) >= 6 else header.hex(),
    }
