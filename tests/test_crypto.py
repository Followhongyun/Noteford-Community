import hashlib
import hmac
import os
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from noteford.crypto import (
    PAGE_SIZE,
    RESERVE_SIZE,
    decrypt_page,
    merge_encrypted_wal,
    verify_key,
)


def test_verify_and_decrypt_synthetic_page1():
    key = bytes(range(32))
    salt = bytes(range(16, 32))
    iv = bytes(range(32, 48))
    plaintext_body = bytearray(PAGE_SIZE - RESERVE_SIZE - 16)
    plaintext_body[:2] = b"\x01\x01"

    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(bytes(plaintext_body)) + encryptor.finalize()

    page = bytearray(PAGE_SIZE)
    page[:16] = salt
    page[16 : PAGE_SIZE - RESERVE_SIZE] = ciphertext
    page[PAGE_SIZE - RESERVE_SIZE : PAGE_SIZE - 64] = iv

    mac_salt = bytes(value ^ 0x3A for value in salt)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, mac_salt, 2, dklen=32)
    digest = hmac.new(
        mac_key,
        bytes(page[16 : PAGE_SIZE - RESERVE_SIZE + 16]),
        hashlib.sha512,
    )
    digest.update(struct.pack("<I", 1))
    page[PAGE_SIZE - 64 :] = digest.digest()

    assert verify_key(key, bytes(page))
    plain = decrypt_page(key, bytes(page), 1)
    assert plain[:16] == b"SQLite format 3\x00"
    assert plain[16:18] == b"\x01\x01"


def test_merge_encrypted_wal_applies_only_committed_frames(tmp_path):
    key = bytes(range(32))
    salt = b"12345678"
    iv = bytes(range(16))

    database = tmp_path / "message.db"
    database.write_bytes(bytearray(PAGE_SIZE * 2))

    def encrypt_page(plaintext: bytes, page_iv: bytes) -> bytes:
        encryptor = Cipher(algorithms.AES(key), modes.CBC(page_iv)).encryptor()
        body = plaintext[: PAGE_SIZE - RESERVE_SIZE]
        ciphertext = encryptor.update(body) + encryptor.finalize()
        return ciphertext + page_iv + (b"\x00" * 64)

    committed_plain = bytearray(PAGE_SIZE)
    committed_plain[:4] = b"\x0dNEW"
    committed_page = encrypt_page(bytes(committed_plain), iv)

    uncommitted_plain = bytearray(PAGE_SIZE)
    uncommitted_plain[:4] = b"\x0dBAD"
    uncommitted_page = encrypt_page(bytes(uncommitted_plain), iv)

    wal_header = (
        struct.pack(">I", 0x377F0682)
        + struct.pack(">I", 3007000)
        + struct.pack(">I", PAGE_SIZE)
        + struct.pack(">I", 1)
        + salt
        + (b"\x00" * 8)
    )
    committed_header = (
        struct.pack(">I", 2)
        + struct.pack(">I", 2)
        + salt
        + (b"\x00" * 8)
    )
    uncommitted_header = (
        struct.pack(">I", 2)
        + struct.pack(">I", 0)
        + salt
        + (b"\x00" * 8)
    )

    wal = tmp_path / "message.db-wal"
    wal.write_bytes(
        wal_header
        + committed_header
        + committed_page
        + uncommitted_header
        + uncommitted_page
    )

    applied = merge_encrypted_wal(database, wal, key)
    assert applied == 1
    merged = database.read_bytes()
    assert merged[PAGE_SIZE : PAGE_SIZE + 4] == b"\x0dNEW"
