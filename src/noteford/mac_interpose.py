"""DYLD interpose key capture — the MAIN macOS route.

Instead of attaching LLDB after WeChat has already run (which lands after the
earliest login-time PBKDF and was the likely "copy ran full session yet zero
hits" cause), this route inserts a tiny CommonCrypto interpose dylib into the
*copy* via DYLD_INSERT_LIBRARIES, so the hook is live from image load — before
WeChat's first crypto call. wechat-cli.com (4.1.13, 27/29 libs) uses the same
approach.

Flow:
  1. prepare_copy (mac_resign) re-signs the *copy* only, merging WeChat's own
     entitlements + get-task-allow + cs.allow-dyld-environment-variables +
     cs.disable-library-validation (so DYLD_INSERT is honoured and our ad-hoc
     hook dylib loads despite nested Hardened-Runtime Tencent libs).
  2. build the hook dylib and drop it inside the copy bundle (readable even if
     the copy keeps app-sandbox).
  3. launch the copy with DYLD_INSERT_LIBRARIES=<hook> WECHAT_KEY_HOOK_OUT=<0600>.
  4. Hongyun scans the QR once (human step; we never automate it).
  5. the hook records the shared passphrase (CCKeyDerivationPBKDF input) to the
     0600 JSONL file; we read it and derive EVERY database's key offline with
     derive_passphrase_keys, verifying each by page-1 HMAC. No chat clicks.

Never touches /Applications/WeChat.app. No sudo, SIP stays on.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .mac_key_storage import capture_session

from .mac_capture import (
    CaptureConfig,
    CaptureError,
    CaptureResult,
    covers_core,
    fingerprint,
    load_databases,
    load_core_databases,
    page1_hmac_ok,
    publish_capture,
)
from .mac_lldb_worker import derive_passphrase_keys, verified

HOOK_SOURCE_REL = Path("native/macos/key_hook/wechat_key_hook.c")
HOOK_DYLIB_NAME = "libwechat_key_hook.dylib"
CANDIDATES_NAME = "hook-candidates.jsonl"
# Inside the copy bundle so a sandboxed copy can still read it.
IN_BUNDLE_HOOK_REL = Path("Contents") / "Frameworks" / HOOK_DYLIB_NAME


def repo_root() -> Path:
    # src/noteford/mac_interpose.py -> repo root is three parents up.
    return Path(__file__).resolve().parents[2]


def hook_source_path() -> Path:
    return repo_root() / HOOK_SOURCE_REL


def hook_dylib_path(state_dir: Path) -> Path:
    return state_dir / HOOK_DYLIB_NAME


def candidates_path(state_dir: Path) -> Path:
    return state_dir / CANDIDATES_NAME


def sandbox_candidates_path(db_root: Path) -> Path:
    """Hook output path INSIDE the WeChat container so a sandboxed copy can
    open() it. db_root is .../Containers/<bundle>/Data/Documents/xwechat_files;
    its container Data dir is writable by the sandboxed app, a path outside the
    container is blocked by app-sandbox (observed: open() fails, no candidates).
    """
    container_data = db_root.parent.parent  # .../Data
    return container_data / "Library" / "Caches" / CANDIDATES_NAME


def build_hook(state_dir: Path, *, source: Path | None = None, runner=subprocess.run) -> Path:
    """Stage the packaged hook, or compile it in a source checkout.

    Pure-ish: the actual clang call goes through ``runner`` so tests can stub it.
    """

    out = hook_dylib_path(state_dir)
    if source is None and getattr(sys, "frozen", False):
        bundled = Path(sys._MEIPASS) / "mac-native" / HOOK_DYLIB_NAME
        if not bundled.is_file():
            raise CaptureError("hook_binary_missing")
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(bundled, out)
        return out
    source = source or hook_source_path()
    if not source.is_file():
        raise CaptureError("hook_source_missing")
    out.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "/usr/bin/clang",
        "-arch",
        "arm64",
        "-dynamiclib",
        "-O2",
        str(source),
        "-o",
        str(out),
        "-framework",
        "CoreFoundation",
        "-framework",
        "Security",
    ]
    completed = runner(command, check=False, capture_output=True, text=True, timeout=120)
    if getattr(completed, "returncode", 1) != 0 or not out.is_file():
        raise CaptureError("hook_build_failed")
    # arm64 refuses to load an unsigned dylib (SIGKILL on insert), so ad-hoc
    # sign the freshly built hook. Ad-hoc ("-") needs no identity or network and
    # is enough for DYLD_INSERT into the disable-library-validation copy.
    sign = runner(
        ["/usr/bin/codesign", "--force", "--sign", "-", str(out)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if getattr(sign, "returncode", 1) != 0:
        raise CaptureError("hook_sign_failed")
    return out


def _hex_bytes(value, size: int) -> bytes | None:
    if not isinstance(value, str) or len(value) != size * 2:
        return None
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        return None
    return raw if len(raw) == size else None


@dataclass
class HookObservations:
    hook_loaded: bool
    passphrases: list[bytes]      # 32B CCKeyDerivationPBKDF inputs (shared passphrase)
    direct_keys: list[bytes]      # 32B pbkdf_out / aes_key (per-db enc_key fallback)
    lines: int


def parse_candidates(text: str) -> HookObservations:
    """Parse the hook's JSONL output into passphrase + direct-key candidates.

    Deduplicates while preserving order. Ignores anything that is not a 32-byte
    hex blob, and reads the ``hook_loaded`` marker proving the dylib was inserted.
    """

    hook_loaded = False
    passphrases: list[bytes] = []
    direct: list[bytes] = []
    seen_pp: set[bytes] = set()
    seen_dk: set[bytes] = set()
    lines = 0
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw or len(raw) > 2000:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or rec.get("v") != 1:
            continue
        lines += 1
        kind = rec.get("kind")
        if kind == "hook_loaded":
            hook_loaded = True
            continue
        blob = _hex_bytes(rec.get("hex"), 32)
        if blob is None:
            continue
        if kind == "pbkdf_in" and rec.get("rounds") == 256000:
            if blob not in seen_pp:
                seen_pp.add(blob)
                passphrases.append(blob)
        elif kind in {"pbkdf_out", "aes_key"} or (kind == "pbkdf_in" and rec.get("rounds") == 2):
            if blob not in seen_dk:
                seen_dk.add(blob)
                direct.append(blob)
    return HookObservations(hook_loaded, passphrases, direct, lines)


def derive_matches(observations: HookObservations, databases) -> list[dict]:
    """Turn captured candidates into verified per-database key records.

    Primary path: shared passphrase -> derive_passphrase_keys (PBKDF2-SHA512,
    256000) -> page-1 HMAC verify, covering databases never opened. Fallback:
    treat each direct 32B key as an already-derived enc_key and page-1 HMAC it.
    """

    rows = [(db.relative_path, db.page1) for db in databases]
    by_rel = {db.relative_path: db for db in databases}
    found: dict[str, dict] = {}

    for passphrase in observations.passphrases:
        remaining = [(rel, page) for rel, page in rows if rel not in found]
        if not remaining:
            break
        for rel, key, salt in derive_passphrase_keys(passphrase, remaining):
            if rel not in found:
                found[rel] = {
                    "relative_path": rel,
                    "key_hex": key.hex(),
                    "salt_hex": salt.hex(),
                    "source": "interpose-passphrase",
                }

    for key in observations.direct_keys:
        for rel, page in rows:
            if rel in found:
                continue
            if verified(key, page):
                db = by_rel[rel]
                found[rel] = {
                    "relative_path": rel,
                    "key_hex": key.hex(),
                    "salt_hex": db.salt.hex(),
                    "source": "interpose-direct-key",
                }
    return list(found.values())


class DarwinInterposeActions:
    """Launch the prepared copy with the interpose hook. Tests inject their own."""

    def notify_scan_required(self) -> None:
        return None

    def install_hook_into_bundle(self, shadow: Path, hook: Path) -> Path:
        dest = shadow / IN_BUNDLE_HOOK_REL
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["/bin/cp", "-f", str(hook), str(dest)], check=False, timeout=60)
        if not dest.is_file():
            raise CaptureError("hook_install_failed")
        return dest

    def launch_with_hook(self, *, exe: Path, hook: Path, out_file: Path) -> subprocess.Popen:
        # IMPORTANT (X1c finding): launch the copy's executable DIRECTLY, never via
        # a wrapper like /usr/bin/nohup. Such wrappers cause the kernel to strip
        # all DYLD_* env vars across the exec, so DYLD_INSERT_LIBRARIES never
        # reaches WeChat and the hook silently fails to load (observed: hook
        # unmapped, zero candidates). Popen(exe, env=...) does NOT strip them.
        # WeChat then re-execs/posix_spawns its real GUI process; the hook's own
        # execve/posix_spawn interposes re-inject DYLD_INSERT so the hook rides
        # into that GUI/login process (verified: hook_loaded + vmmap-mapped in the
        # surviving GUI pid, reinject markers present).
        env = dict(os.environ)
        for key in list(env):
            if key.startswith("DYLD_"):
                env.pop(key, None)
        env["DYLD_INSERT_LIBRARIES"] = str(hook)
        env["WECHAT_KEY_HOOK_OUT"] = str(out_file)
        return subprocess.Popen(
            [str(exe)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

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

    def logged_in_account(self, exe: Path, db_root: Path) -> str | None:
        import psutil
        from .login_status import MacLoginDetector
        prefix = str(exe.parent.parent.resolve()) + os.sep
        def inventory():
            files = []
            incomplete = False
            for process in psutil.process_iter(['exe', 'cmdline']):
                try:
                    if not str(process.info.get('exe') or '').startswith(prefix):
                        continue
                    if any(str(arg).startswith('--type=') for arg in process.info.get('cmdline') or []):
                        continue
                    files.append({str(Path(item.path)) for item in process.open_files()})
                except (psutil.Error, OSError):
                    incomplete = True
            return files, incomplete
        status = MacLoginDetector(inventory=inventory).check(db_root)
        return status.get('account') if status.get('state') == 'active' else None

    def process_alive(self, proc: subprocess.Popen) -> bool:
        return proc.poll() is None

    def terminate(self, proc: subprocess.Popen) -> None:
        import signal
        # Launchers may exit before their injected GUI/helper children.
        # All captures start in their own session; never signal official WeChat.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        shadow = Path(proc.args[0]).parent.parent.parent
        if shadow.suffix == '.app':
            from .mac_resign import DarwinResignActions
            actions = DarwinResignActions()
            actions.stop_shadow_processes(shadow)
            if actions.shadow_still_running(shadow):
                raise CaptureError('quit_trial_copy_first')
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=10)


@capture_session
def capture_login_interpose(
    config: CaptureConfig,
    actions,
    store,
    *,
    acknowledge_debug_copy: bool = False,
    force: bool = False,
    hook_dylib: Path | None = None,
    on_verifying=None,
    poll_interval: float = 1.0,
    monotonic=time.monotonic,
    sleep=time.sleep,
) -> CaptureResult:
    """Launch the prepared copy with the interpose hook and capture verified keys.

    Blocks up to config.timeout_seconds while Hongyun scans the QR once. As soon
    as the hook writes a passphrase we can derive every DB key from, we stop.
    """

    counts = {"databases": 0, "hmac_pass": 0, "hook_loaded": 0, "cand_lines": 0, "alive_seconds": 0}
    matched: list[dict] = []
    error: str | None = None
    trial_killed = False
    proc = None
    cand = None
    try:
        if not acknowledge_debug_copy:
            raise CaptureError("acknowledge_debug_copy_required")
        shadow = config.shadow_app or (config.work_root / "WeChat-trial.app")
        exe = shadow / "Contents" / "MacOS" / "WeChat"
        if not exe.is_file():
            raise CaptureError("copy_not_prepared")
        if str(shadow).startswith("/Applications/") or shadow.resolve() == config.official_app.resolve():
            raise CaptureError("refusing_to_debug_official_app")
        selected_account = config.account_prefix
        def current_databases():
            return [db for db in load_databases(config.db_root)
                    if not selected_account or db.relative_path.split('/', 1)[0] == selected_account]
        databases = current_databases()
        if not databases and not config.require_live_login:
            raise CaptureError("databases_not_found")
        counts["databases"] = len(databases)
        observed_accounts = sorted({db.relative_path.split("/", 1)[0] for db in load_core_databases(config.db_root)})

        keys_out = config.work_root / "keys.json"
        from .mac_key_storage import database_payload, cleanup_hook_files
        if database_payload(keys_out, store)["keys"] and not force:
            raise CaptureError("keys_exist_use_new_state_or_force")

        hook = hook_dylib or hook_dylib_path(config.work_root)
        if not hook.is_file():
            raise CaptureError("hook_dylib_missing")
        # Place the hook inside the bundle so a sandboxed copy can read it.
        in_bundle = actions.install_hook_into_bundle(shadow, hook)

        location = sandbox_candidates_path(config.db_root)
        location.parent.mkdir(parents=True, exist_ok=True)
        cleanup_hook_files(config.db_root)
        directory = Path(tempfile.mkdtemp(prefix=f"hook-capture-{os.getpid()}-", dir=location.parent))
        cand = directory / 'candidates.jsonl'
        fd = os.open(cand, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)

        notify = getattr(actions, "notify_scan_required", None)
        if notify is not None:
            notify()

        proc = actions.launch_with_hook(exe=exe, hook=in_bundle, out_file=cand)
        # LSUIElement copies do not steal focus. One activate, no click.
        nudge = getattr(actions, "nudge_scan_window", None)
        if callable(nudge):
            try:
                nudge(exe, pid=getattr(proc, "pid", None))
            except Exception:
                pass

        deadline = monotonic() + float(config.timeout_seconds)
        start = monotonic()
        observations = HookObservations(False, [], [], 0)
        while monotonic() < deadline:
            if config.is_cancelled and config.is_cancelled():
                raise CaptureError('cancelled')
            if config.require_live_login:
                active = actions.logged_in_account(exe, config.db_root)
                if active and config.account_prefix and active != config.account_prefix:
                    raise CaptureError('wrong_account')
                selected_account = active
            if not actions.process_alive(proc):
                # Copy died (likely anti-tamper exit 255) — stop, report.
                trial_killed = True
                break
            if cand.is_file():
                observations = parse_candidates(cand.read_text(encoding="utf-8", errors="ignore"))
                if (observations.passphrases or observations.direct_keys) and (not config.require_live_login or selected_account):
                    # A first login can create databases after the copy starts.
                    databases = current_databases()
                    if on_verifying is not None:
                        on_verifying(len(databases))
                    candidate_matches = derive_matches(observations, databases)
                    if covers_core(candidate_matches):
                        matched = candidate_matches
                        break
                    if config.account_prefix and not candidate_matches:
                        other_cores = [db for db in load_core_databases(config.db_root)
                                       if db.relative_path.split('/', 1)[0] != config.account_prefix]
                        if covers_core(derive_matches(observations, other_cores)):
                            raise CaptureError('wrong_account')
                    matched = candidate_matches  # keep best-so-far
            sleep(poll_interval)
        counts["alive_seconds"] = int(monotonic() - start)

        # Final read in case the window closed right at the deadline.
        if cand.is_file() and not covers_core(matched) and not config.require_live_login:
            observations = parse_candidates(cand.read_text(encoding="utf-8", errors="ignore"))
            databases = current_databases()
            final = derive_matches(observations, databases)
            if len(final) >= len(matched):
                matched = final
        counts["hook_loaded"] = 1 if observations.hook_loaded else 0
        counts["cand_lines"] = observations.lines

        # Re-verify every key against live page1 before trusting it.
        by_rel = {db.relative_path: db for db in databases}
        trusted = []
        for item in matched:
            rel = item.get("relative_path")
            key_hex = item.get("key_hex")
            db = by_rel.get(rel) if isinstance(rel, str) else None
            if db is None or not isinstance(key_hex, str) or not page1_hmac_ok(key_hex, db.page1):
                continue
            trusted.append(item)
        matched = trusted
        counts["hmac_pass"] = len(matched)

        if covers_core(matched):
            publish_capture(keys_out, store, matched, observed_accounts=observed_accounts)
            error = None
        elif matched:
            error = "coverage_incomplete"
        else:
            if not observations.hook_loaded:
                error = "hook_not_loaded"
            elif trial_killed:
                error = "trial_killed_before_capture"
            else:
                error = "no_passphrase_captured"
    except CaptureError as exc:
        error = exc.code
        matched = []
    finally:
        if proc is not None:
            try:
                actions.terminate(proc)
                trial_killed = True
            except Exception:
                error = 'capture_cleanup_failed'
                trial_killed = False
        if cand is not None:
            from .mac_key_storage import cleanup_hook_capture
            cleanup_hook_capture(cand)
        from .mac_key_storage import cleanup_capture_files
        cleanup_capture_files(config.work_root)

    return CaptureResult(
        error=error,
        matched=matched,
        image_key=None,
        counts=counts,
        wechat_left_running=proc is not None and not trial_killed,
        trial_killed=trial_killed,
    )


class MacCopyInterposeKeySource:
    """Keys from DYLD interpose injection into a temporary WeChat copy (main route)."""

    def __init__(self, config: CaptureConfig, actions=None, store=None, **_ignored):
        self.config = config
        self.actions = actions
        self.store = store

    def candidate_keys(self) -> list[dict]:
        from .mac_capture import SecurityKeychainStore

        actions = self.actions
        store = self.store
        if actions is None or store is None:
            if os.name != "posix":
                raise RuntimeError("macOS interpose capture requires darwin or injected actions")
            actions = actions or DarwinInterposeActions()
            store = store or SecurityKeychainStore()
        result = capture_login_interpose(
            self.config, actions, store, acknowledge_debug_copy=True
        )
        if result.error:
            raise RuntimeError(result.error)
        return [{"key_hex": m["key_hex"], "salt_hex": m["salt_hex"]} for m in result.matched]
