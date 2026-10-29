"""Capture WeChat SQLCipher keys via LLDB Launch of a temporary copy.

Official /Applications/WeChat.app is never resigned or injected. The capture
worker runs under Apple's /usr/bin/python3 + lldb. Candidates are accepted only
after page-1 HMAC verification (crypto.verify_key / local verified()).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .mac_key_storage import capture_session
from typing import Callable

from .crypto import find_matching_key, verify_key

PBKDF_ROUNDS_PASSPHRASE = 256000
PBKDF_ROUNDS_MAC = 2
PBKDF2_ALGORITHM = 2
SHA512_PRF = 5
AES_ALGORITHM = 0
MAC_SALT_XOR = 0x3A
DB_KEY_ACCOUNT = "noteford-wechat-db-keys"
IMAGE_KEY_ACCOUNT = "noteford-wechat-image-aes"
KEYCHAIN_SERVICE = "com.followhongyun.noteford"

ENTITLEMENTS = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>com.apple.security.get-task-allow</key>
	<true/>
</dict>
</plist>
"""

WORKER_NAME = "mac_lldb_worker.py"


class CaptureError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def mac_salt(page_salt: bytes) -> bytes:
    return bytes(byte ^ MAC_SALT_XOR for byte in page_salt)


def official_signature_text_ok(text: str) -> bool:
    """Website WeChat is a Tencent Developer ID app. Ad-hoc and App Store signatures are refused."""

    if "Signature=adhoc" in text or "flags=0x2(adhoc)" in text:
        return False
    return "Developer ID Application" in text and "Tencent" in text


def codesign_command(app: Path, entitlements: Path) -> list[str]:
    """Ad-hoc sign command for a *copy* — main bundle only, NO --deep.

    见 mac_resign.codesign_copy_command 的说明：--deep 会把 libwxld.dylib 等嵌套组件
    一并 ad-hoc 重签、抹掉原版 Tencent 签名，触发反篡改自检 exit 255。只签主 bundle，
    保留嵌套的原版签名。Never use on /Applications.
    """

    return [
        "/usr/bin/codesign",
        "--force",
        "--sign",
        "-",
        "--timestamp=none",
        "--entitlements",
        str(entitlements),
        "--generate-entitlement-der",
        str(app),
    ]


def worker_script_path() -> Path:
    return Path(__file__).with_name(WORKER_NAME)


def default_db_roots(home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    documents = home / "Library" / "Containers" / "com.tencent.xinWeChat" / "Data" / "Documents"
    return [
        documents / "app_data" / "xwechat_files",
        documents / "xwechat_files",
        home / "Documents" / "xwechat_files",
    ]


def _account_dirs(root: Path) -> list[Path]:
    if not root.is_dir() or root.is_symlink():
        return []
    found = []
    try:
        children = list(root.iterdir())
    except OSError:
        return []
    for child in children:
        try:
            if not child.is_dir() or child.is_symlink():
                continue
            session = child / "db_storage" / "session" / "session.db"
            contact = child / "db_storage" / "contact" / "contact.db"
            if session.is_file() and contact.is_file() and not session.is_symlink() and not contact.is_symlink():
                found.append(child)
        except OSError:
            continue
    return found


def select_active_root(candidates: list[Path]) -> Path | None:
    usable = [path for path in candidates if _account_dirs(path)]
    for path in usable:
        if "app_data" in path.parts:
            return path
    return usable[0] if usable else None


@dataclass(frozen=True)
class DatabaseFile:
    relative_path: str
    path: Path
    page1: bytes
    salt: bytes


def load_core_databases(root: Path) -> list[DatabaseFile]:
    """Page-1 of each account's session.db and contact.db only.

    Used by the stale-key check. It does not walk message, favorite, or other
    libraries.
    """

    files: list[DatabaseFile] = []
    for account in _account_dirs(root):
        for name in ("session/session.db", "contact/contact.db"):
            path = account / "db_storage" / name
            if not path.is_file() or path.is_symlink():
                continue
            try:
                with path.open("rb") as handle:
                    page1 = handle.read(4096)
            except OSError:
                continue
            if len(page1) < 4096 or page1.startswith(b"SQLite format 3\x00"):
                continue
            relative = f"{account.name}/db_storage/{name}"
            files.append(DatabaseFile(relative, path, page1, page1[:16]))
    return files


def load_databases(root: Path) -> list[DatabaseFile]:
    files: list[DatabaseFile] = []
    for account in _account_dirs(root):
        storage = account / "db_storage"
        for dirpath, dirnames, filenames in os.walk(storage, followlinks=False):
            current = Path(dirpath)
            if current.is_symlink():
                dirnames[:] = []
                continue
            for name in sorted(filenames):
                if not name.endswith(".db") or name.endswith(".db-wal"):
                    continue
                path = current / name
                if path.is_symlink():
                    continue
                try:
                    with path.open("rb") as handle:
                        page1 = handle.read(4096)
                except OSError:
                    continue
                if len(page1) < 4096 or page1.startswith(b"SQLite format 3\x00"):
                    continue
                relative = f"{account.name}/{path.relative_to(account).as_posix()}"
                files.append(DatabaseFile(relative, path, page1, page1[:16]))
    return files


def write_targets_file(path: Path, databases: list[DatabaseFile]) -> None:
    payload = {
        "databases": [
            {"relative_path": item.relative_path, "page1_hex": item.page1.hex()} for item in databases
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    temporary.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _hex_bytes(value: object, size: int) -> bytes | None:
    if not isinstance(value, str) or len(value) != size * 2:
        return None
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        return None
    return raw if len(raw) == size else None


def _profile_ok(record: dict) -> bool:
    algorithm = record.get("algorithm")
    prf = record.get("prf")
    if algorithm is not None and int(algorithm) != PBKDF2_ALGORITHM:
        return False
    if prf is not None and int(prf) != SHA512_PRF:
        return False
    return True


def plausible_raw_key(key: bytes) -> bool:
    return len(key) == 32 and len(set(key)) >= 8 and key not in {b"\x00" * 32, b"\xff" * 32}


def db_candidates_from_record(record: dict, page_salts: set[bytes]) -> tuple[list[dict], str]:
    """Map a PBKDF/AES observation to database-key candidates (unit-test helper)."""

    kind = record.get("kind")
    if kind == "pbkdf":
        if not _profile_ok(record):
            return [], "rejected_profile"
        password = _hex_bytes(record.get("password"), 32)
        salt = _hex_bytes(record.get("salt"), 16)
        derived = _hex_bytes(record.get("derived"), 32)
        if password is None or salt is None or derived is None:
            return [], "rejected_shape"
        rounds = int(record.get("rounds") or 0)
        if rounds == PBKDF_ROUNDS_MAC:
            page = next((item for item in page_salts if mac_salt(item) == salt), None)
            if page is None or not plausible_raw_key(password):
                return [], "rejected_salt"
            return [{
                "key_hex": password.hex(),
                "salt_hex": page.hex(),
                "source": "pbkdf-rounds-2",
            }], "rounds_2"
        if rounds == PBKDF_ROUNDS_PASSPHRASE:
            if salt not in page_salts or not plausible_raw_key(derived):
                return [], "rejected_salt"
            return [{
                "key_hex": derived.hex(),
                "salt_hex": salt.hex(),
                "source": "pbkdf-rounds-256000",
            }], "rounds_256000"
        return [], "rounds_other"
    if kind == "aes" and int(record.get("key_len") or 0) == 32 and int(record.get("algorithm") or AES_ALGORITHM) == AES_ALGORITHM:
        key = _hex_bytes(record.get("key"), 32)
        if key is None or not plausible_raw_key(key):
            return [], "rejected_shape"
        return [{"key_hex": key.hex(), "salt_hex": None, "source": "aes-256"}], "aes32"
    return [], "ignored"


def image_candidates_from_record(record: dict) -> list[bytes]:
    if record.get("kind") != "aes" or int(record.get("key_len") or 0) != 16:
        return []
    if int(record.get("algorithm") or AES_ALGORITHM) != AES_ALGORITHM:
        return []
    key = _hex_bytes(record.get("key"), 16)
    if key is None:
        return []
    try:
        text = key.decode("ascii")
    except UnicodeError:
        return []
    if len(text) != 16 or not text.isalnum():
        return []
    return [key]


def parse_record(line: str) -> dict | None:
    line = line.strip()
    if not line or len(line) > 2000:
        return None
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(record, dict) or record.get("v") != 1:
        return None
    if record.get("kind") not in {"pbkdf", "aes"}:
        return None
    return record


def image_key_opens_probe(key: bytes, probe: bytes) -> bool:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if len(key) != 16 or len(probe) != 16:
        return False
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    plain = decryptor.update(probe) + decryptor.finalize()
    return plain.startswith(b"\xff\xd8\xff") or plain[:4] in {b"\x89PNG", b"GIF8", b"RIFF", b"wxgf"}


def match_databases(databases: list[DatabaseFile], candidates: list[dict]) -> list[dict]:
    matched = []
    seen: set[tuple[str, str]] = set()
    for database in databases:
        found = find_matching_key(database.path, candidates)
        if not found:
            continue
        key, _fingerprint = found
        raw = key[:32]
        source = next((item["source"] for item in candidates if item["key_hex"] == raw.hex()), "matched")
        item = {
            "relative_path": database.relative_path,
            "key_hex": raw.hex(),
            "salt_hex": database.salt.hex(),
            "source": source,
        }
        identity = (item["relative_path"], item["key_hex"])
        if identity not in seen:
            seen.add(identity)
            matched.append(item)
    return matched


def covers_core(matched: list[dict]) -> bool:
    by_account: dict[str, set[str]] = {}
    for item in matched:
        account = item["relative_path"].split("/", 1)[0]
        by_account.setdefault(account, set()).add(item["relative_path"])
    return any(
        any(path.endswith("session/session.db") for path in paths)
        and any(path.endswith("contact/contact.db") for path in paths)
        for paths in by_account.values()
    )


def page1_hmac_ok(key_hex: str, page1: bytes) -> bool:
    try:
        key = bytes.fromhex(key_hex)
    except ValueError:
        return False
    return verify_key(key, page1)


@dataclass
class CaptureConfig:
    official_app: Path
    work_root: Path
    db_root: Path
    timeout_seconds: float = 180.0
    shadow_app: Path | None = None
    account_prefix: str | None = None
    require_live_login: bool = False
    is_cancelled: Callable[[], bool] | None = None
    image_probe: bytes | None = None
    # Deprecated fields kept so older callers fail clearly if they pass a hook.
    hook_library: Path | None = None


@dataclass
class CaptureResult:
    error: str | None
    matched: list[dict] = field(default_factory=list)
    image_key: bytes | None = None
    counts: dict = field(default_factory=dict)
    wechat_left_running: bool = False
    trial_killed: bool = False

    def candidate_keys(self) -> list[dict]:
        return [{"key_hex": item["key_hex"], "salt_hex": item["salt_hex"]} for item in self.matched]

    def public_summary(self) -> dict:
        return {
            "error": self.error,
            "counts": self.counts,
            "wechat_left_running": self.wechat_left_running,
            "trial_killed": self.trial_killed,
            "databases": [
                {
                    "relative_path": item["relative_path"],
                    "source": item["source"],
                    "fingerprint": fingerprint(bytes.fromhex(item["key_hex"])),
                }
                for item in self.matched
            ],
            "image_fingerprint": fingerprint(self.image_key) if self.image_key else None,
            "scan_hint": "早上验收：在临时微信窗口扫码/确认登录；结束后自动杀副本并恢复原版。",
        }


class MemoryKeyStore:
    def __init__(self):
        self.items: dict[str, bytes] = {}

    def put(self, account: str, payload: bytes) -> None:
        self.items[account] = payload

    def get(self, account: str) -> bytes | None:
        return self.items.get(account)


class SecurityKeychainStore:
    """Login-keychain item. The secret is passed to /usr/bin/security as hex, not via a shell."""

    def __init__(self, runner: Callable[..., subprocess.CompletedProcess] | None = None):
        self._run = runner
        from .product_workspace import process_root, WorkspaceAccess
        self._root = process_root()
        self._access = WorkspaceAccess(self._root) if self._root else None

    def _account(self, account: str) -> str:
        if self._access:
            self._access.check()
            marker = json.loads((self._root / '.product-workspace.json').read_text())
            if marker['workspace'] != 'legacy':
                return account + ':' + marker['workspace']
        return account

    def put(self, account: str, payload: bytes) -> None:
        account = self._account(account)
        if self._run is None:
            from .keychain_secret import put_blob
            try:
                put_blob(KEYCHAIN_SERVICE, account, payload)
            except OSError:
                raise CaptureError("keychain_failed") from None
            return
        command = [
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            account,
            "-X",
            payload.hex(),
        ]
        completed = self._run(command, check=False, capture_output=True, text=True, timeout=30)
        if completed.returncode != 0:
            raise CaptureError("keychain_failed")

    def get(self, account: str) -> bytes | None:
        account = self._account(account)
        if self._run is None:
            from .keychain_secret import get_blob
            try:
                return get_blob(KEYCHAIN_SERVICE, account)
            except OSError:
                raise CaptureError("keychain_failed") from None
        command = [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            KEYCHAIN_SERVICE,
            "-a",
            account,
            "-w",
        ]
        completed = self._run(command, check=False, capture_output=True, text=True, timeout=30)
        if completed.returncode == 44:
            return None
        if completed.returncode != 0:
            raise CaptureError("keychain_failed")
        text = completed.stdout
        if text.endswith("\n"):
            text = text[:-1]
        return text.encode("utf-8")


def merge_key_records(previous: list[dict], captured: list[dict]) -> list[dict]:
    """Replace only matching databases; another account's credentials survive."""
    records = {item['relative_path']: item for item in previous
               if isinstance(item, dict) and isinstance(item.get('relative_path'), str)}
    records.update({item['relative_path']: item for item in captured})
    return list(records.values())


def publish_capture(path: Path, store, matched: list[dict], *, observed_accounts=None) -> None:
    from .mac_key_storage import database_payload
    database_payload(path, store, captured=matched, observed_accounts=observed_accounts)


def _store_capture(store, matched: list[dict], image_key: bytes | None = None) -> list[dict]:
    existing = store.get(DB_KEY_ACCOUNT)
    if existing:
        previous = json.loads(existing)
        matched = merge_key_records(previous.get('keys', []), matched)
    payload = json.dumps(
        {"schema": 1, "keys": matched},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    store.put(DB_KEY_ACCOUNT, payload)
    if image_key is not None:
        store.put(
            IMAGE_KEY_ACCOUNT,
            json.dumps({"schema": 1, "aes_key_ascii": image_key.decode("ascii")}).encode("ascii"),
        )

    return matched

def write_keys_file(path: Path, matched: list[dict], *, observed_accounts: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".new")
    if temporary.exists():
        temporary.unlink()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            payload = {"schema": 1, "keys": matched}
            if observed_accounts is not None:
                payload["observed_accounts"] = observed_accounts
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if temporary.exists():
            temporary.unlink()


def load_keys_file(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    keys = payload.get("keys") if isinstance(payload, dict) else None
    return list(keys) if isinstance(keys, list) else []


def resolve_system_python_and_lldb() -> tuple[str, str]:
    python = "/usr/bin/python3"
    lldb = "/usr/bin/lldb"
    if not Path(python).is_file() or not Path(lldb).is_file():
        raise CaptureError("lldb_or_python_missing")
    completed = subprocess.run([lldb, "-P"], check=False, capture_output=True, text=True, timeout=20)
    path = (completed.stdout or "").strip()
    if completed.returncode != 0 or not path:
        raise CaptureError("lldb_python_path_failed")
    return python, path


@capture_session
def capture_login(
    config: CaptureConfig,
    actions,
    store,
    *,
    acknowledge_debug_copy: bool = False,
    force: bool = False,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> CaptureResult:
    """Launch the prepared WeChat copy under LLDB and capture verified keys.

    Requires ``acknowledge_debug_copy=True`` (user must have quit WeChat and
    accepted that the temporary copy shares the real account data). The morning
    QR-scan step happens while this function blocks on the worker.
    """

    counts: dict[str, int] = {
        "databases": 0,
        "hmac_pass": 0,
        "worker_exit": -1,
    }
    matched: list[dict] = []
    error: str | None = None
    trial_killed = False
    try:
        if not acknowledge_debug_copy:
            raise CaptureError("acknowledge_debug_copy_required")
        if config.hook_library is not None:
            raise CaptureError("hook_path_abandoned_use_lldb_copy")
        shadow = config.shadow_app
        if shadow is None:
            shadow = config.work_root / "WeChat-trial.app"
        exe = shadow / "Contents" / "MacOS" / "WeChat"
        if not exe.is_file():
            raise CaptureError("copy_not_prepared")
        if str(shadow).startswith("/Applications/") or shadow.resolve() == config.official_app.resolve():
            raise CaptureError("refusing_to_debug_official_app")
        databases = load_databases(config.db_root)
        if config.account_prefix:
            databases = [db for db in databases if db.relative_path.split("/", 1)[0] == config.account_prefix]
        if not databases:
            raise CaptureError("databases_not_found")
        counts["databases"] = len(databases)
        targets = config.work_root / "targets.json"
        keys_out = config.work_root / "keys.json"
        from .mac_key_storage import database_payload
        if database_payload(keys_out, store)["keys"] and not force:
            raise CaptureError("keys_exist_use_new_state_or_force")
        pending = config.work_root / 'keys.pending.json'
        pending.unlink(missing_ok=True)
        write_targets_file(targets, databases)
        worker = worker_script_path()
        if not worker.is_file():
            raise CaptureError("lldb_worker_missing")

        notify = getattr(actions, "notify_scan_required", None)
        if notify is not None:
            notify()

        events = actions.run_lldb_worker(
            exe=exe,
            targets=targets,
            out=pending,
            timeout=int(config.timeout_seconds),
            work_root=config.work_root,
        )
        counts["worker_exit"] = int(events.get("exit_code", -1))
        trial_killed = bool(events.get("trial_killed", True))
        matched = load_keys_file(pending)
        pending.unlink(missing_ok=True)
        # Re-verify every key against live page1 before trusting the file.
        trusted = []
        by_rel = {item.relative_path: item for item in databases}
        for item in matched:
            rel = item.get("relative_path")
            key_hex = item.get("key_hex")
            db = by_rel.get(rel) if isinstance(rel, str) else None
            if db is None or not isinstance(key_hex, str) or not page1_hmac_ok(key_hex, db.page1):
                continue
            trusted.append({
                "relative_path": rel,
                "key_hex": key_hex,
                "salt_hex": item.get("salt_hex") or db.salt.hex(),
                "source": item.get("source") or "lldb",
            })
        matched = trusted
        counts["hmac_pass"] = len(matched)
        if covers_core(matched):
            publish_capture(keys_out, store, matched)
            error = None
        elif matched:
            error = "coverage_incomplete"
        else:
            error = "timeout" if counts["worker_exit"] == 3 else "capture_failed"
            matched = []
    except CaptureError as exc:
        error = exc.code
        matched = []
    finally:
        from .mac_key_storage import cleanup_capture_files
        cleanup_capture_files(config.work_root)
    return CaptureResult(
        error=error,
        matched=matched,
        image_key=None,
        counts=counts,
        wechat_left_running=False,
        trial_killed=trial_killed,
    )


class DarwinLldbActions:
    """Run the Apple-Python LLDB worker. Tests inject their own actions."""

    def notify_scan_required(self) -> None:
        return None

    def nudge_scan_window(self, exe: Path, pid: int | None = None) -> None:
        from .mac_resign import activate_scan_script

        del exe
        script = activate_scan_script(pid)
        try:
            subprocess.Popen(
                ["/usr/bin/osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            return

    def run_lldb_worker(
        self,
        *,
        exe: Path,
        targets: Path,
        out: Path,
        timeout: int,
        work_root: Path,
    ) -> dict:
        python, lldb_python = resolve_system_python_and_lldb()
        worker = worker_script_path()
        env = dict(os.environ)
        env["PYTHONPATH"] = lldb_python
        # Strip DYLD_* so nothing injects into the worker or child.
        for key in list(env):
            if key.startswith("DYLD_"):
                env.pop(key, None)
        command = [
            python,
            str(worker),
            "--exe",
            str(exe),
            "--targets",
            str(targets),
            "--out",
            str(out),
            "--timeout",
            str(timeout),
        ]
        log_path = work_root / "capture-events.jsonl"
        proc = None
        try:
            with log_path.open("w", encoding="utf-8") as log:
                try:
                    os.chmod(log_path, 0o600)
                except OSError:
                    pass
                proc = subprocess.Popen(
                    command,
                    start_new_session=True,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                # The worker owns the WeChat pid. Nudge by bundle id once it starts.
                self.nudge_scan_window(exe, pid=None)
                assert proc.stdout is not None
                for line in proc.stdout:
                    # Never echo secrets; worker already omits them. Persist events only.
                    if "enc_key" in line or "key_hex" in line or "password" in line:
                        continue
                    log.write(line)
                    log.flush()
                exit_code = proc.wait(timeout=timeout + 60)
        except subprocess.TimeoutExpired:
            return {"exit_code": 124, "trial_killed": True}
        finally:
            if proc is not None:
                # Stop the worker and its debug child before unlinking outputs,
                # including cancellation and errors while consuming its pipe.
                import signal
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=10)
        return {"exit_code": exit_code, "trial_killed": True}


# Back-compat name for platform_seam / older tests.
DarwinListenActions = DarwinLldbActions


class MacCopyLldbKeySource:
    """Keys from LLDB launch of a temporary WeChat copy."""

    def __init__(self, config: CaptureConfig, actions=None, store=None, **_ignored):
        self.config = config
        self.actions = actions
        self.store = store

    def candidate_keys(self) -> list[dict]:
        actions = self.actions
        store = self.store
        if actions is None or store is None:
            if os.name != "posix" or not Path("/usr/bin/lldb").exists():
                raise RuntimeError("macOS LLDB capture requires darwin or injected actions")
            actions = actions or DarwinLldbActions()
            store = store or SecurityKeychainStore()
        result = capture_login(
            self.config,
            actions,
            store,
            acknowledge_debug_copy=True,
        )
        if result.error:
            raise RuntimeError(result.error)
        return result.candidate_keys()


# Previous names: keep importable, point at the new source.
MacOfficialKeySource = MacCopyLldbKeySource
MacInterposeKeySource = MacCopyLldbKeySource


# --- Abandoned DYLD helpers kept as hard failures so old call sites fail closed ---

def install_launcher(*_args, **_kwargs):
    raise CaptureError("install_launcher_abandoned_use_prepare_copy")


def restore_official_executable(*_args, **_kwargs):
    return False


def launcher_is_ours(_app: Path) -> bool:
    return False


def is_macho(path: Path) -> bool:
    try:
        magic = path.read_bytes()[:4]
    except OSError:
        return False
    return magic in {
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    }
