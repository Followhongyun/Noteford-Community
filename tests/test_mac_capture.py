"""Unit tests for copy+LLDB Mac key capture (no live WeChat, no LLDB required)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import struct
import subprocess
import sys
import textwrap
from pathlib import Path, PurePosixPath

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from noteford.crypto import verify_key
from noteford.mac_capture import (
    DB_KEY_ACCOUNT,
    CaptureConfig,
    CaptureError,
    MacCopyLldbKeySource,
    MemoryKeyStore,
    SecurityKeychainStore,
    capture_login,
    codesign_command,
    db_candidates_from_record,
    image_candidates_from_record,
    image_key_opens_probe,
    is_macho,
    mac_salt,
    match_databases,
    official_signature_text_ok,
    page1_hmac_ok,
    select_active_root,
    write_keys_file,
    write_targets_file,
    load_databases,
)
from noteford.mac_entry import build_server, main as mac_entry_main, route_action, route_status
from noteford.mac_resign import (
    DarwinResignActions,
    assess_pipeline,
    codesign_copy_command,
    copy_is_agent,
    iter_app_bundles,
    legacy_copy_paths,
    merged_entitlements_plist,
    prepare_copy,
    restore_original,
    restore_steps,
    shadow_app,
)
from noteford.mac_lldb_worker import (
    derive_enc_key,
    derive_passphrase_keys,
    try_passphrase,
    verified as worker_verified,
)
from noteford.mac_sync import needs_recapture, sync_databases
from noteford.platform_seam import macos_database_key_source as seam_factory

ROOT = Path(__file__).parents[1]
KEY = bytes(range(32))
SALT = bytes(range(16))
OTHER_KEY = bytes(range(32, 64))
OTHER_SALT = bytes(range(16, 32))
IMAGE_KEY = b"0123456789abcdef"
OFFICIAL_SIG = (
    "Authority=Developer ID Application: Tencent Technology (Shenzhen) Company Limited (5A4RE8SF68)\n"
    "TeamIdentifier=5A4RE8SF68\n"
)
ADHOC_SIG = "Signature=adhoc\nflags=0x2(adhoc)\n"


def encrypted_page(key: bytes, salt: bytes) -> bytes:
    page = bytearray(b"\x11" * 4096)
    page[:16] = salt
    digest = hmac.new(
        hashlib.pbkdf2_hmac("sha512", key, mac_salt(salt), 2, dklen=32),
        bytes(page[16:4032]),
        hashlib.sha512,
    )
    digest.update(struct.pack("<I", 1))
    page[4032:] = digest.digest()
    return bytes(page)


def pbkdf_record(*, rounds: int, password: bytes, salt: bytes, derived: bytes, algorithm=2, prf=5) -> dict:
    return {
        "v": 1,
        "kind": "pbkdf",
        "algorithm": algorithm,
        "prf": prf,
        "rounds": rounds,
        "password": password.hex(),
        "salt": salt.hex(),
        "derived": derived.hex(),
    }


def _macho() -> bytes:
    return b"\xfe\xed\xfa\xcf" + b"\x00" * 64


def _write_wechat(app: Path, marker: bytes, version: str = "4.1.15", build: str = "270135") -> None:
    import plistlib

    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    (macos / "WeChat").write_bytes(marker)
    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": "com.tencent.xinWeChat",
                "CFBundleShortVersionString": version,
                "CFBundleVersion": build,
                "CFBundleExecutable": "WeChat",
            },
            handle,
        )


def _write_account(root: Path, account: str, files: dict[str, bytes]) -> None:
    base = root / account
    for rel, data in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _short_work() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp(prefix="nf-mac-"))


class FakePrepare:
    def __init__(self, fail_sign: bool = False):
        self.fail_sign = fail_sign
        self.events: list[str] = []
        self.signed_app: Path | None = None
        self.entitlements = ""

    def signature_text(self, app: Path) -> str:
        return OFFICIAL_SIG

    def require_wechat_quit(self) -> None:
        self.events.append("quit")

    def copy_app(self, source: Path, dest: Path) -> None:
        import shutil

        self.events.append("copy")
        shutil.copytree(source, dest)

    def strip_attributes(self, app: Path) -> None:
        self.events.append("strip")

    def sign_copy(self, app: Path, entitlements: Path) -> None:
        self.events.append("sign")
        self.signed_app = app
        self.entitlements = entitlements.read_text(encoding="utf-8")
        if self.fail_sign:
            raise CaptureError("codesign_failed")
        # Simulate ad-hoc: leave tree as-is.

    def remove_tree(self, path: Path) -> None:
        import shutil

        shutil.rmtree(path, ignore_errors=True)

    def rename(self, source: Path, dest: Path) -> None:
        source.rename(dest)

    def unregister_launch_services(self, app: Path) -> None:
        self.events.append("lsunregister")

    def shadow_still_running(self, shadow: Path) -> bool:
        return False

    def wechat_running(self) -> bool:
        return False

    def launch_exact(self, exe: Path) -> None:
        self.events.append("launch_exact")


class FakeLldb:
    def __init__(self, matched: list[dict], exit_code: int = 0):
        self.matched = matched
        self.exit_code = exit_code
        self.notified = False

    def notify_scan_required(self) -> None:
        self.notified = True

    def run_lldb_worker(self, *, exe, targets, out, timeout, work_root):
        assert exe.is_file()
        assert targets.is_file()
        if self.matched:
            write_keys_file(out, self.matched)
        return {"exit_code": self.exit_code, "trial_killed": True}


def test_rounds_profiles_keep_only_hmac_inputs():
    page = encrypted_page(KEY, SALT)
    assert verify_key(KEY, page)
    mac_key = hashlib.pbkdf2_hmac("sha512", KEY, mac_salt(SALT), 2, dklen=32)
    assert not verify_key(mac_key, page)

    rounds2, name = db_candidates_from_record(
        pbkdf_record(rounds=2, password=KEY, salt=mac_salt(SALT), derived=mac_key),
        {SALT},
    )
    assert name == "rounds_2"
    assert rounds2 == [{"key_hex": KEY.hex(), "salt_hex": SALT.hex(), "source": "pbkdf-rounds-2"}]

    wrong_salt, wrong_name = db_candidates_from_record(
        pbkdf_record(rounds=2, password=KEY, salt=SALT, derived=mac_key),
        {SALT},
    )
    assert wrong_salt == []
    assert wrong_name == "rejected_salt"

    derived, derived_name = db_candidates_from_record(
        pbkdf_record(rounds=256000, password=b"p" * 32, salt=SALT, derived=KEY),
        {SALT},
    )
    assert derived_name == "rounds_256000"
    assert derived[0]["key_hex"] == KEY.hex()


def test_aes_candidates_and_image_probe():
    aes32, name = db_candidates_from_record(
        {"v": 1, "kind": "aes", "algorithm": 0, "key_len": 32, "key": KEY.hex()},
        {SALT},
    )
    assert name == "aes32"
    assert aes32[0]["key_hex"] == KEY.hex()
    assert image_candidates_from_record({"kind": "aes", "algorithm": 0, "key_len": 16, "key": IMAGE_KEY.hex()}) == [
        IMAGE_KEY
    ]
    plain = b"\xff\xd8\xff" + b"\x00" * 13
    encryptor = Cipher(algorithms.AES(IMAGE_KEY), modes.ECB()).encryptor()
    probe = encryptor.update(plain) + encryptor.finalize()
    assert image_key_opens_probe(IMAGE_KEY, probe)


def test_signature_and_codesign_copy_command():
    text = "Authority=Developer ID Application: Tencent Technology (Shenzhen) Company Limited (5A4RE8SF68)"
    assert official_signature_text_ok(text)
    assert not official_signature_text_ok("Signature=adhoc\n" + text)
    command = codesign_command(Path("/tmp/WeChat-trial.app"), Path("/tmp/entitlements.plist"))
    # No --deep: keep nested Tencent sigs intact so the anti-tamper self-check passes.
    assert "--deep" not in command
    assert "--options" not in command
    assert "runtime" not in command
    assert "sudo" not in command
    assert "--generate-entitlement-der" in command
    assert command[command.index("--sign") + 1] == "-"
    copy_cmd = codesign_copy_command(Path("/tmp/WeChat-trial.app"), Path("/tmp/e.plist"))
    assert "--deep" not in copy_cmd
    assert "--generate-entitlement-der" in copy_cmd


def test_prepare_copy_signs_only_shadow(tmp_path: Path):
    official = tmp_path / "Applications" / "WeChat.app"
    _write_wechat(official, _macho())
    state = tmp_path / "keycap"
    actions = FakePrepare()
    result = prepare_copy(official, state, actions, signature_text=OFFICIAL_SIG)
    assert result["state"] == "needs_capture"
    assert result["changed"] is True
    assert actions.events == ["quit", "copy", "strip", "sign"]
    assert actions.signed_app == shadow_app(state)
    assert "com.apple.security.get-task-allow" in actions.entitlements
    assert "runtime" not in actions.entitlements
    # Official binary untouched (still Mach-O at WeChat, no WeChat.bin launcher).
    assert (official / "Contents" / "MacOS" / "WeChat").read_bytes().startswith(b"\xfe\xed\xfa\xcf")
    assert not (official / "Contents" / "MacOS" / "WeChat.bin").exists()
    assert shadow_app(state).is_dir()


def test_prepare_refuses_app_store_and_failed_sign_leaves_no_shadow(tmp_path: Path):
    official = tmp_path / "WeChat.app"
    _write_wechat(official, _macho())
    with pytest.raises(CaptureError) as store:
        prepare_copy(
            official,
            tmp_path / "state",
            FakePrepare(),
            signature_text="Apple Mac OS Application Signing\n",
        )
    assert store.value.code == "app_store_wechat"
    with pytest.raises(CaptureError) as failed:
        prepare_copy(official, tmp_path / "state2", FakePrepare(fail_sign=True), signature_text=OFFICIAL_SIG)
    assert failed.value.code == "codesign_failed"


def test_assess_pipeline_ready_when_keys_present(tmp_path: Path):
    official = tmp_path / "WeChat.app"
    _write_wechat(official, _macho())
    state = tmp_path / "keycap"
    state.mkdir()
    write_keys_file(
        state / "keys.json",
        [{"relative_path": "a/db_storage/session/session.db", "key_hex": KEY.hex(), "salt_hex": SALT.hex()}],
    )
    status = assess_pipeline(official, OFFICIAL_SIG, state)
    assert status["state"] == "ready"
    assert status["keys_present"] is True
    assert status["scan_required"] is False


def test_capture_login_with_fake_lldb_and_hmac(tmp_path: Path):
    official = tmp_path / "WeChat.app"
    _write_wechat(official, _macho())
    state = tmp_path / "keycap"
    prepare_copy(official, state, FakePrepare(), signature_text=OFFICIAL_SIG)
    db_root = tmp_path / "dbs"
    _write_account(
        db_root,
        "acct",
        {
            "db_storage/session/session.db": encrypted_page(KEY, SALT),
            "db_storage/contact/contact.db": encrypted_page(KEY, SALT),
        },
    )
    matched = [
        {
            "relative_path": "acct/db_storage/session/session.db",
            "key_hex": KEY.hex(),
            "salt_hex": SALT.hex(),
            "source": "pbkdf-rounds-2",
        },
        {
            "relative_path": "acct/db_storage/contact/contact.db",
            "key_hex": KEY.hex(),
            "salt_hex": SALT.hex(),
            "source": "pbkdf-rounds-2",
        },
    ]
    store = MemoryKeyStore()
    result = capture_login(
        CaptureConfig(official, state, db_root, 30.0, shadow_app=shadow_app(state)),
        FakeLldb(matched),
        store,
        acknowledge_debug_copy=True,
    )
    assert result.error is None
    assert result.counts["hmac_pass"] == 2
    assert store.get(DB_KEY_ACCOUNT) is not None
    assert KEY.hex() not in json.dumps(result.public_summary())
    assert page1_hmac_ok(KEY.hex(), encrypted_page(KEY, SALT))


def test_capture_requires_ack_and_refuses_without_copy(tmp_path: Path):
    official = tmp_path / "WeChat.app"
    _write_wechat(official, _macho())
    state = tmp_path / "keycap"
    state.mkdir()
    refused = capture_login(
        CaptureConfig(official, state, tmp_path / "empty", 1.0),
        FakeLldb([]),
        MemoryKeyStore(),
        acknowledge_debug_copy=False,
    )
    assert refused.error == "acknowledge_debug_copy_required"
    missing = capture_login(
        CaptureConfig(official, state, tmp_path / "empty", 1.0),
        FakeLldb([]),
        MemoryKeyStore(),
        acknowledge_debug_copy=True,
    )
    assert missing.error == "copy_not_prepared"


def test_incomplete_capture_does_not_claim_success(tmp_path: Path):
    official = tmp_path / "WeChat.app"
    _write_wechat(official, _macho())
    state = tmp_path / "keycap"
    prepare_copy(official, state, FakePrepare(), signature_text=OFFICIAL_SIG)
    db_root = tmp_path / "dbs"
    _write_account(
        db_root,
        "acct",
        {
            "db_storage/session/session.db": encrypted_page(KEY, SALT),
            "db_storage/contact/contact.db": encrypted_page(OTHER_KEY, OTHER_SALT),
        },
    )
    partial = [
        {
            "relative_path": "acct/db_storage/session/session.db",
            "key_hex": KEY.hex(),
            "salt_hex": SALT.hex(),
            "source": "pbkdf-rounds-2",
        }
    ]
    store = MemoryKeyStore()
    result = capture_login(
        CaptureConfig(official, state, db_root, 5.0, shadow_app=shadow_app(state)),
        FakeLldb(partial),
        store,
        acknowledge_debug_copy=True,
    )
    assert result.error == "coverage_incomplete"
    # Incomplete capture cannot persist partial credentials.
    assert not (state / 'keys.partial.json').exists()
    assert not (state / 'keys.pending.json').exists()
    assert store.get(DB_KEY_ACCOUNT) is None


def test_sync_reuses_snapshot_and_requests_recapture_when_hmac_fails(tmp_path: Path):
    db_root = tmp_path / "dbs"
    _write_account(
        db_root,
        "acct",
        {
            "db_storage/session/session.db": encrypted_page(KEY, SALT),
            "db_storage/contact/contact.db": encrypted_page(OTHER_KEY, OTHER_SALT),
        },
    )
    store = MemoryKeyStore()
    store.put(
        DB_KEY_ACCOUNT,
        json.dumps(
            {
                "schema": 1,
                "keys": [
                    {
                        "relative_path": "acct/db_storage/session/session.db",
                        "key_hex": KEY.hex(),
                        "salt_hex": SALT.hex(),
                    }
                ],
            }
        ).encode(),
    )

    def decrypt(src, dest, key):
        dest.write_bytes(b"decrypted")

    def merge(decrypted, wal_path, key):
        return 4

    rows = sync_databases(
        db_root,
        tmp_path / "project",
        store,
        decrypt=decrypt,
        merge_wal=merge,
        check=lambda _path: True,
        settle_seconds=0,
    )
    by_path = {row["relative_path"]: row for row in rows}
    assert by_path["acct/db_storage/session/session.db"]["status"] == "synced"
    assert by_path["acct/db_storage/contact/contact.db"]["status"] == "recapture"
    assert needs_recapture(rows)


def test_active_root_keychain_and_entries(tmp_path: Path):
    stale = tmp_path / "xwechat_files"
    active = tmp_path / "app_data" / "xwechat_files"
    for root in (stale, active):
        _write_account(
            root,
            "acct",
            {
                "db_storage/session/session.db": b"x" * 4096,
                "db_storage/contact/contact.db": b"y" * 4096,
            },
        )
    assert select_active_root([stale, active]) == active
    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "{}\n", "")

    store = SecurityKeychainStore(runner)
    store.put(DB_KEY_ACCOUNT, b"{}")
    assert store.get(DB_KEY_ACCOUNT) == b"{}"
    assert calls[0][0] == "/usr/bin/security"
    assert route_status(platform="win32")["error"] == "windows_key_path_unchanged"
    assert route_action({"action": "prepare"}, platform="linux")["error"] == "darwin_required"
    assert "sudo" in "\n".join(restore_steps())
    assert "已自动删除" in "\n".join(restore_steps())
    assert "csrutil disable" not in "\n".join(restore_steps())
    assert is_macho  # callable imported


def test_platform_seam_factory_and_http_entry():
    isolated = textwrap.dedent(
        """
        import sys
        import noteford.platform_seam
        import noteford.mac_entry
        raise SystemExit("noteford.winmem" in sys.modules)
        """
    )
    completed = subprocess.run([sys.executable, "-c", isolated], check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    work = _short_work()
    try:
        app = work / "WeChat.app"
        _write_wechat(app, _macho())
        config = CaptureConfig(app, work / "state", work / "missing", 0.1)
        (work / "state").mkdir()
        source = seam_factory(config, actions=FakeLldb([]), store=MemoryKeyStore())
        with pytest.raises(RuntimeError, match="acknowledge|copy_not_prepared|databases"):
            # Without ack path through MacCopyLldbKeySource.candidate_keys forces ack=True,
            # so missing copy/db surfaces.
            source.candidate_keys()
        signature = work / "sig.txt"
        signature.write_text(OFFICIAL_SIG, encoding="utf-8")
        assert mac_entry_main(
            ["status", "--app", str(app), "--state", str(work / "state"), "--signature-file", str(signature)]
        ) == 0
        token_file = work / "api_token.json"
        server = build_server("127.0.0.1", 0, token_file, work / "state", app)
        import threading
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        token = json.loads(token_file.read_text(encoding="utf-8"))["token"]
        health = urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=2)
        assert json.loads(health.read())["ok"] is True
        try:
            urlopen(Request(f"http://127.0.0.1:{port}/api/v1/mac/wechat"), timeout=2)
            raise AssertionError("unauthenticated mac status should be rejected")
        except HTTPError as exc:
            assert exc.code == 401
        authed = urlopen(
            Request(f"http://127.0.0.1:{port}/api/v1/mac/wechat", headers={"X-Noteford-Token": token}),
            timeout=2,
        )
        payload = json.loads(authed.read())
        # Windows must preserve its existing key path; other non-Mac hosts reject
        # Mac operations before trying codesign.
        if sys.platform == "win32":
            assert payload == {"ok": False, "error": "windows_key_path_unchanged"}
        else:
            assert "state" in payload or payload.get("error") == "darwin_required"
        server.shutdown()
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)


def test_worker_script_has_no_secret_prints_and_arm64_regs():
    worker = (ROOT / "src" / "noteford" / "mac_lldb_worker.py").read_text(encoding="utf-8")
    assert "CCKeyDerivationPBKDF" in worker
    assert "ccpbkdf2_hmac" in worker
    assert "wechat.dylib" in worker
    assert "InitCipherConfig" in worker
    assert "event_loop" in worker
    assert "SetScriptCallbackFunction" not in worker or "do NOT use SetAutoContinue" in worker or "NOT use SetAutoContinue" in worker
    assert "SetCondition" in worker
    assert "crypto_images" in worker
    assert "probe_hit" in worker
    assert "--self-test" in worker
    assert "256000" in worker
    assert "passphrase" in worker
    assert "derive_passphrase_keys" in worker
    assert "try_passphrase" in worker
    assert "print(key" not in worker
    assert "key_hex" in worker
    assert "sudo" not in worker
    assert "task_for_pid" not in worker


# --- Shared-passphrase offline derivation (the "scan QR once, derive all" path) ---

PASSPHRASE = b"wechat-mac-shared-passphrase-32b"  # 32 bytes, stands in for the real one
SALT_A = bytes(range(16))
SALT_B = bytes(range(16, 32))
SALT_C = bytes(range(32, 48))


def _passphrase_page(passphrase: bytes, salt: bytes) -> bytes:
    """A page whose SQLCipher key is derived from the shared passphrase + its salt."""
    enc_key = derive_enc_key(passphrase, salt)
    return encrypted_page(enc_key, salt)


def test_derive_enc_key_matches_sqlcipher_profile():
    assert len(PASSPHRASE) == 32
    key = derive_enc_key(PASSPHRASE, SALT_A)
    assert len(key) == 32
    # enc_key must verify page-1 HMAC for a page built from that enc_key.
    page = encrypted_page(key, SALT_A)
    assert worker_verified(key, page)
    # Same passphrase, different salt -> different key (per-file derivation).
    assert derive_enc_key(PASSPHRASE, SALT_B) != key


def test_one_passphrase_derives_all_databases():
    rows = [
        ("acct/db_storage/session/session.db", _passphrase_page(PASSPHRASE, SALT_A)),
        ("acct/db_storage/contact/contact.db", _passphrase_page(PASSPHRASE, SALT_B)),
        # A chat/message DB the user never opened — must still be derivable offline.
        ("acct/db_storage/message/message_0.db", _passphrase_page(PASSPHRASE, SALT_C)),
    ]
    derived = derive_passphrase_keys(PASSPHRASE, rows)
    got = {rel for rel, _key, _salt in derived}
    assert got == {rel for rel, _page in rows}
    # Each derived key verifies against its own page and carries its own salt.
    by_rel = {rel: page for rel, page in rows}
    for rel, key, salt in derived:
        assert salt == by_rel[rel][:16]
        assert worker_verified(key, by_rel[rel])


def test_wrong_passphrase_derives_nothing():
    rows = [("acct/db_storage/session/session.db", _passphrase_page(PASSPHRASE, SALT_A))]
    assert derive_passphrase_keys(b"not-the-right-passphrase-32byte!", rows) == []
    # Implausible lengths are rejected outright.
    assert derive_passphrase_keys(b"short", rows) == []


def test_try_passphrase_writes_all_keys_0600(tmp_path: Path, monkeypatch):
    chmod_calls = []
    original_chmod = os.chmod

    def observe_chmod(path, mode):
        chmod_calls.append((Path(path), mode))
        return original_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", observe_chmod)
    rows = [
        ("acct/db_storage/session/session.db", _passphrase_page(PASSPHRASE, SALT_A)),
        ("acct/db_storage/message/message_0.db", _passphrase_page(PASSPHRASE, SALT_B)),
    ]
    found: dict = {}
    out = tmp_path / "keys.json"
    added = try_passphrase(PASSPHRASE, rows, found, "CCKeyDerivationPBKDF-x1-passphrase", out)
    assert added == 2
    assert out.is_file()
    assert (out, 0o600) in chmod_calls
    # Windows chmod does not implement POSIX owner/group permission bits.
    # Retain the actual permission check on the Mac/Linux hosts that support it.
    if os.name == "posix":
        assert (out.stat().st_mode & 0o777) == 0o600
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema"] == 1
    assert {item["relative_path"] for item in payload["keys"]} == {rel for rel, _ in rows}
    for item in payload["keys"]:
        assert page1_hmac_ok(item["key_hex"], dict(rows)[item["relative_path"]])
    # Re-running is idempotent: already-found rows are not duplicated.
    assert try_passphrase(PASSPHRASE, rows, found, "again", out) == 0


# --- merge-式重签 entitlements ---

def test_merged_entitlements_preserves_originals_and_adds_get_task_allow():
    original = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
        '<plist version="1.0"><dict>'
        '<key>com.apple.security.application-groups</key>'
        '<array><string>5A4RE8SF68.com.tencent.xinWeChat</string></array>'
        '<key>com.apple.security.device.camera</key><true/>'
        '</dict></plist>'
    )
    import plistlib

    merged = plistlib.loads(merged_entitlements_plist(original))
    # get-task-allow added (required so the copy is debuggable / DYLD-injectable).
    assert merged["com.apple.security.get-task-allow"] is True
    # WeChat's original entitlements are preserved (merge, not replace).
    assert merged["com.apple.security.device.camera"] is True
    assert merged["com.apple.security.application-groups"] == [
        "5A4RE8SF68.com.tencent.xinWeChat"
    ]
    # Never a Hardened-Runtime option leaking in as text.
    assert b"runtime" not in merged_entitlements_plist(original)


def test_merged_entitlements_falls_back_to_minimal_when_unparseable():
    import plistlib

    for junk in ("", "not xml at all", "\x00\x01garbage"):
        merged = plistlib.loads(merged_entitlements_plist(junk))
        # Unparseable original -> minimal set is still get-task-allow plus the two
        # injection flags the interpose route always needs.
        assert merged == {
            "com.apple.security.get-task-allow": True,
            "com.apple.security.cs.allow-dyld-environment-variables": True,
            "com.apple.security.cs.disable-library-validation": True,
        }


class MergePrepare(FakePrepare):
    """Like FakePrepare but exposes the app's original entitlements for merge-式重签."""

    ORIGINAL = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<plist version="1.0"><dict>'
        '<key>com.apple.security.device.camera</key><true/>'
        '</dict></plist>'
    )

    def original_entitlements(self, app: Path) -> str:
        return self.ORIGINAL


def test_prepare_copy_uses_merge_entitlements(tmp_path: Path):
    import plistlib

    official = tmp_path / "Applications" / "WeChat.app"
    _write_wechat(official, _macho())
    state = tmp_path / "keycap"
    actions = MergePrepare()
    result = prepare_copy(official, state, actions, signature_text=OFFICIAL_SIG)
    assert result["state"] == "needs_capture"
    assert actions.events == ["quit", "copy", "strip", "sign"]
    merged = plistlib.loads(actions.entitlements.encode("utf-8"))
    # Original WeChat entitlement preserved AND get-task-allow added.
    assert merged["com.apple.security.device.camera"] is True
    assert merged["com.apple.security.get-task-allow"] is True


class ExportFailsPrepare(FakePrepare):
    """Real-ish actions whose entitlements export fails — must ABORT, not fall back."""

    def original_entitlements(self, app: Path) -> str:
        raise CaptureError("entitlements_export_failed")


class ExportEmptyPrepare(FakePrepare):
    """Exporter returns unusable/empty content — must ABORT, not silently go minimal."""

    def original_entitlements(self, app: Path) -> str:
        return ""


def test_prepare_copy_aborts_when_export_raises(tmp_path: Path):
    official = tmp_path / "Applications" / "WeChat.app"
    _write_wechat(official, _macho())
    actions = ExportFailsPrepare()
    with pytest.raises(CaptureError) as exc:
        prepare_copy(official, tmp_path / "keycap", actions, signature_text=OFFICIAL_SIG)
    assert exc.value.code == "entitlements_export_failed"
    # Must NOT have reached signing with a minimal fallback.
    assert "sign" not in actions.events


def test_prepare_copy_aborts_when_export_empty(tmp_path: Path):
    official = tmp_path / "Applications" / "WeChat.app"
    _write_wechat(official, _macho())
    actions = ExportEmptyPrepare()
    with pytest.raises(CaptureError) as exc:
        prepare_copy(official, tmp_path / "keycap", actions, signature_text=OFFICIAL_SIG)
    assert exc.value.code == "entitlements_export_failed"
    assert "sign" not in actions.events


def test_interpose_hook_source_present_and_safe():
    src = (ROOT / "native" / "macos" / "key_hook" / "wechat_key_hook.c").read_text(encoding="utf-8")
    assert "__DATA,__interpose" in src
    assert "CCKeyDerivationPBKDF" in src
    assert "CCCryptorCreate" in src
    # Records only to a 0600 file via env var; never prints secrets to stdout.
    assert "WECHAT_KEY_HOOK_OUT" in src
    assert "0600" in src
    # Writes to a file descriptor, never to stdout/stderr.
    assert "snprintf" in src and "write(g_fd" in src
    assert "fprintf(stdout" not in src and "fprintf(stderr" not in src
    # Copy-only posture is documented; no --force --deep replace of the original.
    assert "never resign /Applications/WeChat.app" in src
    # Anti-tamper neutralisation interposes PUBLIC Security.framework funcs only,
    # and deliberately leaves TLS (SecTrust*) untouched.
    assert "SecCodeCopySigningInformation" in src
    assert "SecStaticCodeCheckValidityWithErrors" in src
    # TLS cert validation must NOT be interposed: no replacement fn for SecTrust*.
    assert "nf_SecTrust" not in src


def test_load_core_databases_reads_only_session_and_contact(tmp_path: Path):
    from noteford.mac_capture import load_core_databases

    root = tmp_path / "dbs"
    _write_account(
        root,
        "acct",
        {
            "db_storage/session/session.db": b"s" * 4096,
            "db_storage/contact/contact.db": b"c" * 4096,
            "db_storage/message/message_0.db": b"m" * 4096,
        },
    )
    rels = {row.relative_path for row in load_core_databases(root)}
    assert rels == {
        "acct/db_storage/session/session.db",
        "acct/db_storage/contact/contact.db",
    }


def test_prepare_copy_injects_lsuielement_before_sign(tmp_path: Path):
    import plistlib

    official = tmp_path / "Applications" / "WeChat.app"
    _write_wechat(official, _macho())
    official_before = (official / "Contents" / "Info.plist").read_bytes()
    seen = {}

    class Watching(FakePrepare):
        def sign_copy(self, app: Path, entitlements: Path) -> None:
            with (app / "Contents" / "Info.plist").open("rb") as handle:
                seen["plist"] = plistlib.load(handle)
            super().sign_copy(app, entitlements)

    state = tmp_path / "keycap"
    prepare_copy(official, state, Watching(), signature_text=OFFICIAL_SIG)
    assert seen["plist"]["LSUIElement"] is True
    assert "LSBackgroundOnly" not in seen["plist"]
    assert (official / "Contents" / "Info.plist").read_bytes() == official_before
    assert copy_is_agent(shadow_app(state))
    assert not copy_is_agent(official)


def test_restore_original_deletes_copy_and_legacy_leftovers(tmp_path: Path):
    import plistlib

    official = tmp_path / "Applications" / "WeChat.app"
    _write_wechat(official, _macho())
    state = tmp_path / "home" / "Noteford Community" / "Data" / "keycap"
    actions = FakePrepare()
    prepare_copy(official, state, actions, signature_text=OFFICIAL_SIG)
    shadow = shadow_app(state)
    nested = shadow / "Contents" / "Frameworks" / "WeChatAppEx.app"
    nested.mkdir(parents=True)
    disabled = state / "WeChat-trial.disabled"
    disabled.mkdir()
    (disabled / "old").write_text("stale", encoding="utf-8")
    home = tmp_path / "home"
    old_app = home / "Noteford Community" / "应用" / "WeChat.app"
    old_app.mkdir(parents=True)
    pipeline = home / "Noteford Community" / "应用" / "数据" / "mac-pipeline" / "WeChat-test.app"
    pipeline.mkdir(parents=True)
    removed = []
    real_remove = actions.remove_tree

    def tracking(path: Path) -> None:
        removed.append(path)
        real_remove(path)

    actions.remove_tree = tracking
    actions.events.clear()
    result = restore_original(official, state, actions, relaunch=True, home=home)
    assert result["trial_removed"] is True
    assert result["trial_disabled"] is False
    assert not shadow.exists() and not disabled.exists()
    assert not old_app.exists() and not pipeline.exists()
    assert official.is_dir()
    assert (official / "Contents" / "MacOS" / "WeChat").read_bytes().startswith(b"\xfe\xed\xfa\xcf")
    assert "rename" not in actions.events
    assert all(not str(path).startswith("/Applications") for path in removed)
    assert "launch_exact" in actions.events
    again = restore_original(official, state, actions, relaunch=False, home=home)
    assert again["trial_removed"] is True


class AbsentMacPath(PurePosixPath):
    """Preserve macOS path syntax on every test host without probing real files."""

    def exists(self) -> bool:
        return False


def test_restore_refuses_to_delete_a_running_copy_or_the_official_app(tmp_path: Path, monkeypatch):
    official = tmp_path / "Applications" / "WeChat.app"
    _write_wechat(official, _macho())
    state = tmp_path / "keycap"
    actions = FakePrepare()
    prepare_copy(official, state, actions, signature_text=OFFICIAL_SIG)
    shadow = shadow_app(state)

    class Running(FakePrepare):
        def shadow_still_running(self, _shadow: Path) -> bool:
            return True

    with pytest.raises(CaptureError) as exc:
        restore_original(official, state, Running(), relaunch=False, home=tmp_path)
    assert exc.value.code == "quit_trial_copy_first"
    assert shadow.is_dir()
    monkeypatch.setattr(
        subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not invoke deletion")
    )
    with pytest.raises(CaptureError) as refused:
        DarwinResignActions().remove_tree(AbsentMacPath("/Applications/WeChat.app"))
    assert refused.value.code == "refusing_to_delete_official_app"
    assert official.is_dir()


def test_unregister_walks_nested_apps_before_the_root(tmp_path: Path, monkeypatch):
    import noteford.mac_resign as resign

    shadow = tmp_path / "WeChat-trial.app"
    for name in ("WeChatAppEx.app", "XPlayer.app", "Sparkle Updater.app", "WeChatHelper.app"):
        (shadow / "Contents" / "Frameworks" / name / "Contents").mkdir(parents=True)
    (shadow / "Contents" / "MacOS").mkdir(parents=True)
    names = [path.name for path in iter_app_bundles(shadow)]
    assert names[-1] == "WeChat-trial.app"
    assert names.index("WeChatAppEx.app") < names.index("WeChat-trial.app")
    lsregister = tmp_path / "lsregister"
    lsregister.write_text("x", encoding="utf-8")
    monkeypatch.setattr(resign, "LSREGISTER", str(lsregister))
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(resign.subprocess, "run", fake_run)
    DarwinResignActions().unregister_launch_services(shadow)
    targets = [Path(cmd[2]).name for cmd in calls]
    assert targets[-1] == "WeChat-trial.app"
    assert {"WeChatAppEx.app", "XPlayer.app", "Sparkle Updater.app", "WeChatHelper.app"} <= set(targets)
    assert all(cmd[1] == "-u" for cmd in calls)
    calls.clear()
    DarwinResignActions().unregister_launch_services(AbsentMacPath("/Applications/WeChat.app"))
    assert calls == []
    home = tmp_path / "home"
    state = home / "Noteford Community" / "Data" / "keycap"
    state.mkdir(parents=True)
    assert all(not str(path).startswith("/Applications") for path in legacy_copy_paths(state, home=home))
    from noteford.mac_resign import activate_scan_script

    script = activate_scan_script(7)
    assert "unix id is 7" in script and "click" not in script.lower()
    assert "LSBackgroundOnly" not in script


def test_database_discovery_does_not_drop_second_account_after_forty_files(tmp_path):
    from noteford.mac_capture import load_databases
    for account in ('first', 'second'):
        files = {'db_storage/session/session.db': b's' * 4096,
                 'db_storage/contact/contact.db': b'c' * 4096}
        files.update({f'db_storage/message/message_{i}.db': b'm' * 4096 for i in range(25)})
        _write_account(tmp_path, account, files)
    rows = load_databases(tmp_path)
    assert len(rows) == 54
    assert len([row for row in rows if row.relative_path.startswith('second/')]) == 27


def test_capture_restores_other_account_keys_from_keychain(tmp_path):
    from noteford.mac_capture import MemoryKeyStore, publish_capture, load_keys_file, _store_capture
    store = MemoryKeyStore()
    old = {'relative_path': 'old/session/session.db', 'key_hex': 'aa' * 32}
    new = {'relative_path': 'new/session/session.db', 'key_hex': 'bb' * 32}
    _store_capture(store, [old])
    publish_capture(tmp_path / 'keys.json', store, [new])
    assert not (tmp_path / 'keys.json').exists()
    assert json.loads(store.get(DB_KEY_ACCOUNT))['keys'] == [old, new]


def test_official_relaunch_uses_launch_services_exact_app(monkeypatch):
    from noteford.mac_resign import DarwinResignActions
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(subprocess, 'run', run)
    DarwinResignActions().launch_exact(Path('/Applications/WeChat.app/Contents/MacOS/WeChat'))
    assert calls == [['/usr/bin/open', '-a', '/Applications/WeChat.app']]


def test_temporary_process_cleanup_includes_helpers_but_never_official(monkeypatch, tmp_path):
    import psutil
    from noteford.mac_resign import DarwinResignActions
    shadow = tmp_path / 'WeChat-trial.app'
    stopped = []
    class Process:
        def __init__(self, exe): self.info = {'exe': exe}
        def terminate(self): stopped.append(self.info['exe'])
    paths = [str(shadow / 'Contents/MacOS/WeChat'),
             str(shadow / 'Contents/MacOS/Helper.app/Contents/MacOS/Helper'),
             '/Applications/WeChat.app/Contents/MacOS/WeChat',
             str(tmp_path / 'WeChat-trial.app-other/Contents/MacOS/WeChat')]
    monkeypatch.setattr(psutil, 'process_iter', lambda fields: [Process(p) for p in paths])
    monkeypatch.setattr(psutil, 'wait_procs', lambda processes, timeout: (processes, []))
    DarwinResignActions().stop_shadow_processes(shadow)
    assert stopped == paths[:2]
