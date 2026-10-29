#!/usr/bin/env python3
"""Read-only macOS WeChat layout probe.

Writes a report outside the WeChat directories. Does not decrypt databases,
extract keys, re-sign WeChat, or change SIP. On a successful task_for_pid it
counts memory regions and releases the task port without logging their bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from classify import (
    PAGE_SIZE,
    classify_database_header,
    classify_dat_header,
    classify_wal_header,
)

SCHEMA = 1
MAX_ACCOUNTS = 32
MAX_DB_FILES = 80
MAX_DAT_SAMPLES = 24
MAX_WAL_FILES = 40
MAX_NAMED_DIRS = 40
MAX_WALK_DIRECTORIES = 4000
HEADER_BYTES = 4096
DAT_HEADER_BYTES = 16
INTERESTING_DIR_NAMES = {"kvcomm", "msg", "attach", "db_storage", "message", "session", "contact"}


def _script_sha256() -> str:
    blob = Path(__file__).read_bytes() + Path(__file__).with_name("classify.py").read_bytes()
    return hashlib.sha256(blob).hexdigest()


def _read_prefix(path: Path, count: int) -> tuple[bytes, int] | dict:
    try:
        info = path.lstat()
    except OSError as exc:
        return {"error": _os_error(exc)}
    if stat.S_ISLNK(info.st_mode):
        return {"error": "symlink_skipped"}
    try:
        size = info.st_size
        with path.open("rb") as handle:
            return handle.read(count), size
    except OSError as exc:
        return {"error": _os_error(exc)}


def _os_error(exc: OSError) -> str:
    if isinstance(exc, PermissionError):
        return "permission_denied"
    return exc.errno and f"errno_{exc.errno}" or "os_error"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def default_search_roots(home: Path) -> list[Path]:
    container = home / "Library" / "Containers" / "com.tencent.xinWeChat" / "Data"
    return [
        container / "Documents" / "app_data" / "xwechat_files",
        container / "Documents" / "xwechat_files",
        home / "Documents" / "xwechat_files",
        container / "Library" / "Application Support" / "com.tencent.xinWeChat",
        home / "Library" / "Containers" / "com.tencent.xinWeChat",
        home / "Library" / "Application Support" / "com.tencent.xinWeChat",
        home / "Library" / "Group Containers",
    ]


def _child_dirs(path: Path) -> list[Path] | dict:
    try:
        children = []
        for child in sorted(path.iterdir(), key=lambda item: item.name):
            try:
                info = child.lstat()
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                children.append(child)
        return children
    except OSError as exc:
        return {"error": _os_error(exc)}


def _account_marker(account: Path) -> str | None:
    storage = account / "db_storage"
    session = storage / "session" / "session.db"
    contact = storage / "contact" / "contact.db"
    try:
        if session.is_file() and contact.is_file() and not session.is_symlink() and not contact.is_symlink():
            return "wechat4_db_storage"
    except OSError:
        return None
    message = account / "Message"
    try:
        if message.is_dir() and not message.is_symlink() and any(message.glob("*.db")):
            return "legacy_message_dir"
    except OSError:
        return None
    return None


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _inspect_databases(account: Path) -> dict:
    found = []
    truncated = False
    storage_dirs = []
    marker = _account_marker(account)
    if marker == "wechat4_db_storage":
        storage_dirs.append(account / "db_storage")
    elif marker == "legacy_message_dir":
        storage_dirs.append(account / "Message")
    for storage in storage_dirs:
        for dirpath, dirnames, filenames in os.walk(storage, followlinks=False):
            dirnames[:] = [name for name in dirnames if name not in {".", ".."}]
            current = Path(dirpath)
            if current.is_symlink():
                dirnames[:] = []
                continue
            for name in sorted(filenames):
                if not name.endswith(".db"):
                    continue
                if len(found) >= MAX_DB_FILES:
                    truncated = True
                    break
                path = current / name
                loaded = _read_prefix(path, HEADER_BYTES)
                item = {"relative_path": _rel(path, account)}
                if isinstance(loaded, dict):
                    item.update(loaded)
                else:
                    header, size = loaded
                    item.update(classify_database_header(header, size))
                found.append(item)
            if truncated:
                break
    return {"files": found, "truncated": truncated}


def _inspect_wals(account: Path) -> dict:
    found = []
    truncated = False
    root = account / "db_storage"
    if not root.is_dir():
        return {"files": [], "truncated": False}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        if current.is_symlink():
            dirnames[:] = []
            continue
        for name in sorted(filenames):
            if not name.endswith(".db-wal"):
                continue
            if len(found) >= MAX_WAL_FILES:
                truncated = True
                break
            path = current / name
            loaded = _read_prefix(path, 32)
            item = {"relative_path": _rel(path, account)}
            if isinstance(loaded, dict):
                item.update(loaded)
            else:
                header, size = loaded
                item.update(classify_wal_header(header, size))
            found.append(item)
        if truncated:
            break
    return {"files": found, "truncated": truncated}


def _inspect_images(account: Path) -> dict:
    samples = []
    counts = {"wechat_v2": 0, "wechat_v1": 0, "other": 0, "error": 0}
    thumbnail_names = 0
    visited = 0
    truncated = False
    attach = account / "msg" / "attach"
    walk_root = attach if attach.is_dir() else None
    if walk_root is None:
        return {
            "attach_dir_present": False,
            "samples": [],
            "counts": counts,
            "thumbnail_name_count": 0,
            "truncated": False,
        }
    for dirpath, dirnames, filenames in os.walk(walk_root, followlinks=False):
        current = Path(dirpath)
        if current.is_symlink():
            dirnames[:] = []
            continue
        for name in sorted(filenames):
            visited += 1
            if visited > MAX_WALK_DIRECTORIES:
                truncated = True
                break
            if name.endswith("_t.dat"):
                thumbnail_names += 1
            if not name.endswith(".dat") or len(samples) >= MAX_DAT_SAMPLES:
                if name.endswith(".dat"):
                    truncated = True
                continue
            path = current / name
            loaded = _read_prefix(path, DAT_HEADER_BYTES)
            item = {"relative_path": _rel(path, account)}
            if isinstance(loaded, dict):
                item.update(loaded)
                counts["error"] += 1
            else:
                header, size = loaded
                classified = classify_dat_header(header, size)
                item.update(classified)
                counts[classified["classification"]] = counts.get(classified["classification"], 0) + 1
            samples.append(item)
        if truncated:
            break
    return {
        "attach_dir_present": True,
        "samples": samples,
        "counts": counts,
        "thumbnail_name_count": thumbnail_names,
        "truncated": truncated,
    }


def _interesting_dirs(account: Path) -> list[str]:
    found = []
    seen_dirs = 0
    base_depth = len(account.parts)
    for dirpath, dirnames, _filenames in os.walk(account, followlinks=False):
        current = Path(dirpath)
        if current.is_symlink() or len(current.parts) - base_depth > 6:
            dirnames[:] = []
            continue
        seen_dirs += 1
        if seen_dirs > MAX_WALK_DIRECTORIES:
            break
        for name in dirnames:
            if name.casefold() in INTERESTING_DIR_NAMES or "kvcomm" in name.casefold():
                found.append(_rel(current / name, account))
            if len(found) >= MAX_NAMED_DIRS:
                return found
    return found


def inspect_roots(roots: list[Path]) -> dict:
    locations = []
    accounts = []
    for root in roots:
        entry = {"path": str(root), "exists": False, "readable": False}
        try:
            exists = root.exists()
        except OSError as exc:
            entry["error"] = _os_error(exc)
            locations.append(entry)
            continue
        entry["exists"] = exists
        if not exists:
            locations.append(entry)
            continue
        children = _child_dirs(root)
        if isinstance(children, dict):
            entry["error"] = children["error"]
            locations.append(entry)
            continue
        entry["readable"] = True
        entry["child_dir_count"] = len(children)
        locations.append(entry)
        own_marker = _account_marker(root)
        if own_marker and len(accounts) < MAX_ACCOUNTS:
            accounts.append(_inspect_account(root, own_marker, root.parent))
        for child in children:
            if len(accounts) >= MAX_ACCOUNTS:
                break
            marker = _account_marker(child)
            if marker:
                accounts.append(_inspect_account(child, marker, root))
                continue
            if root.name == "xwechat_files":
                continue
            nested_children = _child_dirs(child)
            if isinstance(nested_children, dict):
                continue
            for nested in nested_children:
                if len(accounts) >= MAX_ACCOUNTS:
                    break
                nested_marker = _account_marker(nested)
                if nested_marker:
                    accounts.append(_inspect_account(nested, nested_marker, root))
    return {"locations": locations, "accounts": accounts}


def _inspect_account(account: Path, marker: str, root: Path) -> dict:
    return {
        "root": str(root),
        "name": account.name,
        "layout": marker,
        "databases": _inspect_databases(account),
        "wal": _inspect_wals(account),
        "images": _inspect_images(account),
        "interesting_directories": _interesting_dirs(account),
    }


def _app_candidates(home: Path) -> list[Path]:
    return [
        Path("/Applications/WeChat.app"),
        Path("/Applications") / "微信.app",
        home / "Applications" / "WeChat.app",
        home / "Applications" / "微信.app",
    ]


def _read_app(bundle: Path) -> dict:
    plist_path = bundle / "Contents" / "Info.plist"
    item = {"path": str(bundle), "exists": bundle.exists()}
    if not item["exists"]:
        return item
    try:
        with plist_path.open("rb") as handle:
            info = plistlib.load(handle)
    except OSError as exc:
        item["error"] = _os_error(exc)
        return item
    except Exception:
        item["error"] = "plist_unreadable"
        return item
    item["bundle_id"] = info.get("CFBundleIdentifier")
    item["short_version"] = info.get("CFBundleShortVersionString")
    item["build_version"] = info.get("CFBundleVersion")
    item["executable"] = info.get("CFBundleExecutable")
    item["codesign"] = _codesign(bundle)
    return item


def _codesign(bundle: Path) -> dict:
    try:
        completed = subprocess.run(
            ["codesign", "-d", "--verbose=2", str(bundle)],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": type(exc).__name__}
    text = (completed.stderr or "") + (completed.stdout or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    flags = next((line for line in lines if line.startswith("CodeDirectory") or "flags=" in line), "")
    authority = [line.split("=", 1)[1] for line in lines if line.startswith("Authority=")]
    return {
        "exit_code": completed.returncode,
        "flags_line": flags[:300],
        "runtime_flag": "runtime" in flags,
        "adhoc": any("Signature=adhoc" in line or "flags=0x2(adhoc)" in line for line in lines),
        "authorities": authority[:4],
        "get_task_allow": "get-task-allow" in text,
    }


def _list_processes() -> list[dict]:
    try:
        completed = subprocess.run(
            ["ps", "-ax", "-o", "pid=", "-o", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [{"error": type(exc).__name__}]
    found = []
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        command = command.strip()
        base = Path(command.split()[0]).name if command else ""
        interesting = (
            base in {"WeChat", "WeChatAppEx", "Weixin"}
            or "/WeChat.app/" in command
            or "WeChatAppEx" in command
        )
        if interesting:
            found.append({"pid": int(pid_text), "command": command[:240]})
    return found


def _attach_summary(processes: list[dict]) -> dict:
    if sys.platform != "darwin":
        return {"attempted": False, "reason": "not_darwin", "results": []}
    selected = []
    for proc in processes:
        if "error" in proc or "pid" not in proc:
            continue
        command = proc.get("command") or ""
        if "/Contents/MacOS/WeChat" in command or command.split()[0:1] == ["WeChat"]:
            selected.append(proc)
    for proc in processes:
        if len(selected) >= 4:
            break
        if proc in selected or "pid" not in proc:
            continue
        if "WeChatAppEx" in (proc.get("command") or ""):
            selected.append(proc)
    results = []
    for proc in selected[:4]:
        try:
            results.append(_task_for_pid(int(proc["pid"])))
        except Exception as exc:
            results.append({
                "pid": int(proc["pid"]),
                "error": type(exc).__name__,
                "memory_bytes_logged": 0,
            })
    return {
        "attempted": True,
        "euid": os.geteuid(),
        "memory_bytes_logged": 0,
        "results": results,
    }


def _task_for_pid(pid: int) -> dict:
    import ctypes

    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libc.mach_task_self.restype = ctypes.c_uint32
    libc.task_for_pid.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.POINTER(ctypes.c_uint32)]
    libc.task_for_pid.restype = ctypes.c_int
    libc.mach_vm_region.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    libc.mach_vm_region.restype = ctypes.c_int
    libc.mach_port_deallocate.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    libc.mach_port_deallocate.restype = ctypes.c_int

    self_task = libc.mach_task_self()
    task = ctypes.c_uint32(0)
    kern = int(libc.task_for_pid(self_task, pid, ctypes.byref(task)))
    result = {"pid": pid, "kern_return": kern, "attached": kern == 0, "memory_bytes_logged": 0}
    if kern != 0 or task.value == 0:
        return result
    try:
        result["regions"] = _count_regions(libc, task.value)
    finally:
        libc.mach_port_deallocate(self_task, task)
        result["task_port_released"] = True
    return result


def _count_regions(libc, task: int) -> dict:
    import ctypes

    address = ctypes.c_uint64(0)
    size = ctypes.c_uint64(0)
    info = ctypes.create_string_buffer(64)
    object_name = ctypes.c_uint32(0)
    total = 0
    readable = 0
    while total < 100000:
        count = ctypes.c_uint32(9)
        kern = int(
            libc.mach_vm_region(
                task,
                ctypes.byref(address),
                ctypes.byref(size),
                9,
                ctypes.cast(info, ctypes.c_void_p),
                ctypes.byref(count),
                ctypes.byref(object_name),
            )
        )
        if kern != 0:
            return {"walk_kern_return": kern, "regions": total, "readable_regions": readable}
        total += 1
        protection = int.from_bytes(info.raw[:4], "little", signed=True)
        if protection & 0x1:
            readable += 1
        nxt = address.value + size.value
        if size.value == 0 or nxt <= address.value:
            return {
                "walk_kern_return": 0,
                "regions": total,
                "readable_regions": readable,
                "stopped": "non_advancing",
            }
        address.value = nxt
    return {"walk_kern_return": 0, "regions": total, "readable_regions": readable, "stopped": "cap"}


def _sip_status() -> dict:
    try:
        completed = subprocess.run(
            ["csrutil", "status"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": type(exc).__name__}
    text = (completed.stdout or completed.stderr or "").strip()
    return {"exit_code": completed.returncode, "summary": text[:300]}


def inspect_host(home: Path) -> dict:
    if sys.platform != "darwin":
        return {"skipped": True, "reason": "not_darwin"}
    processes = _list_processes()
    return {
        "skipped": False,
        "os": platform.mac_ver()[0],
        "machine": platform.machine(),
        "python": platform.python_version(),
        "apps": [_read_app(path) for path in _app_candidates(home)],
        "sip": _sip_status(),
        "processes": processes,
        "attach": _attach_summary(processes),
    }


def build_report(roots: list[Path], home: Path, *, host: dict | None = None) -> dict:
    return {
        "schema": SCHEMA,
        "probe_sha256": _script_sha256(),
        "platform": sys.platform,
        "guarantees": {
            "wechat_open_mode": "read-only",
            "process_memory_bytes_logged": 0,
            "resign_performed": False,
            "sip_changed": False,
        },
        "host": host if host is not None else inspect_host(home),
        "data": inspect_roots(roots),
    }


def render_markdown(report: dict) -> str:
    lines = [
        "# Noteford macOS 探测报告",
        "",
        f"脚本摘要 `{report['probe_sha256'][:16]}`",
        "",
        "本报告来自只读探测。salt 是数据库文件头的前 16 字节，不是密钥。报告不含消息正文。",
        "",
        "## 主机",
        "",
    ]
    host = report["host"]
    if host.get("skipped"):
        lines.append(f"主机检查已跳过：{host.get('reason')}")
    else:
        lines.append(f"macOS {host.get('os') or '未知'}，架构 {host.get('machine')}，Python {host.get('python')}")
        sip = host.get("sip") or {}
        lines.append(f"SIP：{sip.get('summary') or sip.get('error') or '无输出'}")
        lines.append("")
        lines.append("### 应用")
        lines.append("")
        for app in host.get("apps") or []:
            if not app.get("exists"):
                continue
            sign = app.get("codesign") or {}
            lines.append(
                f"- `{app.get('path')}` {app.get('short_version')} ({app.get('build_version')}) "
                f"bundle `{app.get('bundle_id')}` runtime={sign.get('runtime_flag')} adhoc={sign.get('adhoc')}"
            )
        lines.append("")
        lines.append("### 进程与附加")
        lines.append("")
        attach = host.get("attach") or {}
        lines.append(f"euid={attach.get('euid')}，记录的内存字节数={attach.get('memory_bytes_logged')}")
        for item in attach.get("results") or []:
            lines.append(
                f"- pid {item.get('pid')} kern_return={item.get('kern_return')} attached={item.get('attached')} "
                f"regions={item.get('regions')}"
            )
        if not (attach.get("results") or []):
            lines.append("没有找到要尝试附加的微信进程，或本次没有尝试。")
    lines.extend(["", "## 数据目录", ""])
    for location in report["data"]["locations"]:
        state = "存在且可读" if location.get("readable") else ("存在" if location.get("exists") else "不存在")
        if location.get("error"):
            state = location["error"]
        extra = f"，子目录 {location['child_dir_count']}" if "child_dir_count" in location else ""
        lines.append(f"- `{location['path']}`：{state}{extra}")
    if not report["data"]["accounts"]:
        lines.extend(["", "未找到同时包含 `db_storage/session/session.db` 与 `db_storage/contact/contact.db` 的账号目录，也未找到旧式 `Message/*.db` 目录。", ""])
    for account in report["data"]["accounts"]:
        lines.extend(["", f"## 账号 `{account['name']}`", "", f"布局 `{account['layout']}`，根目录 `{account['root']}`", ""])
        databases = account["databases"]["files"]
        kinds: dict[str, int] = {}
        for item in databases:
            kinds[item.get("classification") or item.get("error") or "unknown"] = (
                kinds.get(item.get("classification") or item.get("error") or "unknown", 0) + 1
            )
        lines.append("数据库分类：" + ", ".join(f"{key} {value}" for key, value in sorted(kinds.items())))
        for item in databases[:12]:
            lines.append(
                f"- `{item['relative_path']}` {item.get('classification') or item.get('error')} "
                f"size={item.get('size')} salt={item.get('salt_sha256_16')}"
            )
        if len(databases) > 12:
            lines.append(f"- 其余 {len(databases) - 12} 个见 report.json")
        wal_files = account["wal"]["files"]
        wal_kinds: dict[str, int] = {}
        for item in wal_files:
            wal_kinds[item.get("classification") or item.get("error") or "unknown"] = (
                wal_kinds.get(item.get("classification") or item.get("error") or "unknown", 0) + 1
            )
        lines.append("WAL：" + (", ".join(f"{key} {value}" for key, value in sorted(wal_kinds.items())) or "无"))
        images = account["images"]
        lines.append(
            f"图片目录存在={images['attach_dir_present']}，抽样 {images['counts']}，"
            f"`*_t.dat` 名称计数 {images['thumbnail_name_count']}"
        )
        named = [item for item in account["interesting_directories"] if "kvcomm" in item.casefold()]
        lines.append("kvcomm 路径：" + (", ".join(f"`{item}`" for item in named) or "未发现"))
    lines.extend(["", "完整结构见同目录 `report.json`。", ""])
    return "\n".join(lines)


def write_report(report: dict, output: Path, protected_roots: list[Path]) -> None:
    output = output.expanduser()
    if output.exists() and not output.is_dir():
        raise SystemExit(f"output exists and is not a directory: {output}")
    for root in protected_roots:
        try:
            if root.exists() and (_is_within(output, root) or _is_within(root, output)):
                raise SystemExit(f"refusing to write inside or over a WeChat path: {output}")
        except OSError:
            continue
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "report.md").write_text(render_markdown(report), encoding="utf-8")


def _snapshot_tree(root: Path) -> list[tuple[str, int, int]]:
    rows = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            info = path.lstat()
            rows.append((str(path), info.st_size, info.st_mtime_ns))
    return rows


def run_self_test() -> int:
    with tempfile.TemporaryDirectory(prefix="noteford-mac-probe-") as temporary:
        root = Path(temporary) / "xwechat_files"
        account = root / "wxid_demo_ab12"
        session = account / "db_storage" / "session" / "session.db"
        contact = account / "db_storage" / "contact" / "contact.db"
        wal = account / "db_storage" / "message" / "message_0.db-wal"
        dat = account / "msg" / "attach" / "ab" / "cd" / "Img" / "sample_t.dat"
        kv = account / "db_storage" / "kvcomm" / "cache.bin"
        for path in (session, contact, wal, dat, kv):
            path.parent.mkdir(parents=True, exist_ok=True)
        session.write_bytes(bytes(range(256)) * 32)
        contact.write_bytes(b"SQLite format 3\x00" + bytes(PAGE_SIZE - 16))
        wal.write_bytes(b"\x37\x7f\x06\x82" + b"\x00" * 28)
        dat.write_bytes(b"\x07\x08V2\x08\x07" + b"\x00" * 32)
        kv.write_bytes(b"not-a-key")
        before = _snapshot_tree(root)
        report = build_report([root], Path(temporary), host={"skipped": True, "reason": "self-test"})
        after = _snapshot_tree(root)
        if before != after:
            raise SystemExit("self-test mutated the fixture")
        output = Path(temporary) / "out"
        write_report(report, output, [root])
        accounts = report["data"]["accounts"]
        if len(accounts) != 1 or accounts[0]["name"] != "wxid_demo_ab12":
            raise SystemExit("self-test did not find the fixture account")
        kinds = {item["relative_path"]: item["classification"] for item in accounts[0]["databases"]["files"]}
        if kinds.get("db_storage/session/session.db") != "possible_sqlcipher4_page4096":
            raise SystemExit(f"unexpected session classification: {kinds}")
        if kinds.get("db_storage/contact/contact.db") != "sqlite_plaintext":
            raise SystemExit(f"unexpected contact classification: {kinds}")
        if accounts[0]["wal"]["files"][0]["classification"] != "sqlite_wal_header":
            raise SystemExit("unexpected wal classification")
        if accounts[0]["images"]["counts"]["wechat_v2"] != 1:
            raise SystemExit("unexpected dat classification")
        if not any("kvcomm" in item for item in accounts[0]["interesting_directories"]):
            raise SystemExit("kvcomm directory was not reported")
        if report["guarantees"]["process_memory_bytes_logged"] != 0:
            raise SystemExit("self-test logged process memory")
        if not (output / "report.md").is_file():
            raise SystemExit("markdown report missing")
    print("mac-probe self-test ok")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only macOS WeChat layout probe")
    parser.add_argument("--output", type=Path, help="directory for report.json and report.md")
    parser.add_argument("--root", action="append", type=Path, default=[], help="extra existing data directory")
    parser.add_argument("--self-test", action="store_true", help="run the fixture check and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.self_test:
        return run_self_test()
    if sys.platform != "darwin":
        print("This probe reads a Mac WeChat layout. On this system run: python3 scripts/mac-probe/probe.py --self-test", file=sys.stderr)
        return 2
    if args.output is None:
        print("--output is required", file=sys.stderr)
        return 2
    home = Path.home()
    roots = default_search_roots(home)
    for extra in args.root:
        if not extra.expanduser().is_dir():
            print(f"--root is not an existing directory: {extra}", file=sys.stderr)
            return 2
        roots.insert(0, extra.expanduser())
    protected = [path for path in roots if path.exists()]
    report = build_report(roots, home, host={"skipped": True, "reason": "host_checks_not_finished"})
    write_report(report, args.output, protected)
    try:
        report["host"] = inspect_host(home)
    except Exception as exc:
        report["host"] = {"skipped": True, "reason": type(exc).__name__}
    write_report(report, args.output, protected)
    print(args.output.expanduser() / "report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
