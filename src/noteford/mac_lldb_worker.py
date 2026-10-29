#!/usr/bin/env python3
"""Standalone LLDB worker: launch a WeChat *copy* and capture SQLCipher keys.

Must run under Apple's /usr/bin/python3 with PYTHONPATH from `/usr/bin/lldb -P`.
Does not import noteford. Never prints secrets. Only kills the process it launches.

Hit handling uses the LLDB *event loop* (stop → read regs → Continue). We do
NOT use SetAutoContinue + SetScriptCallbackFunction: on Xcode LLDB those
callbacks were observed to never fire (zero probe_hit even for SecCodeCopySelf),
so auto-continue silently swallowed every breakpoint.

Static note (WeChat 4.1.15): the thin ``WeChat`` binary loads
``Contents/Resources/wechat.dylib`` (~358MB) which contains WCDB/SQLCipher
strings (``PRAGMA kdf_iter``, ``cipher_page_size``, ``InitCipherConfig``) and
**imports** ``_CCKeyDerivationPBKDF`` / ``_CCCryptorCreate`` / ``_CCHmac*``.
``libwxld.dylib`` only re-exports ``LdMain`` and imports ``CCCrypt`` + SecCode*;
it is NOT where SQLCipher lives. Login alone may not open message DBs — after
QR login, open chats / allow phone sync so codec init runs.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

MAC_SALT_XOR = 0x3A
PAGE_SIZE = 4096
HMAC_END = 4032
PRF_SHA512 = 5
ROUNDS_PASSPHRASE = 256000
ROUNDS_MAC = 2

# Keep list focused. Conditions cut UI AES noise. No SetAutoContinue.
CORE_BREAKPOINTS = (
    "CCKeyDerivationPBKDF",  # imported by wechat.dylib — real KDF when DBs open
    "ccpbkdf2_hmac",
    "ccpbkdf2_hmac_internal",
    "CCCryptorCreate",
    "CCCryptorCreateWithMode",
    "sqlite3_key",
    "sqlite3_key_v2",
    "PKCS5_PBKDF2_HMAC",
    "PKCS5_PBKDF2_HMAC_SHA1",
)

CONDITIONS = {
    "CCKeyDerivationPBKDF": "$x4 == 16 && ($x6 == 2 || $x6 == 256000)",
    "ccpbkdf2_hmac": "$x6 == 32",
    "ccpbkdf2_hmac_internal": "$x6 == 32",
    "CCCryptorCreate": "$x4 == 32",
    "CCCryptorCreateWithMode": "$x6 == 32",
    "sqlite3_key": "$x2 == 32 || $x2 == 48 || $x2 == -32 || $x2 == -48",
    "sqlite3_key_v2": "$x3 == 32 || $x3 == 48 || $x3 == -32 || $x3 == -48",
    "PKCS5_PBKDF2_HMAC": "$x6 == 32",
    "PKCS5_PBKDF2_HMAC_SHA1": "$x6 == 32",
}

IMAGE_HINTS = re.compile(
    r"(?i)(crypto|ssl|boringssl|openssl|sqlcipher|wcdb|commoncrypto|libcrypto|libssl|wxld|corecrypto|wechat\.dylib)"
)
DEEP_SCAN_IMAGES = re.compile(r"(?i)(wechat\.dylib|libwxld|libcrypto|libboringssl|libcorecrypto)")
SYMBOL_HINTS = re.compile(
    r"(?i)(pbkdf|sqlcipher|sqlite3_key|codec|derive|cipher.?key|InitCipher|"
    r"CCKeyDerivation|CCCrypt|ccpbkdf|wcdb|WCDB)"
)

MAX_PROBE_EVENTS = 500
MAX_HITS_PER_SYMBOL = 80

# Known InitCipherConfig string xrefs in wechat.dylib arm64 (file VA == offset for __TEXT at 0).
# From static analysis 2026-10-16; useful if name breakpoints stay silent after DB open.
WECHAT_DYLIB_INITCIPHER_XREFS = (
    0x5C72AB4,
    0x5C72BD8,
    0x5C72CC4,
    0x5C72DAC,
    0x5C72E3C,
    0x5C74738,
    0x5C7485C,
    0x5C74914,
    0x5C749C4,
    0x5C74A54,
)


def emit(kind: str, **items) -> None:
    print(json.dumps({"type": kind, **items}, ensure_ascii=False), flush=True)


def verified(key: bytes, page: bytes) -> bool:
    if len(key) != 32 or len(page) != PAGE_SIZE:
        return False
    mac_key = hashlib.pbkdf2_hmac(
        "sha512", key, bytes(b ^ MAC_SALT_XOR for b in page[:16]), ROUNDS_MAC, 32
    )
    actual = hmac.new(mac_key, page[16:HMAC_END] + b"\x01\x00\x00\x00", hashlib.sha512).digest()
    return hmac.compare_digest(actual, page[HMAC_END:PAGE_SIZE])


def private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_suffix(".new")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if tmp.exists():
            tmp.unlink()


def load_targets(path: Path) -> list[tuple[str, bytes]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for item in payload.get("databases") or []:
        rel = item.get("relative_path")
        page_hex = item.get("page1_hex")
        if not isinstance(rel, str) or not isinstance(page_hex, str):
            continue
        try:
            page = bytes.fromhex(page_hex)
        except ValueError:
            continue
        if len(page) == PAGE_SIZE and not page.startswith(b"SQLite format 3\x00"):
            rows.append((rel, page))
    return rows


def read_mem(process, addr: int, size: int) -> bytes:
    if addr == 0 or size <= 0 or size > 4096:
        return b""
    import lldb  # noqa: PLC0415

    err = lldb.SBError()
    blob = process.ReadMemory(addr, size, err)
    return bytes(blob) if err.Success() and blob is not None and len(blob) == size else b""


def try_accept_key(key, salt_hint, rows, found, source, out) -> int:
    if len(key) != 32:
        return 0
    newly = 0
    for rel, page in rows:
        if rel in found:
            continue
        if salt_hint is not None and page[:16] != salt_hint:
            continue
        if verified(key, page):
            found[rel] = {
                "relative_path": rel,
                "key_hex": key.hex(),
                "salt_hex": page[:16].hex(),
                "source": source,
            }
            newly += 1
    if newly:
        private_json(out, {"schema": 1, "keys": list(found.values())})
        emit(
            "verified_progress",
            verified_databases=len(found),
            total_databases=len(rows),
            source=source,
            added=newly,
        )
    return newly


def derive_enc_key(passphrase: bytes, salt: bytes) -> bytes:
    """Shared-passphrase model: enc_key = PBKDF2-HMAC-SHA512(passphrase, db_salt, 256000, 32).

    WeChat Mac 4.1.10+ keeps a single 32-byte passphrase in memory and derives
    every database's SQLCipher key from it with the per-file salt. Verified by two
    independent projects (wcdb-key-tool real-device 18/18; wechat-cli.com 4.1.13
    27/29). Pure function so it is unit-testable offline without LLDB.
    """

    return hashlib.pbkdf2_hmac("sha512", passphrase, salt, ROUNDS_PASSPHRASE, 32)


def derive_passphrase_keys(passphrase: bytes, rows) -> list:
    """From ONE captured login passphrase, derive+verify a key for EVERY database.

    Returns [(relative_path, enc_key, salt), ...] for databases whose page-1
    HMAC verifies. This is what lets a single login (no chat clicks) unlock all
    libraries — including message DBs the user never opened.
    """

    out = []
    if not (16 <= len(passphrase) <= 256):
        return out
    for rel, page in rows:
        salt = page[:16]
        key = derive_enc_key(passphrase, salt)
        if verified(key, page):
            out.append((rel, key, salt))
    return out


def try_passphrase(passphrase, rows, found, source, out) -> int:
    """Offline-derive all DB keys from a candidate shared passphrase; persist verified."""

    newly = 0
    for rel, key, salt in derive_passphrase_keys(passphrase, rows):
        if rel in found:
            continue
        found[rel] = {
            "relative_path": rel,
            "key_hex": key.hex(),
            "salt_hex": salt.hex(),
            "source": source,
        }
        newly += 1
    if newly:
        private_json(out, {"schema": 1, "keys": list(found.values())})
        emit(
            "passphrase_derived",
            verified_databases=len(found),
            total_databases=len(rows),
            source=source,
            added=newly,
        )
    return newly


def _module_path(module) -> str:
    spec = module.GetFileSpec()
    directory = spec.GetDirectory() or ""
    filename = spec.GetFilename() or ""
    if directory and filename:
        return f"{directory}/{filename}"
    return filename or directory or "?"


def frame_module_name(frame) -> str:
    module = frame.GetModule()
    if module is None or not module.IsValid():
        return "?"
    return module.GetFileSpec().GetFilename() or _module_path(module)


def enumerate_crypto_images(target) -> list[dict]:
    interesting = []
    for index in range(target.GetNumModules()):
        module = target.GetModuleAtIndex(index)
        path = _module_path(module)
        name = module.GetFileSpec().GetFilename() or ""
        if not IMAGE_HINTS.search(path) and not IMAGE_HINTS.search(name):
            continue
        interesting.append(
            {"index": index, "name": name, "path": path, "num_symbols": module.GetNumSymbols()}
        )
    return interesting


def scan_symbol_hints(target, limit: int = 120) -> list[dict]:
    hits: list[dict] = []
    deep_first: list = []
    others: list = []
    for index in range(target.GetNumModules()):
        module = target.GetModuleAtIndex(index)
        name = module.GetFileSpec().GetFilename() or ""
        path = _module_path(module)
        bucket = deep_first if DEEP_SCAN_IMAGES.search(name) or DEEP_SCAN_IMAGES.search(path) else others
        bucket.append(module)
    for module in deep_first + others:
        mod_name = module.GetFileSpec().GetFilename() or "?"
        for sym_i in range(module.GetNumSymbols()):
            symbol = module.GetSymbolAtIndex(sym_i)
            sym_name = symbol.GetName() or ""
            if not SYMBOL_HINTS.search(sym_name):
                continue
            hits.append({"module": mod_name, "symbol": sym_name})
            if len(hits) >= limit:
                return hits
    return hits


def install_breakpoints(target, names: tuple[str, ...]) -> dict[int, dict]:
    """Name breakpoints with conditions. Never SetAutoContinue / script callbacks."""

    mapping: dict[int, dict] = {}
    for name in names:
        bp = target.BreakpointCreateByName(name)
        if not bp.IsValid():
            emit("breakpoint_skipped", symbol=name, reason="invalid")
            continue
        bp.SetAutoContinue(False)
        cond = CONDITIONS.get(name)
        if cond:
            try:
                bp.SetCondition(cond)
            except Exception:
                pass
        info = {
            "symbol": name,
            "locations": bp.GetNumLocations(),
            "pending": bp.GetNumLocations() == 0,
            "condition": cond,
            "auto_continue": False,
            "handler": "event_loop",
        }
        mapping[bp.GetID()] = info
        emit(
            "breakpoint_set",
            symbol=name,
            breakpoint_id=bp.GetID(),
            locations=info["locations"],
            pending=info["pending"],
            condition=cond,
            auto_continue=False,
            handler="event_loop",
        )
    return mapping


def install_wechat_dylib_offset_breakpoints(target) -> dict[int, dict]:
    """Once wechat.dylib is loaded, add address breakpoints at InitCipherConfig xrefs."""

    added: dict[int, dict] = {}
    module = None
    for index in range(target.GetNumModules()):
        candidate = target.GetModuleAtIndex(index)
        name = candidate.GetFileSpec().GetFilename() or ""
        if name == "wechat.dylib":
            module = candidate
            break
    if module is None:
        emit("wechat_dylib_not_loaded_yet")
        return added
    base = module.GetObjectFileHeaderAddress().GetLoadAddress(target)
    if base == 0 or base == 0xFFFFFFFFFFFFFFFF:
        # Fallback: file address equals preferred VA for this binary's __TEXT at 0.
        base = module.GetObjectFileHeaderAddress().GetFileAddress()
    emit("wechat_dylib_loaded", base=hex(base) if isinstance(base, int) else str(base), path=_module_path(module))
    for offset in WECHAT_DYLIB_INITCIPHER_XREFS:
        # __TEXT vmaddr 0 → offset is both file and preferred VA.
        addr = offset if base in (0, 0xFFFFFFFFFFFFFFFF) else (base + offset)
        # Prefer slide-aware: SBAddress from module+offset
        try:
            import lldb  # noqa: PLC0415

            sb_addr = module.ResolveFileAddress(offset)
            load = sb_addr.GetLoadAddress(target)
            if load and load != 0xFFFFFFFFFFFFFFFF:
                addr = load
        except Exception:
            pass
        bp = target.BreakpointCreateByAddress(addr)
        if not bp.IsValid():
            continue
        bp.SetAutoContinue(False)
        info = {
            "symbol": f"wechat.dylib+InitCipherConfig_xref_{offset:#x}",
            "locations": bp.GetNumLocations(),
            "pending": False,
            "condition": None,
            "auto_continue": False,
            "handler": "event_loop",
            "offset": hex(offset),
        }
        added[bp.GetID()] = info
        emit(
            "breakpoint_set",
            symbol=info["symbol"],
            breakpoint_id=bp.GetID(),
            address=hex(addr),
            offset=hex(offset),
            handler="event_loop",
        )
    return added


def _reg(frame, name: str) -> int:
    return frame.FindRegister(name).GetValueAsUnsigned()


def handle_hit(symbol: str, frame, process, rows, salts, macsalts, found, out, counts) -> int:
    """Return newly verified key count. Never print secrets."""

    module = frame_module_name(frame)
    added = 0
    args: dict = {}

    if symbol == "CCKeyDerivationPBKDF" or "CCKeyDerivationPBKDF" in symbol:
        counts["pbkdf_calls"] = counts.get("pbkdf_calls", 0) + 1
        # arm64 CCKeyDerivationPBKDF(alg=x0, password=x1, passwordLen=x2,
        #   salt=x3, saltLen=x4, prf=x5, rounds=x6, derivedKey=x7, dkLen=[sp]).
        pw_ptr, pw_len = _reg(frame, "x1"), _reg(frame, "x2")
        salt_ptr, salt_len = _reg(frame, "x3"), _reg(frame, "x4")
        prf, rounds = _reg(frame, "x5"), _reg(frame, "x6")
        args = {
            "password_len": int(pw_len) if pw_len < 10_000 else -1,
            "salt_len": int(salt_len) if salt_len < 10_000 else -1,
            "prf": int(prf),
            "rounds": int(rounds) if rounds < 10_000_000 else -1,
        }
        # ★ Shared-passphrase model (wcdb-key-tool / wechat-cli.com, 4.1.10+):
        # the 256000-round SHA512 call carries the shared 32B passphrase as the
        # password INPUT (x1). Capture it ONCE, then offline-derive EVERY db's key
        # from that db's own salt. No chat clicks, no per-db output needed — one
        # login call unlocks all libraries (incl. message DBs never opened).
        if rounds == ROUNDS_PASSPHRASE and prf == PRF_SHA512 and 0 < pw_len <= 256:
            passphrase = read_mem(process, pw_ptr, pw_len)
            if len(passphrase) == pw_len:
                counts["passphrase_calls"] = counts.get("passphrase_calls", 0) + 1
                added += try_passphrase(
                    passphrase, rows, found, "CCKeyDerivationPBKDF-x1-passphrase", out
                )
        # Fallback (per-db raw-key model): the rounds==2 MAC derivation passes the
        # per-db enc_key as the password with salt = db_salt ^ 0x3a. Grab it direct.
        elif rounds == ROUNDS_MAC and salt_len == 16 and pw_len == 32:
            salt_arg = read_mem(process, salt_ptr, 16)
            file_salt = macsalts.get(salt_arg)
            key = read_mem(process, pw_ptr, 32)
            if len(key) == 32:
                if file_salt is not None:
                    counts["matched_salt_calls"] = counts.get("matched_salt_calls", 0) + 1
                added += try_accept_key(key, file_salt, rows, found, "CCKeyDerivationPBKDF-rounds2", out)
                if file_salt is None:
                    added += try_accept_key(key, None, rows, found, "CCKeyDerivationPBKDF-rounds2-scan", out)

    elif symbol.startswith("ccpbkdf2_hmac"):
        counts["pbkdf_calls"] = counts.get("pbkdf_calls", 0) + 1
        # arm64 ccpbkdf2_hmac(di=x0, passwordLen=x1, password=x2, saltLen=x3,
        #   salt=x4, iterations=x5, dkLen=x6, dk=x7). NOTE: this corecrypto
        #   register convention may differ across macOS builds -> verify on device.
        pw_len, pw_ptr = _reg(frame, "x1"), _reg(frame, "x2")
        salt_len, salt_ptr = _reg(frame, "x3"), _reg(frame, "x4")
        iterations, dk_len = _reg(frame, "x5"), _reg(frame, "x6")
        args = {
            "password_len": int(pw_len) if pw_len < 10_000 else -1,
            "salt_len": int(salt_len) if salt_len < 10_000 else -1,
            "iterations": int(iterations) if iterations < 10_000_000 else -1,
            "dk_len": int(dk_len) if dk_len < 10_000 else -1,
        }
        # Shared-passphrase model at the corecrypto layer: the 256000-round call's
        # password INPUT (x2) is the shared passphrase -> derive every db offline.
        if iterations == ROUNDS_PASSPHRASE and dk_len == 32 and 0 < pw_len <= 256:
            passphrase = read_mem(process, pw_ptr, pw_len)
            if len(passphrase) == pw_len:
                counts["passphrase_calls"] = counts.get("passphrase_calls", 0) + 1
                added += try_passphrase(passphrase, rows, found, f"{symbol}-x2-passphrase", out)
        # Fallback: rounds==2 MAC derivation -> x2 is the per-db enc_key directly.
        elif dk_len == 32 and salt_len == 16 and iterations == ROUNDS_MAC and pw_len == 32:
            salt_arg = read_mem(process, salt_ptr, 16)
            file_salt = macsalts.get(salt_arg)
            key = read_mem(process, pw_ptr, 32)
            if len(key) == 32:
                if file_salt is not None:
                    counts["matched_salt_calls"] = counts.get("matched_salt_calls", 0) + 1
                added += try_accept_key(key, file_salt, rows, found, f"{symbol}-entry-rounds2", out)
                if file_salt is None:
                    added += try_accept_key(key, None, rows, found, f"{symbol}-entry-scan", out)

    elif symbol in {"CCCryptorCreate", "CCCryptorCreateWithMode"} or "CCCryptorCreate" in symbol:
        if "WithMode" in symbol:
            key_ptr, key_len = _reg(frame, "x5"), _reg(frame, "x6")
            args = {"key_len": int(key_len) if key_len < 10_000 else -1, "alg": int(_reg(frame, "x2")), "mode": int(_reg(frame, "x1"))}
        else:
            key_ptr, key_len = _reg(frame, "x3"), _reg(frame, "x4")
            args = {"key_len": int(key_len) if key_len < 10_000 else -1, "alg": int(_reg(frame, "x1"))}
        if key_len == 32:
            key = read_mem(process, key_ptr, 32)
            if len(key) == 32:
                added += try_accept_key(key, None, rows, found, f"{symbol}-aes256", out)

    elif "sqlite3_key" in symbol:
        if "v2" in symbol:
            key_ptr, key_len = _reg(frame, "x2"), _reg(frame, "x3")
        else:
            key_ptr, key_len = _reg(frame, "x1"), _reg(frame, "x2")
        n = int(key_len)
        if n < 0:
            n = -n
        args = {"key_len": n if n < 10_000 else -1}
        if n in (32, 48):
            blob = read_mem(process, key_ptr, n)
            if len(blob) == n:
                key = blob[:32]
                salt_hint = blob[32:48] if n >= 48 else None
                added += try_accept_key(key, salt_hint, rows, found, f"{symbol}-len{n}", out)
                if salt_hint is None:
                    added += try_accept_key(key, None, rows, found, f"{symbol}-scan", out)

    elif "PKCS5_PBKDF2" in symbol:
        pw_ptr, pw_len = _reg(frame, "x0"), _reg(frame, "x1")
        salt_ptr, salt_len = _reg(frame, "x2"), _reg(frame, "x3")
        iterations, key_len = _reg(frame, "x4"), _reg(frame, "x6")
        args = {
            "password_len": int(pw_len) if pw_len < 10_000 else -1,
            "salt_len": int(salt_len) if salt_len < 10_000 else -1,
            "iterations": int(iterations) if iterations < 10_000_000 else -1,
            "key_len": int(key_len) if key_len < 10_000 else -1,
        }
        if key_len == 32 and salt_len == 16 and 0 < pw_len <= 256:
            salt_arg = read_mem(process, salt_ptr, 16)
            file_salt = salt_arg if salt_arg in salts else macsalts.get(salt_arg)
            secret = read_mem(process, pw_ptr, pw_len)
            if file_salt is not None and len(secret) == pw_len:
                counts["matched_salt_calls"] = counts.get("matched_salt_calls", 0) + 1
                for hash_name in ("sha512", "sha256", "sha1"):
                    key = hashlib.pbkdf2_hmac(hash_name, secret, file_salt, iterations, 32)
                    added += try_accept_key(key, file_salt, rows, found, f"{symbol}-{hash_name}", out)

    elif "InitCipherConfig" in symbol:
        # Offset probe: log only; OpenSSL InitCipherConfig is not the SQLCipher key itself.
        args = {f"x{i}": (_reg(frame, f"x{i}") if _reg(frame, f"x{i}") < 10_000 else "ptr") for i in range(0, 8)}

    else:
        args = {f"x{i}": (_reg(frame, f"x{i}") if _reg(frame, f"x{i}") < 10_000 else "ptr") for i in range(0, 8)}

    return added, module, args


def run_session(exe: Path, rows: list[tuple[str, bytes]], out: Path, timeout: int) -> int:
    import lldb  # noqa: PLC0415

    salts = {page[:16] for _, page in rows}
    macsalts = {bytes(b ^ MAC_SALT_XOR for b in salt): salt for salt in salts}
    found: dict[str, dict] = {}
    counts = {"pbkdf_calls": 0, "passphrase_calls": 0, "matched_salt_calls": 0, "probe_hits": 0, "probe_events_emitted": 0}
    hit_by_symbol: dict[str, int] = {}
    os.umask(0o077)

    debugger = lldb.SBDebugger.Create()
    debugger.SetAsync(True)
    debugger.SkipLLDBInitFiles(True)
    listener = debugger.GetListener()
    target = debugger.CreateTarget(str(exe))
    if not target.IsValid():
        emit("capture_error", error="invalid_launch_target")
        return 2
    if not target.GetTriple().startswith("arm64"):
        emit("capture_error", error="arm64_only")
        return 2

    bp_map = install_breakpoints(target, CORE_BREAKPOINTS)
    if not bp_map:
        emit("capture_error", error="no_breakpoints_installed")
        return 2

    launch = lldb.SBLaunchInfo([])
    launch.AddSuppressFileAction(0, True, False)
    launch.AddSuppressFileAction(1, False, True)
    launch.AddSuppressFileAction(2, False, True)
    launch.SetWorkingDirectory(str(exe.parent))
    err = lldb.SBError()
    process = None
    modules_scanned = False
    offset_bps_installed = False
    try:
        process = target.Launch(launch, err)
        if err.Fail() or process is None or not process.IsValid():
            emit("launch_failed", error=err.GetCString() or "unknown")
            return 2
        emit(
            "launched",
            pid=process.GetProcessID(),
            database_count=len(rows),
            breakpoint_count=len(bp_map),
            breakpoint_symbols=[m["symbol"] for m in bp_map.values()],
            strategy="event_loop_stop_read_continue",
            instruction=(
                "扫码登录后请点开几个聊天/允许手机同步历史，以便 wechat.dylib 真正开库；"
                "断点用事件循环处理（已废弃不可靠的 SetAutoContinue 脚本回调）。"
            ),
        )
        deadline = time.monotonic() + min(max(timeout, 10), 300)
        last_update = time.monotonic()
        next_module_scan = time.monotonic() + 1.5

        while time.monotonic() < deadline and (not rows or len(found) < len(rows)):
            event = lldb.SBEvent()
            if not listener.WaitForEvent(1, event):
                now = time.monotonic()
                if not modules_scanned and now >= next_module_scan and process.IsValid():
                    images = enumerate_crypto_images(target)
                    emit("crypto_images", count=len(images), images=images[:50])
                    hinted = scan_symbol_hints(target)
                    emit("symbol_hints", count=len(hinted), samples=hinted[:60])
                    resolved = []
                    for bp_id, meta in list(bp_map.items()):
                        bp = target.FindBreakpointByID(bp_id)
                        if bp.IsValid():
                            meta["locations"] = bp.GetNumLocations()
                            meta["pending"] = bp.GetNumLocations() == 0
                            resolved.append(
                                {
                                    "symbol": meta["symbol"],
                                    "locations": meta["locations"],
                                    "pending": meta["pending"],
                                    "condition": meta.get("condition"),
                                }
                            )
                    emit("breakpoints_resolved", breakpoints=resolved)
                    modules_scanned = True
                if not offset_bps_installed and process.IsValid():
                    extra = install_wechat_dylib_offset_breakpoints(target)
                    if extra:
                        bp_map.update(extra)
                        offset_bps_installed = True
                if now - last_update > 15:
                    emit(
                        "waiting",
                        verified_databases=len(found),
                        hit_by_symbol=dict(hit_by_symbol),
                        hint="若仍无 hit：登录后点开聊天触发开库；确认 wechat.dylib 已加载",
                        **counts,
                    )
                    last_update = now
                continue

            if not lldb.SBProcess.EventIsProcessEvent(event):
                continue
            state = lldb.SBProcess.GetStateFromEvent(event)
            if state in (lldb.eStateExited, lldb.eStateCrashed, lldb.eStateDetached):
                emit(
                    "process_ended",
                    state=int(state),
                    exit_status=process.GetExitStatus(),
                    hit_by_symbol=dict(hit_by_symbol),
                    verified_databases=len(found),
                )
                break
            if state != lldb.eStateStopped:
                continue

            for thread in process:
                if thread.GetStopReason() != lldb.eStopReasonBreakpoint:
                    continue
                if thread.GetStopReasonDataCount() < 1:
                    continue
                bp_id = thread.GetStopReasonDataAtIndex(0)
                meta = bp_map.get(bp_id)
                if meta is None:
                    continue
                symbol = meta["symbol"]
                hit_by_symbol[symbol] = hit_by_symbol.get(symbol, 0) + 1
                counts["probe_hits"] += 1
                if hit_by_symbol[symbol] > MAX_HITS_PER_SYMBOL:
                    continue
                frame = thread.GetFrameAtIndex(0)
                try:
                    added, module, args = handle_hit(
                        symbol, frame, process, rows, salts, macsalts, found, out, counts
                    )
                except Exception as exc:  # noqa: BLE001
                    added, module, args = 0, "?", {"probe_error": type(exc).__name__}
                if counts["probe_events_emitted"] < MAX_PROBE_EVENTS:
                    emit(
                        "probe_hit",
                        symbol=symbol,
                        module=module,
                        hit_index=hit_by_symbol[symbol],
                        breakpoint_id=bp_id,
                        args=args,
                        keys_verified_after=len(found),
                    )
                    counts["probe_events_emitted"] += 1
                if added:
                    emit(
                        "verified_progress",
                        verified_databases=len(found),
                        total_databases=len(rows),
                        hit_by_symbol=dict(hit_by_symbol),
                        **{k: counts[k] for k in ("pbkdf_calls", "matched_salt_calls", "probe_hits")},
                    )

            cont = process.Continue()
            if cont.Fail():
                emit("capture_error", error="could_not_resume")
                return 2
            if rows and len(found) == len(rows):
                break

        emit(
            "capture_finished",
            verified_databases=len(found),
            total_databases=len(rows),
            hit_by_symbol=dict(hit_by_symbol),
            **counts,
        )
        # Self-test / probe success: either keys found, or at least one probe_hit recorded.
        if found:
            return 0
        if counts.get("probe_hits", 0) > 0 and not rows:
            return 0
        return 3
    finally:
        if process is not None and process.IsValid():
            state = process.GetState()
            if state not in (lldb.eStateExited, lldb.eStateDetached, lldb.eStateInvalid):
                process.Kill()
                emit("owned_process_closed", pid=process.GetProcessID())
        lldb.SBDebugger.Destroy(debugger)



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path)
    parser.add_argument("--targets", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Compile native/macos/lldb_probe/probe_target.c and verify event-loop probe_hit works",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        # Dedicated self-test path with inline verification of emitted events.
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="noteford-lldb-selftest-") as raw:
            work = Path(raw)
            src = Path(__file__).resolve().parents[2] / "native" / "macos" / "lldb_probe" / "probe_target.c"
            exe = work / "probe_target"
            completed = subprocess.run(
                ["/usr/bin/clang", "-arch", "arm64", "-O0", "-g", str(src), "-o", str(exe)],
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0 or not exe.is_file():
                emit("self_test_compile_failed", stderr=(completed.stderr or "")[-800:])
                return 2
            emit("self_test_compiled", exe=str(exe))
            out = work / "keys-should-not-matter.json"
            with redirect_stdout(buf):
                run_session(exe, rows=[], out=out, timeout=min(max(args.timeout, 5), 60))
            text = buf.getvalue()
            # Re-emit captured events to real stdout for the log file.
            sys.stdout.write(text)
            sys.stdout.flush()
            hits = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
            probe_hits = [e for e in hits if e.get("type") == "probe_hit"]
            cckey_hits = [e for e in probe_hits if "CCKeyDerivationPBKDF" in str(e.get("symbol"))]
            emit(
                "self_test_result",
                probe_hits=len(probe_hits),
                cckey_hits=len(cckey_hits),
                ok=len(cckey_hits) > 0,
            )
            return 0 if cckey_hits else 3

    if args.exe is None or args.targets is None or args.out is None:
        parser.error("--exe/--targets/--out required unless --self-test")
    if args.out.exists():
        emit("capture_error", error="output_exists")
        return 2
    exe = args.exe.resolve(strict=True)
    rows = load_targets(args.targets.resolve(strict=True))
    if not rows:
        emit("capture_error", error="no_encrypted_databases")
        return 2
    return run_session(exe, rows, args.out, args.timeout)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        emit("capture_error", error=str(exc))
        raise SystemExit(2) from exc
