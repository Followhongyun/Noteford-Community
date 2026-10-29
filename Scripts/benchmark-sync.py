"""Synthetic WAL merge benchmark; run each revision in a fresh process.

PYTHONPATH=src python Scripts/benchmark-sync.py --database-mib 128 --frames 4096
No user data, keys, services or WeChat files are accessed.
"""
import argparse
import json
import resource
import struct
import tempfile
import time
import tracemalloc
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from noteford.crypto import PAGE_SIZE, RESERVE_SIZE, merge_encrypted_wal

parser = argparse.ArgumentParser()
parser.add_argument('--database-mib', type=int, default=128)
parser.add_argument('--frames', type=int, default=4096)
parser.add_argument('--unique-pages', action='store_true')
args = parser.parse_args()
key = bytes(range(32))
iv = bytes(range(16))
salt = b'12345678'
plain = b'\x0dNEW' + bytes(PAGE_SIZE - RESERVE_SIZE - 4)
encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
page = encryptor.update(plain) + encryptor.finalize() + iv + bytes(64)
with tempfile.TemporaryDirectory(prefix='noteford-bench-') as directory:
    db = Path(directory) / 'fixture.db'
    wal = Path(directory) / 'fixture.db-wal'
    with db.open('wb') as out:
        block = bytes(1024 * 1024)
        for _ in range(args.database_mib):
            out.write(block)
    with wal.open('wb') as out:
        out.write(struct.pack('>IIII', 0x377f0682, 3007000, PAGE_SIZE, 1) + salt + bytes(8))
        for i in range(args.frames):
            commit = args.database_mib * 256 if i == args.frames - 1 else 0
            number = i + 2 if args.unique_pages else 2
            if number > args.database_mib * 256:
                raise ValueError('frames exceed database pages')
            out.write(struct.pack('>II', number, commit) + salt + bytes(8) + page)
    before = resource.getrusage(resource.RUSAGE_SELF)
    tracemalloc.start()
    start = time.perf_counter()
    applied = merge_encrypted_wal(db, wal, key)
    elapsed = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    after = resource.getrusage(resource.RUSAGE_SELF)
    with db.open('rb') as handle:
        handle.seek(PAGE_SIZE)
        assert handle.read(4) == b'\x0dNEW'
    print(json.dumps(dict(database_mib=args.database_mib, wal_frames=args.frames,
        unique_pages=args.unique_pages,
        applied=applied, wall_seconds=elapsed,
        cpu_seconds=after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime,
        python_peak_bytes=peak, process_peak_rss_bytes=after.ru_maxrss,
        physical_read_bytes=None, physical_write_bytes=None,
        io_note='Physical bytes unavailable via macOS getrusage; cache and clone effects not inferred.')))
