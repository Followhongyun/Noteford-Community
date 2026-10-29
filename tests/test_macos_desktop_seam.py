"""macOS packaging seams. Windows discovery and the .exe service check stay in place."""
from __future__ import annotations

import importlib.util
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from noteford.platform_seam import (
    client_script_name,
    decrypted_archive_root,
    discover_library_candidates,
    library_source_kind,
    mcp_discovery_file,
    refresh_host_library,
    service_command_acceptable,
)
from noteford.service import MultiLibraryApi

ROOT = Path(__file__).parents[1]


def _sqlite(path: Path, script: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(script)
        con.commit()
    finally:
        con.close()


def _archive(root: Path) -> Path:
    account = root / "wxid_friend_ab12"
    _sqlite(
        account / "session" / "20261017" / "session.db",
        """
        CREATE TABLE SessionTable (
            username TEXT, type INTEGER, last_timestamp INTEGER, sort_timestamp INTEGER
        );
        CREATE TABLE SessionNoContactInfoTable (username TEXT, session_title TEXT);
        INSERT INTO SessionTable VALUES ('wxid_pal', 0, 1700000000, 1700000000);
        """,
    )
    _sqlite(
        account / "contact" / "20261017" / "contact.db",
        """
        CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, nick_name TEXT);
        INSERT INTO contact VALUES ('wxid_friend', '', '', '小明');
        INSERT INTO contact VALUES ('wxid_pal', '', '朋友', '朋友');
        """,
    )
    _sqlite(
        account / "message" / "20261017" / "message_0.db",
        """
        CREATE TABLE Msg_demo (
            local_id INTEGER, server_id INTEGER, create_time INTEGER,
            local_type INTEGER, message_content TEXT
        );
        INSERT INTO Msg_demo VALUES (1, 10, 1700000000, 1, 'hello');
        """.replace("Msg_demo", "Msg_" + hashlib.md5(b"wxid_pal").hexdigest()),
    )
    return root


def test_windows_kind_and_discovery_path_stay_put():
    home = Path("/Users/example")
    local = Path("C:/Users/example/AppData/Local")
    assert library_source_kind("win32") == "wechat_files"
    assert library_source_kind("linux") == "wechat_files"
    assert decrypted_archive_root("win32", home=home) is None
    assert mcp_discovery_file("win32", home=home, local_app_data=local) == local / "Noteford Community" / "connection.json"
    assert client_script_name("win32") == "noteford.ps1"
    assert service_command_acceptable(
        Path("C:/Program Files/Noteford/Noteford.exe"),
        Path("C:/Program Files/Noteford/resources/service/noteford-service.exe"),
        platform="win32",
    )
    assert not service_command_acceptable(
        Path("C:/Program Files/Noteford/Noteford.exe"),
        Path("C:/Python/python.exe"),
        platform="win32",
        args=[],
    )


def test_macos_discovery_service_and_archive_root():
    home = Path("/Users/example")
    assert library_source_kind("darwin") == "decrypted_archive"
    assert decrypted_archive_root("darwin", home=home) == home / "Noteford Community" / "Data" / "data" / "decrypted"
    assert mcp_discovery_file("darwin", home=home) == (
        home / "Library" / "Application Support" / "Noteford Community" / "connection.json"
    )
    assert client_script_name("darwin") == "noteford.sh"
    app = home / "Applications" / "Noteford 开源版.app" / "Contents" / "MacOS" / "Noteford Community"
    binary = home / "Applications" / "Noteford 开源版.app" / "Contents" / "Resources" / "service" / "noteford-service"
    assert service_command_acceptable(app, binary, platform="darwin")
    python = home / "venv" / "bin" / "python3"
    assert service_command_acceptable(app, python, platform="darwin", args=["-m", "noteford.service"])
    assert not service_command_acceptable(app, python, platform="darwin", args=[])


def test_decrypted_archive_lists_account_and_opens_conversations(tmp_path, monkeypatch):
    archive = _archive(tmp_path / "decrypted")
    original = {file: file.read_bytes() for file in archive.rglob("*.db")}
    monkeypatch.setattr("noteford.platform_seam.library_source_kind", lambda platform=None: "decrypted_archive")
    monkeypatch.setattr(
        "noteford.platform_seam.decrypted_archive_root",
        lambda platform=None, home=None: archive,
    )
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"project_root": str(tmp_path), "wechat_roots": []}), encoding="utf-8")
    api = MultiLibraryApi(config)
    rows = api.discover_candidates()
    assert [row["account"] for row in rows] == ["wxid_friend_ab12"]
    assert rows[0]["display_name"] == "小明"
    assert rows[0]["source_kind"] == "decrypted_archive"
    assert api.libraries() == []
    api.register_candidate(rows[0]["id"])
    switched = api.switch_library(rows[0]["id"])
    assert switched["initialization_error"] is None
    assert switched["library"]["state"] == "ready"
    usernames = [item["username"] for item in api.conversations()]
    assert usernames == ["wxid_pal"]
    # Mac follow must never enter encrypted-file or memory-scan paths.
    monkeypatch.setattr("noteford.service.follow_conversation", lambda **_kw: pytest.fail("Windows follow path called"))
    monkeypatch.setattr("noteford.service.LocalApi._prefetch_followed_media", lambda *a, **k: {})
    monkeypatch.setattr("noteford.service._refresh_library", lambda **_kw: pytest.fail("Mac follow must not refresh the live source"))
    followed = api.follow("wxid_pal")
    assert followed["selection"]["state"] == "following"
    assert followed["refresh"]["source_dbs"] == ["message_0.db"]
    messages = api.messages("wxid_pal")["messages"]
    assert any(row.get("text") == "hello" for row in messages)
    assert {file: file.read_bytes() for file in archive.rglob("*.db")} == original
    api.pause("wxid_pal")
    assert api.follow("wxid_pal")["selection"]["state"] == "following"
    assert api.messages("wxid_pal")["messages"] == messages



def test_non_mac_follow_keeps_existing_dispatch(monkeypatch):
    from noteford import service
    monkeypatch.setattr("noteford.platform_seam.library_source_kind", lambda: "wechat_files")
    monkeypatch.setattr(service, "follow_conversation", lambda **kwargs: kwargs)
    assert service._follow_library(username="fixture", allow_memory_rescan=False) == {
        "username": "fixture", "allow_memory_rescan": False,
    }


def test_decrypted_archive_import_does_not_load_winmem():
    completed = subprocess.run(
        [sys.executable, "-c", "import sys; import noteford.decrypted_archive; raise SystemExit('noteford.winmem' in sys.modules)"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_manual_folder_on_macos_uses_the_same_archive(tmp_path, monkeypatch):
    archive = _archive(tmp_path / "picked")
    monkeypatch.setattr("noteford.platform_seam.library_source_kind", lambda platform=None: "decrypted_archive")
    rows = discover_library_candidates(wechat_root=archive / "wxid_friend_ab12", platform="darwin")
    assert rows[0]["account"] == "wxid_friend_ab12"
    assert rows[0]["display_name"] == "小明"


def test_windows_refresh_still_delegates_to_catalog(monkeypatch):
    called = {}

    def fake_catalog(**kwargs):
        called["kwargs"] = kwargs
        return {"archive_status": {"last_success_at": "t"}, "selected_refresh": []}

    monkeypatch.setattr("noteford.selective.refresh_catalog", fake_catalog)
    result = refresh_host_library(
        platform="win32",
        wechat_root=Path("/wechat"),
        account="wxid",
        project_root=Path("/project"),
        candidates_path=Path("/keys"),
        index_path=Path("/index"),
    )
    assert result["archive_status"]["last_success_at"] == "t"
    assert called["kwargs"]["account"] == "wxid"
    assert called["kwargs"]["wechat_root"] == Path("/wechat")


def test_client_script_paths_match_the_seam():
    script = ROOT / "desktop" / "mcp-plugin" / "skills" / "noteford-community" / "scripts" / "noteford.py"
    spec = importlib.util.spec_from_file_location("noteford_client_script", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = Path("/Users/example")
    assert module.discovery_file("darwin", home=home) == mcp_discovery_file("darwin", home=home)
    local = "C:/Users/example/AppData/Local"
    assert module.discovery_file("win32", home=home, local_app_data=local) == mcp_discovery_file(
        "win32", home=home, local_app_data=Path(local)
    )
    shell = (script.parent / "noteford.sh").read_text(encoding="utf-8")
    assert "noteford.py" in shell
    assert "noteford.ps1" not in shell


def test_newer_plaintext_snapshot_wins(tmp_path):
    account = tmp_path / "wxid_friend_ab12"
    older = account / "session" / "old" / "session.db"
    newer = account / "session" / "new" / "session.db"
    for path, title in ((older, "旧"), (newer, "新")):
        _sqlite(
            path,
            f"""
            CREATE TABLE SessionTable (
                username TEXT, type INTEGER, last_timestamp INTEGER, sort_timestamp INTEGER
            );
            CREATE TABLE SessionNoContactInfoTable (username TEXT, session_title TEXT);
            INSERT INTO SessionNoContactInfoTable VALUES ('wxid_pal', '{title}');
            INSERT INTO SessionTable VALUES ('wxid_pal', 0, 1, 1);
            """,
        )
    _sqlite(
        account / "contact" / "c" / "contact.db",
        """
        CREATE TABLE contact (username TEXT, alias TEXT, remark TEXT, nick_name TEXT);
        INSERT INTO contact VALUES ('wxid_friend', '', '', '小明');
        """,
    )
    import os

    os.utime(older, (1, 1))
    os.utime(newer, (2_000_000_000, 2_000_000_000))
    from noteford.decrypted_archive import _newest_named

    assert _newest_named(account, "session.db") == newer
    rows = discover_library_candidates(wechat_root=tmp_path, platform="darwin")
    assert [row["account"] for row in rows] == ["wxid_friend_ab12"]
