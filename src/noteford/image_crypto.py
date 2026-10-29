from __future__ import annotations

import hashlib
import json
import re
import struct
import sys
import time
from collections import Counter
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _windows_memory():
    from .winmem import bind_names

    return bind_names(globals())


V2_MAGIC = b'\x07\x08\x56\x32\x08\x07'
ASCII32_RE = re.compile(rb'(?<![A-Za-z0-9])([A-Za-z0-9]{32})(?![A-Za-z0-9])')
U16_RE = re.compile(rb'(?:[A-Za-z0-9]\x00){32}')
FOOTER_MAX = 32


def _looks_like_image(data: bytes) -> bool:
    return (
        data[:3] == b'\xff\xd8\xff'
        or data[:4] in (b'\x89PNG', b'GIF8', b'RIFF', b'wxgf')
    )


def _aligned_aes_block_size(aes_size: int) -> int:
    return aes_size + (16 - aes_size % 16) if aes_size % 16 else aes_size + 16


def _probe_block(dat_path: Path) -> bytes:
    head = dat_path.read_bytes()[:31]
    if len(head) < 31 or head[:6] != V2_MAGIC:
        raise ValueError('sample is not a WeChat V2 image container')
    return head[15:31]


def _valid_aes_key(key16: bytes, probe: bytes) -> bool:
    if len(key16) != 16:
        return False
    try:
        decryptor = Cipher(algorithms.AES(key16), modes.ECB()).decryptor()
        plain = decryptor.update(probe) + decryptor.finalize()
    except Exception:
        return False
    return _looks_like_image(plain)


def _candidates(block: bytes):
    for match in ASCII32_RE.finditer(block):
        yield match.group(1)[:16]
    for match in U16_RE.finditer(block):
        raw = match.group()
        yield raw[::2][:16]


def scan_image_aes_key(
    sample_dat: Path,
    *,
    timeout_seconds: float = 45.0,
    chunk_size: int = 1024 * 1024,
) -> bytes | None:
    """Read-only scan of likely Weixin private heap memory for the transient
    account image AES key.

    The main Weixin process is tried first. Only committed readable MEM_PRIVATE
    regions are considered, reads are chunked, and the scan has a hard time
    budget. Candidate strings are accepted only if decrypting a real V2 image
    ciphertext block produces a known image container signature.
    """
    probe = _probe_block(sample_dat)
    tested: set[bytes] = set()
    deadline = time.monotonic() + max(1.0, timeout_seconds)

    ProcessReader, list_weixin_processes = _windows_memory()
    processes = list_weixin_processes()
    # Child helper processes normally declare --type=...; the UI/main process
    # does not. Scan it first because transient account config lives there.
    processes.sort(
        key=lambda proc: (
            any(str(arg).startswith('--type=') for arg in (proc.get('cmdline') or [])),
            int(proc.get('pid') or 0),
        )
    )

    overlap = 128
    for proc in processes:
        if time.monotonic() >= deadline:
            return None
        pid = int(proc['pid'])
        try:
            reader = ProcessReader(pid)
        except OSError:
            continue
        try:
            for base, size in reader.private_regions(max_size=16 * 1024 * 1024):
                if time.monotonic() >= deadline:
                    return None
                offset = 0
                tail = b''
                while offset < size:
                    if time.monotonic() >= deadline:
                        return None
                    take = min(chunk_size, size - offset)
                    block = reader.read(base + offset, take)
                    if not block:
                        break
                    data = tail + block
                    for candidate in _candidates(data):
                        if candidate in tested:
                            continue
                        tested.add(candidate)
                        if _valid_aes_key(candidate, probe):
                            return candidate
                    tail = data[-overlap:] if len(data) > overlap else data
                    offset += len(block)
                    if len(block) < take:
                        break
        finally:
            reader.close()
    return None


def derive_xor_key(template_paths: list[Path]) -> int | None:
    """Infer the account-level XOR byte from cached JPEG thumbnail tails."""
    exact: Counter[int] = Counter()
    fallback: Counter[int] = Counter()
    for path in template_paths:
        try:
            with path.open('rb') as handle:
                handle.seek(0, 2)
                size = handle.tell()
                back = min(size, 2 + FOOTER_MAX)
                handle.seek(-back, 2)
                tail = handle.read()
        except OSError:
            continue
        if len(tail) >= 2:
            key = tail[-2] ^ 0xFF
            if tail[-1] ^ key == 0xD9:
                exact[key] += 1
        for index in range(max(0, len(tail) - 2 - FOOTER_MAX), len(tail) - 1):
            key = tail[index] ^ 0xFF
            if tail[index + 1] ^ key == 0xD9:
                fallback[key] += 1
    if exact:
        return exact.most_common(1)[0][0]
    if fallback:
        key, count = fallback.most_common(1)[0]
        return key if count >= 2 else None
    return None


def find_recent_thumbnail_templates(
    account_root: Path,
    keep: int = 24,
) -> list[Path]:
    attach = account_root / 'msg' / 'attach'
    if not attach.is_dir():
        return []
    latest: list[tuple[int, Path]] = []
    for path in attach.rglob('*_t.dat'):
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            continue
        latest.append((stamp, path))
    latest.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in latest[:keep]]


def strip_container_footer(plain: bytes) -> bytes:
    endings = (
        (b'\xff\xd8', b'\xff\xd9'),
        (b'\x89PNG\r\n\x1a\n', b'\x49\x45\x4e\x44\xae\x42\x60\x82'),
    )
    for start, end in endings:
        if not plain.startswith(start):
            continue
        if plain.endswith(end):
            return plain
        pos = plain.rfind(end)
        if pos > 0 and len(plain) - pos - len(end) <= FOOTER_MAX:
            return plain[: pos + len(end)]
    return plain


def decrypt_v2_bytes(data: bytes, aes_key: bytes, xor_key: int) -> bytes:
    if len(data) < 31 or data[:6] != V2_MAGIC:
        raise ValueError('not a WeChat V2 image container')
    aes_size, xor_size = struct.unpack_from('<LL', data, 6)
    aes_block = _aligned_aes_block_size(aes_size)
    offset = 15
    end_aes = offset + aes_block
    if end_aes > len(data) or xor_size > len(data) - end_aes:
        raise ValueError('invalid WeChat V2 image lengths')
    encrypted = data[offset:end_aes]
    raw = data[end_aes : len(data) - xor_size] if xor_size else data[end_aes:]
    xor_data = data[len(data) - xor_size :] if xor_size else b''
    decryptor = Cipher(algorithms.AES(aes_key), modes.ECB()).decryptor()
    first = decryptor.update(encrypted) + decryptor.finalize()
    if first:
        pad = first[-1]
        if 1 <= pad <= 16 and first[-pad:] == bytes([pad]) * pad:
            first = first[:-pad]
    plain = first + raw + bytes(value ^ xor_key for value in xor_data)
    return strip_container_footer(plain)


def image_extension(data: bytes) -> str:
    if data[:3] == b'\xff\xd8\xff':
        return 'jpg'
    if data[:4] == b'\x89PNG':
        return 'png'
    if data[:4] == b'GIF8':
        return 'gif'
    if data[:4] == b'RIFF':
        return 'webp'
    if data[:4] == b'wxgf':
        return 'wxgf'
    return 'bin'


def save_image_keys(path: Path, aes_key: bytes, xor_key: int) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'schema': 1,
        'aes_key_ascii': aes_key.decode('ascii'),
        'xor_key': int(xor_key),
    }
    if sys.platform == 'darwin':
        from .mac_key_storage import cached_secret
        cached_secret(path, 'image', payload=payload)
    else:
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.chmod(0o600)
        tmp.replace(path)
    return {
        'aes_fingerprint': hashlib.sha256(aes_key).hexdigest()[:16],
        'xor_fingerprint': hashlib.sha256(bytes([xor_key])).hexdigest()[:16],
    }


def load_image_keys(path: Path) -> tuple[bytes, int] | None:
    if sys.platform == 'darwin':
        from .mac_key_storage import cached_secret
        payload = cached_secret(path, 'image')
        if payload is None:
            return None
    else:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
    try:
        aes_key = str(payload['aes_key_ascii']).encode('ascii')
        xor_key = int(payload['xor_key'])
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        return None
    if len(aes_key) != 16 or not (0 <= xor_key <= 255):
        return None
    return aes_key, xor_key
