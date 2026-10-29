"""Tests for the DYLD interpose key-capture route (mac_interpose)."""

from __future__ import annotations

import hashlib
import hmac
import json
import plistlib
import struct
from pathlib import Path

import pytest

from noteford.mac_capture import CaptureConfig, CaptureError, mac_salt
from noteford.mac_interpose import (
    DarwinInterposeActions,
    HookObservations,
    build_hook,
    capture_login_interpose,
    derive_matches,
    parse_candidates,
)
from noteford.mac_resign import merged_entitlements_plist
from noteford.mac_lldb_worker import derive_enc_key

ROUNDS_PASSPHRASE = 256000
PASSPHRASE = bytes(range(100, 132))  # 32 bytes
SALT_A = bytes(range(16))
SALT_B = bytes(range(16, 32))


def encrypted_page(enc_key: bytes, salt: bytes) -> bytes:
    page = bytearray(b"\x11" * 4096)
    page[:16] = salt
    digest = hmac.new(
        hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt(salt), 2, dklen=32),
        bytes(page[16:4032]),
        hashlib.sha512,
    )
    digest.update(struct.pack("<I", 1))
    page[4032:] = digest.digest()
    return bytes(page)


def passphrase_page(passphrase: bytes, salt: bytes) -> bytes:
    return encrypted_page(derive_enc_key(passphrase, salt), salt)


# ---------- parse_candidates ----------

def test_parse_candidates_reads_marker_and_dedups():
    pp = PASSPHRASE.hex()
    text = "\n".join([
        json.dumps({"v": 1, "kind": "hook_loaded", "pid": 7}),
        json.dumps({"v": 1, "kind": "pbkdf_in", "len": 32, "rounds": 256000, "salt": "", "hex": pp}),
        json.dumps({"v": 1, "kind": "pbkdf_in", "len": 32, "rounds": 256000, "salt": "", "hex": pp}),
        json.dumps({"v": 1, "kind": "pbkdf_out", "len": 32, "rounds": 256000, "salt": "", "hex": SALT_A.hex() * 2}),
        json.dumps({"v": 1, "kind": "aes_key", "len": 32, "rounds": 0, "salt": "", "hex": SALT_B.hex() * 2}),
        "garbage not json",
        json.dumps({"v": 1, "kind": "pbkdf_in", "len": 8, "rounds": 1, "salt": "", "hex": "aabb"}),
    ])
    obs = parse_candidates(text)
    assert obs.hook_loaded is True
    assert obs.passphrases == [PASSPHRASE]  # deduped, 8-byte one ignored
    assert len(obs.direct_keys) == 2


def test_parse_candidates_no_marker():
    obs = parse_candidates(json.dumps({"v": 1, "kind": "pbkdf_in", "hex": PASSPHRASE.hex(), "len": 32, "rounds": 256000, "salt": ""}))
    assert obs.hook_loaded is False
    assert obs.passphrases == [PASSPHRASE]


def test_mac_hmac_inputs_are_not_expensive_login_passphrases():
    text = "\n".join([
        json.dumps({"v": 1, "kind": "pbkdf_in", "len": 32, "rounds": 2, "hex": PASSPHRASE.hex()}),
        json.dumps({"v": 1, "kind": "pbkdf_in", "len": 32, "rounds": 256000, "hex": (SALT_A * 2).hex()}),
    ])
    obs = parse_candidates(text)
    assert obs.passphrases == [SALT_A * 2]
    assert obs.direct_keys == [PASSPHRASE]


# ---------- derive_matches ----------

class DB:
    def __init__(self, relative_path, page1):
        self.relative_path = relative_path
        self.page1 = page1
        self.salt = page1[:16]
        self.path = Path("/dev/null")


def test_derive_matches_passphrase_covers_all():
    dbs = [
        DB("acct/db_storage/session/session.db", passphrase_page(PASSPHRASE, SALT_A)),
        DB("acct/db_storage/contact/contact.db", passphrase_page(PASSPHRASE, SALT_B)),
    ]
    obs = HookObservations(True, [PASSPHRASE], [], 3)
    matched = derive_matches(obs, dbs)
    assert len(matched) == 2
    assert all(m["source"] == "interpose-passphrase" for m in matched)
    for m, db in zip(matched, dbs):
        assert m["key_hex"] == derive_enc_key(PASSPHRASE, db.salt).hex()


def test_derive_matches_direct_key_fallback():
    enc = derive_enc_key(PASSPHRASE, SALT_A)
    db = DB("acct/db_storage/session/session.db", encrypted_page(enc, SALT_A))
    obs = HookObservations(True, [], [enc], 1)
    matched = derive_matches(obs, [db])
    assert len(matched) == 1
    assert matched[0]["source"] == "interpose-direct-key"
    assert matched[0]["key_hex"] == enc.hex()


def test_derive_matches_wrong_passphrase_nothing():
    db = DB("acct/db_storage/session/session.db", passphrase_page(PASSPHRASE, SALT_A))
    obs = HookObservations(True, [bytes(range(1, 33))], [], 1)
    assert derive_matches(obs, [db]) == []


# ---------- merged entitlements ----------

def test_merged_entitlements_has_injection_flags():
    original = plistlib.dumps({
        "com.apple.security.app-sandbox": True,
        "com.apple.security.application-groups": ["5A4RE8SF68.com.tencent.xinWeChat"],
    }).decode("utf-8")
    merged = plistlib.loads(merged_entitlements_plist(original))
    assert merged["com.apple.security.get-task-allow"] is True
    assert merged["com.apple.security.cs.allow-dyld-environment-variables"] is True
    assert merged["com.apple.security.cs.disable-library-validation"] is True
    # original entitlements preserved
    assert merged["com.apple.security.app-sandbox"] is True
    assert merged["com.apple.security.application-groups"] == ["5A4RE8SF68.com.tencent.xinWeChat"]


# ---------- build_hook ----------

def test_build_hook_success(tmp_path: Path):
    src = tmp_path / "hook.c"
    src.write_text("int x;\n", encoding="utf-8")
    calls = []

    class R:
        returncode = 0

    def runner(cmd, **kw):
        calls.append(cmd)
        if "-o" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"\xcf\xfa\xed\xfe")
        return R()

    out = build_hook(tmp_path, source=src, runner=runner)
    assert out.is_file()
    assert "-dynamiclib" in calls[0] and "arm64" in calls[0]
    # hook now interposes Security.framework self-check funcs, so it must link Security
    assert "Security" in calls[0] and "CoreFoundation" in calls[0]
    # arm64 SIGKILLs an unsigned dylib on insert, so build_hook must ad-hoc sign it
    assert any(c[:2] == ["/usr/bin/codesign", "--force"] and "--sign" in c for c in calls)


def test_build_hook_failure(tmp_path: Path):
    src = tmp_path / "hook.c"
    src.write_text("int x;\n", encoding="utf-8")

    class R:
        returncode = 1

    with pytest.raises(CaptureError) as exc:
        build_hook(tmp_path, source=src, runner=lambda *a, **k: R())
    assert exc.value.code == "hook_build_failed"



def test_build_hook_sign_failure(tmp_path: Path):
    src = tmp_path / "hook.c"
    src.write_text("int x;\n", encoding="utf-8")

    def runner(cmd, **kw):
        if "-o" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"\xcf\xfa\xed\xfe")
            class Ok:
                returncode = 0
            return Ok()
        class Fail:  # codesign fails
            returncode = 1
        return Fail()

    with pytest.raises(CaptureError) as exc:
        build_hook(tmp_path, source=src, runner=runner)
    assert exc.value.code == "hook_sign_failed"


def test_build_hook_missing_source(tmp_path: Path):
    with pytest.raises(CaptureError) as exc:
        build_hook(tmp_path, source=tmp_path / "nope.c")
    assert exc.value.code == "hook_source_missing"


def test_frozen_hook_needs_no_source_compiler_or_runtime_signing(tmp_path, monkeypatch):
    import noteford.mac_interpose as interpose

    bundled = tmp_path / "_internal" / "mac-native" / interpose.HOOK_DYLIB_NAME
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"packaged-signed-arm64-hook")
    monkeypatch.setattr(interpose.sys, "frozen", True, raising=False)
    monkeypatch.setattr(interpose.sys, "_MEIPASS", str(bundled.parent.parent), raising=False)
    out = build_hook(tmp_path / "state", runner=lambda *a, **k: pytest.fail("installed app must not compile"))
    assert out.read_bytes() == bundled.read_bytes()
    bundled.unlink()
    with pytest.raises(CaptureError) as exc:
        build_hook(tmp_path / "state")
    assert exc.value.code == "hook_binary_missing"


# ---------- capture_login_interpose end-to-end (fake actions) ----------

class FakeProc:
    def __init__(self):
        self._alive = True

    def poll(self):
        return None if self._alive else 0


class FakeInterposeActions:
    """Simulates: hook loads, writes passphrase to candidate file on launch."""

    def __init__(self, candidate_text: str, *, loaded_ok: bool = True):
        self.candidate_text = candidate_text
        self.loaded_ok = loaded_ok
        self.events = []

    def notify_scan_required(self):
        self.events.append("notify")

    def install_hook_into_bundle(self, shadow, hook):
        self.events.append("install")
        dest = shadow / "Contents" / "Frameworks" / "libwechat_key_hook.dylib"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\xcf\xfa\xed\xfe")
        return dest

    def launch_with_hook(self, *, exe, hook, out_file):
        self.events.append("launch")
        if self.loaded_ok:
            out_file.write_text(self.candidate_text, encoding="utf-8")
        return FakeProc()

    def process_alive(self, proc):
        return proc.poll() is None

    def terminate(self, proc):
        proc._alive = False
        self.events.append("terminate")


def _make_copy_and_dbs(tmp_path: Path):
    shadow = tmp_path / "WeChat-trial.app"
    exe = shadow / "Contents" / "MacOS" / "WeChat"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"\xcf\xfa\xed\xfe")
    official = tmp_path / "Applications" / "WeChat.app"
    (official / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
    (official / "Contents" / "MacOS" / "WeChat").write_bytes(b"\xcf\xfa\xed\xfe")
    db_root = tmp_path / "xwechat_files"
    acct = db_root / "acct" / "db_storage"
    for rel, salt in [("session/session.db", SALT_A), ("contact/contact.db", SALT_B)]:
        p = acct / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(passphrase_page(PASSPHRASE, salt))
    return official, shadow, db_root


class MemStore:
    def __init__(self):
        self.items = {}

    def put(self, a, b):
        self.items[a] = b

    def get(self, a):
        return self.items.get(a)


def test_capture_nudges_the_scan_window_by_pid(tmp_path: Path):
    from noteford.mac_resign import activate_scan_script

    official, shadow, db_root = _make_copy_and_dbs(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    hook = work / "libwechat_key_hook.dylib"
    hook.write_bytes(b"\xcf\xfa\xed\xfe")
    cand = "\n".join([
        json.dumps({"v": 1, "kind": "hook_loaded", "pid": 1}),
        json.dumps({"v": 1, "kind": "pbkdf_in", "len": 32, "rounds": 256000, "salt": "", "hex": PASSPHRASE.hex()}),
    ])

    class Actions(FakeInterposeActions):
        def launch_with_hook(self, *, exe, hook, out_file):
            proc = super().launch_with_hook(exe=exe, hook=hook, out_file=out_file)
            proc.pid = 4242
            return proc

        def nudge_scan_window(self, exe, pid=None):
            self.events.append(("nudge", pid, exe.name))

    actions = Actions(cand)
    config = CaptureConfig(official_app=official, work_root=work, db_root=db_root, timeout_seconds=5, shadow_app=shadow)
    result = capture_login_interpose(config, actions, MemStore(), acknowledge_debug_copy=True, poll_interval=0.01)
    assert result.error is None
    assert ("nudge", 4242, "WeChat") in actions.events
    script = activate_scan_script(4242)
    assert "click" not in script.lower()
    assert "unix id is 4242" in script


def test_capture_login_interpose_success(tmp_path: Path, monkeypatch):
    import noteford.mac_interpose as interpose
    original_derive = interpose.derive_matches
    calls = []
    phases = []
    def derive(*args):
        assert phases == [2]  # Progress changes before expensive validation.
        calls.append(1)
        return original_derive(*args)
    monkeypatch.setattr(interpose, "derive_matches", derive)
    official, shadow, db_root = _make_copy_and_dbs(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    hook = work / "libwechat_key_hook.dylib"
    hook.write_bytes(b"\xcf\xfa\xed\xfe")
    cand = "\n".join([
        json.dumps({"v": 1, "kind": "hook_loaded", "pid": 1}),
        json.dumps({"v": 1, "kind": "pbkdf_in", "len": 32, "rounds": 256000, "salt": "", "hex": PASSPHRASE.hex()}),
    ])
    config = CaptureConfig(official_app=official, work_root=work, db_root=db_root, timeout_seconds=5, shadow_app=shadow)
    actions = FakeInterposeActions(cand)
    store = MemStore()
    result = capture_login_interpose(config, actions, store, acknowledge_debug_copy=True, poll_interval=0.01, on_verifying=phases.append)
    assert result.error is None, result.error
    assert len(result.matched) == 2
    assert result.counts["hook_loaded"] == 1
    assert not (work / "keys.json").exists()
    assert store.get("noteford-wechat-db-keys") is not None
    assert "install" in actions.events and "launch" in actions.events
    assert calls == [1]  # No duplicate final PBKDF pass after core success.
    assert json.loads(store.get("noteford-wechat-db-keys"))["observed_accounts"] == ["acct"]


def test_capture_login_interpose_hook_not_loaded(tmp_path: Path):
    official, shadow, db_root = _make_copy_and_dbs(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    (work / "libwechat_key_hook.dylib").write_bytes(b"\xcf\xfa\xed\xfe")
    config = CaptureConfig(official_app=official, work_root=work, db_root=db_root, timeout_seconds=1, shadow_app=shadow)
    actions = FakeInterposeActions("", loaded_ok=False)
    result = capture_login_interpose(config, actions, MemStore(), acknowledge_debug_copy=True, poll_interval=0.01)
    assert result.error == "hook_not_loaded"
    assert not (work / "keys.json").is_file()


def test_capture_login_interpose_requires_ack(tmp_path: Path):
    official, shadow, db_root = _make_copy_and_dbs(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    config = CaptureConfig(official_app=official, work_root=work, db_root=db_root, timeout_seconds=1, shadow_app=shadow)
    result = capture_login_interpose(config, FakeInterposeActions(""), MemStore(), acknowledge_debug_copy=False)
    assert result.error == "acknowledge_debug_copy_required"


def test_capture_login_interpose_missing_hook_dylib(tmp_path: Path):
    official, shadow, db_root = _make_copy_and_dbs(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    config = CaptureConfig(official_app=official, work_root=work, db_root=db_root, timeout_seconds=1, shadow_app=shadow)
    result = capture_login_interpose(config, FakeInterposeActions(""), MemStore(), acknowledge_debug_copy=True, poll_interval=0.01)
    assert result.error == "hook_dylib_missing"


def test_sandbox_candidates_path_is_inside_container():
    from pathlib import Path
    from noteford.mac_interpose import sandbox_candidates_path, CANDIDATES_NAME
    db_root = Path("/Users/x/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files")
    out = sandbox_candidates_path(db_root)
    # must live under the container Data dir so a sandboxed copy can write it
    assert out.as_posix().startswith("/Users/x/Library/Containers/com.tencent.xinWeChat/Data/")
    assert out.name == CANDIDATES_NAME
    assert "Library/Caches" in out.as_posix()


@pytest.mark.parametrize('scan_account', ['acct', 'wrong'])
def test_adding_account_preserves_existing_keys_even_when_scan_fails(tmp_path, scan_account):
    from noteford.mac_capture import write_keys_file, load_keys_file, _store_capture
    official, shadow, db_root = _make_copy_and_dbs(tmp_path)
    work = tmp_path / 'work'
    work.mkdir()
    (work / 'libwechat_key_hook.dylib').write_bytes(b'hook')
    old = {'relative_path': 'previous/db_storage/session/session.db', 'key_hex': 'ab' * 32, 'salt_hex': 'cd' * 16, 'source': 'test'}
    write_keys_file(work / 'keys.json', [old])
    store = MemStore()
    _store_capture(store, [old])
    cand = json.dumps({'v': 1, 'kind': 'pbkdf_in', 'rounds': 256000, 'hex': PASSPHRASE.hex()})
    config = CaptureConfig(official_app=official, work_root=work, db_root=db_root,
                           timeout_seconds=.1, shadow_app=shadow, account_prefix=scan_account)
    result = capture_login_interpose(config, FakeInterposeActions(cand), store, acknowledge_debug_copy=True, force=True, poll_interval=.01)
    from noteford.mac_key_storage import database_payload
    keys = database_payload(work / 'keys.json', store)['keys']
    assert old in keys
    assert old in json.loads(store.get('noteford-wechat-db-keys'))['keys']
    if scan_account == 'acct':
        assert result.error is None
        assert len(keys) == 3
    else:
        assert result.error
        assert keys == [old]


def test_scan_of_another_existing_account_is_rejected_without_replacing_keys(tmp_path):
    from noteford.mac_capture import write_keys_file, load_keys_file
    official, shadow, db_root = _make_copy_and_dbs(tmp_path)
    work = tmp_path / 'work'; work.mkdir()
    (work / 'libwechat_key_hook.dylib').write_bytes(b'hook')
    for category, salt in [('session', SALT_A), ('contact', SALT_B)]:
        db = db_root / 'target' / 'db_storage' / category / f'{category}.db'
        db.parent.mkdir(parents=True)
        db.write_bytes(passphrase_page(bytes(range(32)), salt))
    old = {'relative_path': 'acct/db_storage/session/session.db', 'key_hex': 'ab' * 32, 'salt_hex': 'cd' * 16}
    write_keys_file(work / 'keys.json', [old])
    before = (work / 'keys.json').read_bytes()
    cand = json.dumps({'v': 1, 'kind': 'pbkdf_in', 'rounds': 256000, 'hex': PASSPHRASE.hex()})
    config = CaptureConfig(official_app=official, work_root=work, db_root=db_root,
                           timeout_seconds=5, shadow_app=shadow, account_prefix='target')
    store = MemStore()
    result = capture_login_interpose(config, FakeInterposeActions(cand), store, acknowledge_debug_copy=True, force=True, poll_interval=.01)
    assert result.error == 'wrong_account'
    assert result.matched == []
    from noteford.mac_key_storage import database_payload
    assert database_payload(work / 'keys.json', store)['keys'] == json.loads(before)['keys']


@pytest.mark.parametrize('login', ['new_account', None, 'cancelled'])
def test_add_requires_live_copy_login_and_loads_new_databases(tmp_path, login):
    from noteford.mac_capture import write_keys_file, load_keys_file
    official, shadow, db_root = _make_copy_and_dbs(tmp_path)
    work = tmp_path / 'work'
    work.mkdir()
    (work / 'libwechat_key_hook.dylib').write_bytes(b'hook')
    old = {'relative_path': 'previous/db_storage/session/session.db', 'key_hex': 'ab' * 32, 'salt_hex': 'cd' * 16, 'source': 'test'}
    write_keys_file(work / 'keys.json', [old])
    cand = '\n'.join([
        json.dumps({'v': 1, 'kind': 'hook_loaded', 'pid': 1}),
        json.dumps({'v': 1, 'kind': 'pbkdf_in', 'rounds': 256000, 'hex': PASSPHRASE.hex()}),
    ])
    class Actions(FakeInterposeActions):
        def logged_in_account(self, exe, root):
            if login != 'new_account':
                return None
            # First login creates a source that did not exist when capture began.
            for rel, salt in [('session/session.db', SALT_A), ('contact/contact.db', SALT_B)]:
                target = root / login / 'db_storage' / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(passphrase_page(PASSPHRASE, salt))
            return login
    config = CaptureConfig(official_app=official, work_root=work, db_root=db_root,
                           timeout_seconds=.1, shadow_app=shadow, require_live_login=True,
                           is_cancelled=lambda: login == 'cancelled')
    store = MemStore()
    result = capture_login_interpose(config, Actions(cand), store, acknowledge_debug_copy=True,
                                     force=True, poll_interval=.01)
    from noteford.mac_key_storage import database_payload
    keys = database_payload(work / 'keys.json', store)['keys']
    assert old in keys
    if login == 'new_account':
        assert result.error is None
        assert {row['relative_path'].split('/')[0] for row in result.matched} == {'new_account'}
        assert len(keys) == 3  # Existing on-disk acct is never accepted as this login.
    else:
        assert result.error == ('cancelled' if login else 'no_passphrase_captured')
        assert keys == [old]


def test_copy_login_evidence_ignores_official_app_and_helpers(tmp_path, monkeypatch):
    from types import SimpleNamespace
    official, shadow, root = _make_copy_and_dbs(tmp_path)
    files = [SimpleNamespace(path=str(p)) for p in (root / 'acct' / 'db_storage').rglob('*.db')]
    exe = shadow / 'Contents' / 'MacOS' / 'WeChat'
    def process(path, cmdline=()):
        return SimpleNamespace(info={'exe': str(path), 'cmdline': cmdline}, open_files=lambda: files)
    entries = [process(official / 'Contents' / 'MacOS' / 'WeChat'), process(exe, ['--type=utility'])]
    monkeypatch.setattr('psutil.process_iter', lambda *args, **kwargs: entries)
    actions = DarwinInterposeActions()
    assert actions.logged_in_account(exe, root) is None
    entries.append(process(exe))
    assert actions.logged_in_account(exe, root) == 'acct'


@pytest.mark.parametrize('failure', [RuntimeError, KeyboardInterrupt])
def test_interpose_exception_removes_hook_output_and_directory(tmp_path, failure):
    official, shadow, db_root = _make_copy_and_dbs(tmp_path)
    work = tmp_path / 'work'
    work.mkdir()
    (work / 'libwechat_key_hook.dylib').write_bytes(b'fake')
    class Actions(FakeInterposeActions):
        def launch_with_hook(self, **kwargs):
            self.output = kwargs['out_file']
            return super().launch_with_hook(**kwargs)
        def process_alive(self, proc):
            raise failure()
    actions = Actions('synthetic only')
    config = CaptureConfig(official, work, db_root, shadow_app=shadow)
    with pytest.raises(failure):
        capture_login_interpose(config, actions, MemStore(), acknowledge_debug_copy=True)
    assert 'terminate' in actions.events
    assert not actions.output.exists()
    assert not actions.output.parent.exists()


def test_terminate_stops_inherited_writer_child(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    import time
    import psutil
    pidfile = tmp_path / 'child.pid'
    script = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); open(sys.argv[1],'w').write(str(p.pid)); time.sleep(60)"
    proc = subprocess.Popen([sys.executable, '-c', script, str(pidfile)], start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        child = psutil.Process(int(pidfile.read_text()))
        DarwinInterposeActions().terminate(proc)
        deadline = time.monotonic() + 5
        while child.is_running() and child.status() != psutil.STATUS_ZOMBIE and time.monotonic() < deadline:
            time.sleep(.01)
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=5)
