"""Mac synthetic encrypted SQLite snapshot benchmark, with a real quick_check.

Uses system SQLite's SQLITE_FCNTL_RESERVE_BYTES (38) to create WCDB-sized
reserved pages. All source writes are confined to a temporary fixture.
"""
import argparse
import ctypes
import ctypes.util
import hashlib
import hmac
import inspect
import json
import resource
import sqlite3
import struct
import tempfile
import time
import tracemalloc
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from noteford import mac_live_sync as live

parser = argparse.ArgumentParser()
parser.add_argument('--database-mib', type=int, default=32)
args = parser.parse_args()
key, iv, salt = bytes(range(32)), bytes(range(16)), b'fixture-salt-123'
salt = salt.ljust(16, b'0')
mac_key = hashlib.pbkdf2_hmac('sha512', key, bytes(v ^ 0x3a for v in salt), 2, dklen=32)


def encrypt(page, number):
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    body = page[16:4016] if number == 1 else page[:4016]
    data = cipher.update(body) + cipher.finalize()
    encoded = (salt if number == 1 else b'') + data + iv
    digest = hmac.new(mac_key, encoded[16:] + struct.pack('<I', 1), hashlib.sha512).digest() if number == 1 else bytes(64)
    return encoded + digest


def create(path):
    lib = ctypes.CDLL(ctypes.util.find_library('sqlite3'))
    lib.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    lib.sqlite3_file_control.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    lib.sqlite3_exec.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    lib.sqlite3_close.argtypes = [ctypes.c_void_p]
    db = ctypes.c_void_p()
    assert lib.sqlite3_open(bytes(str(path), 'utf-8'), ctypes.byref(db)) == 0
    try:
        reserve = ctypes.c_int(80)
        assert lib.sqlite3_file_control(db, b'main', 38, ctypes.byref(reserve)) == 0
        sql = f'''PRAGMA page_size=4096;
            CREATE TABLE messages(id INTEGER PRIMARY KEY, body TEXT);
            INSERT INTO messages VALUES(1,'old0');
            CREATE TABLE padding(body BLOB);
            INSERT INTO padding VALUES(zeroblob({args.database_mib * 1024 * 1024}));'''
        assert lib.sqlite3_exec(db, sql.encode(), None, None, None) == 0
    finally:
        lib.sqlite3_close(db)


with tempfile.TemporaryDirectory(prefix='noteford-source-bench-') as directory:
    root = Path(directory)
    plain, source = root/'fixture-plain', root/'message_0.db'
    create(plain)
    with plain.open('rb') as inp, source.open('wb') as out:
        number = 1
        while page := inp.read(4096):
            assert page[20] == 80 if number == 1 else True
            out.write(encrypt(page, number))
            number += 1
    calls = []
    decrypt = live.decrypt_database
    def counted(*a):
        pages = decrypt(*a)
        calls.append(pages)
        return pages
    live.decrypt_database = counted
    copied_source_bytes = []
    copyfile = live.shutil.copyfile
    def counted_copy(src, dst, *a, **kw):
        if Path(src) in (source, Path(str(source)+'-wal')):
            copied_source_bytes.append(Path(src).stat().st_size)
        return copyfile(src, dst, *a, **kw)
    live.shutil.copyfile = counted_copy
    previous = stamp = None
    results = []
    for scenario in ('initial', 'wal-only', 'wal-repeat'):
        if scenario != 'initial':
            with sqlite3.connect(plain) as con:
                con.execute('UPDATE messages SET body=?', ('new1' if scenario == 'wal-only' else 'new2',))
            with plain.open('rb') as handle:
                handle.seek(4096)
                page = encrypt(handle.read(4096), 2)
            wal = Path(str(source)+'-wal')
            wal.write_bytes(struct.pack('>IIII', 0x377f0682, 3007000, 4096, 1)
                + b'12345678' + bytes(8) + struct.pack('>II', 2, number-1) + b'12345678' + bytes(8) + page)
        kwargs = {}
        if previous and 'previous_base' in inspect.signature(live._publish_snapshot).parameters:
            kwargs = dict(previous_base=previous.parent/'.source-base', previous_stamp=stamp)
        calls.clear()
        copied_source_bytes.clear()
        tracemalloc.start()
        usage = resource.getrusage(resource.RUSAGE_SELF)
        start = time.perf_counter()
        previous, stamp, frames = live._publish_snapshot(source, root/'snapshots', key, **kwargs)
        elapsed = time.perf_counter() - start
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        end = resource.getrusage(resource.RUSAGE_SELF)
        with sqlite3.connect(previous) as con:
            value = con.execute('SELECT body FROM messages').fetchone()[0]
        assert value == {'initial':'old0','wal-only':'new1','wal-repeat':'new2'}[scenario]
        results.append(dict(scenario=scenario, database_bytes=source.stat().st_size,
            wall_seconds=elapsed, cpu_seconds=end.ru_utime+end.ru_stime-usage.ru_utime-usage.ru_stime,
            python_peak_bytes=peak, process_peak_rss_bytes=end.ru_maxrss,
            decrypted_pages=sum(calls), applied_pages=frames,
            logical_source_copy_bytes=sum(copied_source_bytes),
            physical_read_bytes=None, physical_write_bytes=None))
    print(json.dumps(results))
