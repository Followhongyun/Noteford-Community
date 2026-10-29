"""Shared modules import on non-Windows, and key access stays on the seam."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from noteford.platform_seam import (
    MacMemoryKeySource,
    PlatformCapabilityError,
    WindowsMemoryKeySource,
    acquire_database_key_candidates,
)
from noteford.winmem import Candidate, UnsupportedPlatform, save_candidates


def test_shared_modules_import_without_winmem():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                [
                    "import sys",
                    "import noteford.service",
                    "import noteford.mcp_server",
                    "import noteford.history_read",
                    "import noteford.refresh",
                    "import noteford.discovery",
                    "import noteford.image_crypto",
                    "import noteford.cdn_session",
                    "import noteford.cli",
                    "import noteford.platform_seam",
                    "import noteford.mac_entry",
                    "raise SystemExit('noteford.winmem' in sys.modules)",
                ]
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_windows_memory_entry_points_fail_clearly_off_windows():
    if sys.platform == "win32":
        pytest.skip("Windows runs the real memory scan")
    from noteford.winmem import ProcessReader, list_weixin_processes, scan_config_cipher_candidates

    with pytest.raises(UnsupportedPlatform, match="Windows memory scan"):
        scan_config_cipher_candidates()
    with pytest.raises(UnsupportedPlatform, match="Windows memory scan"):
        list_weixin_processes()
    with pytest.raises(UnsupportedPlatform, match="Windows memory scan"):
        ProcessReader(1)


def test_refused_scan_does_not_call_windll(monkeypatch):
    if sys.platform == "win32":
        pytest.skip("Windows binds kernel32 on first use")
    import ctypes

    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: pytest.fail("WinDLL"), raising=False)
    from noteford.winmem import scan_config_cipher_candidates

    with pytest.raises(UnsupportedPlatform):
        scan_config_cipher_candidates()


def test_save_candidates_schema_is_unchanged(tmp_path: Path):
    path = tmp_path / "secrets" / "candidate_keys.json"
    item = Candidate(pid=4, key_hex="ab" * 32, salt_hex="cd" * 16)
    save_candidates(path, [item])
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == 1
    assert payload["source"] == "read-only Weixin.exe Config.Cipher memory scan"
    assert payload["candidates"] == [
        {
            "pid": 4,
            "key_hex": "ab" * 32,
            "salt_hex": "cd" * 16,
            "fingerprint": item.fingerprint,
        }
    ]


def test_darwin_reads_keys_file_and_does_not_scan(tmp_path: Path, monkeypatch):
    keys = tmp_path / "keys.json"
    keys.write_text(
        json.dumps({"schema": 1, "keys": [{"relative_path": "account/db_storage/session/session.db", "key_hex": "ab" * 32, "salt_hex": "cd" * 16}]}),
        encoding="utf-8",
    )
    import noteford.winmem as winmem

    monkeypatch.setattr(winmem, "scan_config_cipher_candidates", lambda: pytest.fail("memory scan"))
    rows = acquire_database_key_candidates(platform="darwin", keys_file=keys)
    assert rows == [{"relative_path": "account/db_storage/session/session.db", "key_hex": "ab" * 32, "salt_hex": "cd" * 16}]


def test_darwin_without_keys_explains_the_interpose_path(tmp_path: Path):
    missing = tmp_path / "keys.json"
    with pytest.raises(PlatformCapabilityError, match="macos_database_key_source"):
        acquire_database_key_candidates(platform="darwin", keys_file=missing)


def test_other_platforms_and_cross_calls_are_explicit():
    with pytest.raises(PlatformCapabilityError, match="linux"):
        acquire_database_key_candidates(platform="linux")
    with pytest.raises(PlatformCapabilityError, match="interpose"):
        acquire_database_key_candidates(platform="win32", capture=True, config=object())
    with pytest.raises(NotImplementedError, match="macos_database_key_source"):
        MacMemoryKeySource().candidate_keys()
    if sys.platform != "win32":
        with pytest.raises(PlatformCapabilityError, match="Windows memory scan"):
            WindowsMemoryKeySource().candidate_keys()


def test_discovery_preview_on_this_host_does_not_crash(tmp_path: Path):
    if sys.platform == "win32":
        pytest.skip("Windows preview may scan Weixin.exe")
    from noteford.discovery import scan_library_candidates

    root = tmp_path / "xwechat_files"
    account = root / "wxid_example_ab12" / "db_storage"
    for name in ("session", "contact"):
        folder = account / name
        folder.mkdir(parents=True)
        (folder / f"{name}.db").write_bytes(b"fixture")
    rows = scan_library_candidates([root], include_previews=True)
    assert len(rows) == 1
    assert rows[0]["preview_status"] == "unavailable"
