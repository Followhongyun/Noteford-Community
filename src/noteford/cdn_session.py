"""Read-only, account-bound recovery of the current desktop CDN session.

Offsets describe the verified Windows MMKV layout, not a stable public API.
Unsupported layouts fail closed. Credentials are used in memory only.
"""
from pathlib import Path
import re
import struct
import time
import zlib

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


# Names stay on the module so tests can replace them. They are filled from
# winmem on first use; importing this module does not import winmem.
ProcessReader = None
list_weixin_processes = None


def _windows_memory():
    from .winmem import bind_names

    return bind_names(globals())


def _varint(data, offset):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError('session_unavailable')
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError('session_unavailable')


def _parse_mmkv(data):
    _, offset = _varint(data, 0)
    values = {}
    while offset < len(data):
        length, offset = _varint(data, offset)
        if not 0 < length <= 512 or offset+length > len(data):
            raise ValueError('session_unavailable')
        key = data[offset:offset+length].decode('utf8')
        offset += length
        if not key.isprintable():
            raise ValueError('session_unavailable')
        length, offset = _varint(data, offset)
        if length > 1024*1024 or offset+length > len(data):
            raise ValueError('session_unavailable')
        values[key] = data[offset:offset+length]
        offset += length
    return values


def _read_crc(path):
    crc_path = Path(str(path)+'.crc')
    if not 32 <= crc_path.stat().st_size <= 4096:
        raise ValueError('session_unavailable')
    return crc_path.read_bytes()


def read_target(path):
    if not 4 <= path.stat().st_size <= 1024*1024:
        raise ValueError('session_unavailable')
    raw = path.read_bytes()
    meta = _read_crc(path)
    size = struct.unpack_from('<I', meta, 28)[0]
    if not 0 < size <= len(raw)-4:
        raise ValueError('session_unavailable')
    encrypted, iv = raw[4:4+size], meta[12:28]
    if zlib.crc32(encrypted) != struct.unpack_from('<I', meta)[0]:
        raise ValueError('session_unavailable')
    target = str(path).replace('\\', '/').lower()
    name = path.name.encode()
    deadline = time.monotonic()+15
    ProcessReader, list_weixin_processes = _windows_memory()
    for process in list_weixin_processes():
        if any(str(a).startswith('--type=') for a in process.get('cmdline') or []):
            continue
        with ProcessReader(process['pid']) as reader:
            for base, size in reader.private_regions(max_size=128*1024*1024):
                for offset in range(0, size, 1024*1024):
                    if time.monotonic() > deadline:
                        raise ValueError('session_unavailable')
                    data = reader.read(base+offset, min(1024*1024+4096, size-offset)) or b''
                    pos = 0
                    while (pos := data.find(name+b'\0', pos)) >= 0:
                        candidate = reader.read(base+offset+pos, 384) or b''
                        pos += len(name)
                        if len(candidate) < 384 or struct.unpack_from('<QQ', candidate, 16) != (len(name), 15):
                            continue
                        pointer, length, capacity = struct.unpack_from('<Q8xQQ', candidate, 40)
                        if not 30 < length < 512 or not length <= capacity < 1024:
                            continue
                        actual_path = reader.read(pointer, length*2) or b''
                        actual_path = actual_path.decode('utf-16le', errors='replace').replace('\\', '/').lower()
                        if actual_path != target:
                            continue
                        for crypt_offset in (168, 176, 184):
                            pointer = struct.unpack_from('<Q', candidate, crypt_offset)[0]
                            if not 0x10000 < pointer < 0x7fffffffffff:
                                continue
                            crypt = reader.read(pointer, 80) or b''
                            for key_offset in (16, 24):
                                key = crypt[key_offset:key_offset+16]
                                if len(key) != 16:
                                    continue
                                decryptor = Cipher(algorithms.AES(key), modes.CFB(iv)).decryptor()
                                plain = decryptor.update(encrypted)+decryptor.finalize()
                                try:
                                    values = _parse_mmkv(plain)
                                except (ValueError, UnicodeError):
                                    continue
                                if len(values) > 3:
                                    return values
    raise ValueError('session_unavailable')


def acquire_session(wechat_root, account):
    """Never log, persist or expose the returned request fields through the API."""
    from .libraries import account_username
    from .cdn_download import unpack_fields
    path = Path(wechat_root) / 'all_users/config/global_config'
    stamp = _read_crc(path)
    values = read_target(path)
    raw = values.get('mmkv_key_latest_login_username', b'')
    length, offset = _varint(raw, 0)
    if offset+length != len(raw) or raw[offset:offset+length].decode() != account_username(account):
        raise ValueError('account_mismatch')
    uin, end = _varint(values.get('mmkv_key_latest_login_uin', b''), 0)
    if not uin or end != len(values['mmkv_key_latest_login_uin']):
        raise ValueError('session_unavailable')
    deadline = time.monotonic()+15
    ProcessReader, list_weixin_processes = _windows_memory()
    for process in list_weixin_processes():
        if any(str(a).startswith('--type=') for a in process.get('cmdline') or []):
            continue
        with ProcessReader(process['pid']) as reader:
            for base, size in reader.private_regions(max_size=128*1024*1024):
                for offset in range(0, size, 1024*1024):
                    if time.monotonic() > deadline:
                        raise ValueError('session_unavailable')
                    data = reader.read(base+offset, min(1024*1024+65536, size-offset)) or b''
                    pos = 0
                    while (pos := data.find(b'\0\0\0\x03ver', pos)) >= 0:
                        fields = unpack_fields(data[pos:pos+65536], strict=False)
                        pos += 8
                        # sourceflag is optional in current desktop request layouts.
                        # Account identity and credential structure remain mandatory.
                        if (fields.get('weixinnum') == str(uin).encode()
                                and 32 <= len(fields.get('authkey', b'')) <= 256
                                and len(fields.get('rsavalue', b'')) == 128
                                and re.fullmatch(rb'[0-9]{1,10}', fields.get('seq', b''))
                                and re.fullmatch(rb'[0-9a-fA-F]{32,4096}', fields.get('fileid', b''))):
                            if _read_crc(path) != stamp:
                                raise ValueError('account_mismatch')
                            return fields
    raise ValueError('session_unavailable')
