from __future__ import annotations

import ctypes
import hashlib
import json
import re
import struct
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil

CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
HEX_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_GUARD = 0x100
READABLE_MASK = 0xE6

# kernel32 is bound on first Windows use. Importing this module on macOS or
# Linux must not touch WinDLL; the scan entry points raise UnsupportedPlatform.
_k32 = None
_MBI = None
_bind_lock = threading.Lock()


class UnsupportedPlatform(OSError):
    """Windows process-memory access was requested on a non-Windows host."""


def _require_windows() -> None:
    if sys.platform != "win32":
        raise UnsupportedPlatform(
            "Windows memory scan is only available on Windows (winmem). "
            "macOS database keys come from keys.json / the interpose pipeline "
            "via platform_seam.macos_database_key_source."
        )


def _ensure_windows() -> None:
    """Bind kernel32 once, with the same signatures the module used to set at import."""

    global _k32, _MBI
    _require_windows()
    if _k32 is not None:
        return
    with _bind_lock:
        if _k32 is not None:
            return
        from ctypes import wintypes

        class MBI(ctypes.Structure):
            _fields_ = [
                ("BaseAddress", ctypes.c_void_p),
                ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD),
                ("_alignment1", wintypes.DWORD),
                ("RegionSize", ctypes.c_size_t),
                ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("_alignment2", wintypes.DWORD),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.VirtualQueryEx.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t
        ]
        kernel.VirtualQueryEx.restype = ctypes.c_size_t
        kernel.ReadProcessMemory.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        _MBI = MBI
        _k32 = kernel


def bind_names(namespace: dict):
    """Return process-memory callables, keeping names already patched on ``namespace``.

    Shared modules call this at use time so importing them does not import the
    Windows binding, while tests can still replace ``ProcessReader`` and
    ``list_weixin_processes`` on the caller module.
    """

    reader = namespace.get("ProcessReader")
    listed = namespace.get("list_weixin_processes")
    if reader is None:
        reader = ProcessReader
        namespace["ProcessReader"] = reader
    if listed is None:
        listed = list_weixin_processes
        namespace["list_weixin_processes"] = listed
    return reader, listed


@dataclass(frozen=True)
class Candidate:
    pid: int
    key_hex: str
    salt_hex: str | None

    @property
    def fingerprint(self) -> str:
        raw = bytes.fromhex(self.key_hex)
        return hashlib.sha256(raw).hexdigest()[:16]


def list_weixin_processes() -> list[dict]:
    _require_windows()
    out: list[dict] = []
    for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            if (proc.info.get("name") or "").lower() == "weixin.exe":
                out.append(proc.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def _probable_key(value: bytes) -> bool:
    return (
        len(value) == 32
        and len(set(value)) >= 15
        and value not in {b"\x00" * 32, b"\xff" * 32}
    )


class ProcessReader:
    def __init__(self, pid: int):
        _ensure_windows()
        self.pid = pid
        self.handle = _k32.OpenProcess(
            PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid
        )
        if not self.handle:
            raise OSError(ctypes.get_last_error(), f"OpenProcess failed for PID {pid}")

    def close(self) -> None:
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self) -> "ProcessReader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def read(self, address: int, size: int) -> bytes | None:
        if size <= 0:
            return b""
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t(0)
        ok = _k32.ReadProcessMemory(
            self.handle,
            ctypes.c_void_p(address),
            buf,
            size,
            ctypes.byref(got),
        )
        if not ok or got.value == 0:
            return None
        return buf.raw[: got.value]

    def region_infos(self):
        address = 0
        while True:
            mbi = _MBI()
            result = _k32.VirtualQueryEx(
                self.handle,
                ctypes.c_void_p(address),
                ctypes.byref(mbi),
                ctypes.sizeof(mbi),
            )
            if result == 0:
                return
            base = int(mbi.BaseAddress or 0)
            size = int(mbi.RegionSize)
            if size <= 0:
                return
            if (
                mbi.State == MEM_COMMIT
                and ((mbi.Protect & 0xFF) & READABLE_MASK)
                and not (mbi.Protect & PAGE_GUARD)
                and size < 0x10000000
            ):
                yield base, size, int(mbi.Type), int(mbi.Protect)
            nxt = base + size
            if nxt <= address:
                return
            address = nxt

    def regions(self):
        for base, size, _type, _protect in self.region_infos():
            yield base, size

    def private_regions(self, max_size: int = 32 * 1024 * 1024):
        for base, size, region_type, _protect in self.region_infos():
            if region_type == MEM_PRIVATE and 0 < size <= max_size:
                yield base, size

    def find_bytes(self, needle: bytes, chunk_size: int = 2 * 1024 * 1024) -> list[int]:
        hits: list[int] = []
        overlap = max(0, len(needle) - 1)
        for base, size in self.regions():
            offset = 0
            tail = b""
            while offset < size:
                take = min(chunk_size, size - offset)
                block = self.read(base + offset, take)
                if not block:
                    break
                data = tail + block
                data_base = base + offset - len(tail)
                pos = 0
                while True:
                    pos = data.find(needle, pos)
                    if pos < 0:
                        break
                    hits.append(data_base + pos)
                    pos += 1
                tail = data[-overlap:] if overlap else b""
                offset += len(block)
                if len(block) < take:
                    break
        return hits


def _scan_pid(pid: int) -> list[Candidate]:
    candidates: dict[tuple[str, str | None], Candidate] = {}
    with ProcessReader(pid) as reader:
        names = reader.find_bytes(CONFIG_CIPHER_NAME)
        if not names:
            return []

        pairs = [
            struct.pack("<Q", address) + struct.pack("<Q", len(CONFIG_CIPHER_NAME))
            for address in names
        ]
        for pair in pairs:
            for qaddr in reader.find_bytes(pair):
                node = reader.read(qaddr - 0x10, 0x50)
                if not node or len(node) < 0x40:
                    continue
                if struct.unpack_from("<Q", node, 0x10)[0] not in names:
                    continue
                if struct.unpack_from("<Q", node, 0x18)[0] != len(CONFIG_CIPHER_NAME):
                    continue

                config_ptr = struct.unpack_from("<Q", node, 0x28)[0]
                if not (0x10000 <= config_ptr < 0x800000000000):
                    continue

                obj = reader.read(config_ptr + 0x88, 0x28)
                if not obj or len(obj) < 0x18:
                    continue

                data_ptr = struct.unpack_from("<Q", obj, 0x8)[0]
                data_len = struct.unpack_from("<Q", obj, 0x10)[0]
                if not (0 < data_len <= 1024 and 0x10000 <= data_ptr < 0x800000000000):
                    continue

                blob = reader.read(data_ptr, int(data_len))
                if not blob or len(blob) != data_len:
                    continue

                decoded = bytes(
                    value ^ CONFIG_XOR_MASK[index % len(CONFIG_XOR_MASK)]
                    for index, value in enumerate(blob)
                )
                for match in HEX_LITERAL_RE.finditer(decoded):
                    run = match.group(1).decode("ascii").lower()
                    starts = [0]
                    if len(run) > 96:
                        starts.extend(range(0, len(run) - 63, 32))
                        starts.append(len(run) - 64)

                    for start in dict.fromkeys(starts):
                        if start + 64 > len(run):
                            continue
                        raw = bytes.fromhex(run[start : start + 64])
                        if not _probable_key(raw):
                            continue
                        salt_hex = None
                        if start + 96 <= len(run):
                            salt_hex = run[start + 64 : start + 96]
                        item = Candidate(pid=pid, key_hex=raw.hex(), salt_hex=salt_hex)
                        candidates[(item.key_hex, item.salt_hex)] = item
    return list(candidates.values())


def scan_config_cipher_candidates() -> list[Candidate]:
    _require_windows()
    found: dict[tuple[str, str | None], Candidate] = {}
    for proc in list_weixin_processes():
        pid = int(proc["pid"])
        try:
            for item in _scan_pid(pid):
                found[(item.key_hex, item.salt_hex)] = item
        except OSError:
            continue
    return list(found.values())


def save_candidates(path: Path, candidates: list[Candidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "source": "read-only Weixin.exe Config.Cipher memory scan",
        "candidates": [asdict(item) | {"fingerprint": item.fingerprint} for item in candidates],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
