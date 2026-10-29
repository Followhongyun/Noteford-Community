import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from noteford.platform_seam import (
    ImportedKeySource,
    MacKeychainSecretProtector,
    MacMemoryKeySource,
    default_data_root,
    derive_sqlcipher4_raw_key,
    secret_backend,
    service_executable_name,
    wechat_process_names,
    wechat_search_roots,
    writer_lock_backend,
)

ROOT = Path(__file__).parents[1]
PROBE = ROOT / "scripts" / "mac-probe" / "probe.py"
CLASSIFY = ROOT / "scripts" / "mac-probe" / "classify.py"


def _classify():
    spec = importlib.util.spec_from_file_location("mac_probe_classify", CLASSIFY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_database_header_classes():
    classify = _classify()
    encrypted = classify.classify_database_header(b"\x01" * 32, 8192)
    assert encrypted["classification"] == "possible_sqlcipher4_page4096"
    assert encrypted["salt_hex"] == ("01" * 16)
    plaintext = classify.classify_database_header(b"SQLite format 3\x00" + b"\x00" * 16, 4096)
    assert plaintext["classification"] == "sqlite_plaintext"
    assert plaintext["salt_hex"] is None
    legacy = classify.classify_database_header(b"\x02" * 16, 2048)
    assert legacy["classification"] == "possible_sqlcipher3_page1024"
    assert classify.classify_wal_header(b"\x37\x7f\x06\x83" + b"\x00" * 28, 32)["classification"] == "sqlite_wal_header"
    assert classify.classify_dat_header(b"\x07\x08V2\x08\x07", 40)["classification"] == "wechat_v2"
    assert classify.classify_dat_header(b"\x07\x08V1\x08\x07", 40)["classification"] == "wechat_v1"


def test_probe_self_test_and_guards():
    source = PROBE.read_text(encoding="utf-8")
    assert "mach_vm_read" not in source
    assert "ReadProcessMemory" not in source
    assert "--sign" not in source
    completed = subprocess.run([sys.executable, str(PROBE), "--self-test"], check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    refused = subprocess.run(
        [sys.executable, str(PROBE), "--output", "/tmp/noteford-mac-probe-should-not-exist"],
        check=False,
        capture_output=True,
        text=True,
    )
    if sys.platform == "darwin":
        assert refused.returncode == 0
    else:
        assert refused.returncode == 2
        assert not Path("/tmp/noteford-mac-probe-should-not-exist").exists()


def test_platform_seam_names_and_import():
    assert service_executable_name("win32") == "noteford-service.exe"
    assert service_executable_name("darwin") == "noteford-service"
    assert wechat_process_names("win32") == ("Weixin.exe",)
    assert "WeChat" in wechat_process_names("darwin")
    assert writer_lock_backend("win32") == "msvcrt"
    assert writer_lock_backend("darwin") == "fcntl"
    assert secret_backend("win32") == "dpapi"
    assert secret_backend("darwin") == "keychain"
    home = Path("/Users/example")
    assert default_data_root("darwin", home=home) == home / "Library" / "Application Support" / "Noteford Community"
    assert default_data_root("win32", home=home, local_app_data=Path("C:/Users/example/AppData/Local")).name == "Noteford Community"
    roots = wechat_search_roots("darwin", home=home)
    assert roots[0] == home / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/xwechat_files"
    assert any(part == "xwechat_files" for root in roots for part in root.parts)
    isolated = subprocess.run(
        [sys.executable, "-c", "import sys; import noteford.platform_seam; raise SystemExit('noteford.winmem' in sys.modules)"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert isolated.returncode == 0, isolated.stderr
    with pytest.raises(NotImplementedError):
        MacMemoryKeySource().candidate_keys()
    assert MacKeychainSecretProtector.service == "com.followhongyun.noteford.community"
    assert MacKeychainSecretProtector.account == "noteford-data-key"
    assert MacKeychainSecretProtector.accessible == "kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly"
    if sys.platform != "darwin":
        with pytest.raises(OSError):
            MacKeychainSecretProtector().wrap(b"k" * 32)


def test_imported_key_and_passphrase_derivation():
    source = ImportedKeySource([{"key_hex": "ab" * 32, "salt_hex": "cd" * 16}])
    assert source.candidate_keys() == [{"key_hex": "ab" * 32, "salt_hex": "cd" * 16}]
    with pytest.raises(ValueError):
        ImportedKeySource([{"key_hex": "aa"}])
    passphrase = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
    salt = bytes.fromhex("00112233445566778899aabbccddeeff")
    assert derive_sqlcipher4_raw_key(passphrase, salt).hex() == (
        "0ff2eb85671c24f4103c490ed634e91669cd3415a4e4af68d09dfc6f4eb4d0c1"
    )
