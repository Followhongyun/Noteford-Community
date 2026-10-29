import json
import http.client
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from noteford.discovery import (
    _read_avatar,
    _read_profile,
    candidate_roots,
    scan_library_candidates,
)
from noteford.libraries import account_username
from noteford.metadata import sync_metadata
from noteford.indexer import ensure_index_schema
from noteford.metadata import ensure_metadata_schema
from noteford.repository import WeChatRepository
from noteford.service import MultiLibraryApi, make_handler


def _account(root: Path, name: str) -> None:
    for category in ("contact", "session"):
        path = root / name / "db_storage" / category / f"{category}.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")


def test_weixin_path_config_discovers_all_accounts_without_registering(tmp_path):
    root = tmp_path / "custom" / "xwechat_files"
    _account(root, "wxid_first_ab12")
    _account(root, "wxid_second_cd34")
    roaming = tmp_path / "roaming"
    config_dir = roaming / "Tencent" / "xwechat" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "account.ini").write_text(str(root.parent), encoding="utf-8")

    roots = candidate_roots([], roaming_root=roaming, home=tmp_path / "home")
    rows = scan_library_candidates(roots, include_previews=False)

    assert roots == [root]
    assert {row["account"] for row in rows} == {"wxid_first_ab12", "wxid_second_cd34"}
    assert all(row["preview_status"] == "unavailable" for row in rows)
    assert account_username("wxid_first_ab12") == "wxid_first"
    assert not (tmp_path / "custom" / "libraries").exists()


def test_registering_candidate_adds_only_selected_account(tmp_path, monkeypatch):
    # These synthetic db_storage fixtures use the encrypted-file adapter on every host.
    monkeypatch.setattr("noteford.platform_seam.library_source_kind", lambda platform=None: "wechat_files")
    root = tmp_path / "xwechat_files"
    _account(root, "wxid_first_ab12")
    _account(root, "wxid_second_cd34")
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"project_root": str(tmp_path), "wechat_roots": []}), encoding="utf-8")
    api = MultiLibraryApi(config)
    monkeypatch.setattr(
        "noteford.service.candidate_roots", lambda _configured: [root]
    )
    monkeypatch.setattr(
        "noteford.service.scan_library_candidates",
        lambda roots: scan_library_candidates(roots, include_previews=False),
    )

    rows = api.discover_candidates()
    assert len(rows) == 2
    assert api.libraries() == []
    selected = next(row for row in rows if row["account"] == "wxid_second_cd34")
    api.register_candidate(selected["id"])
    assert [row["id"] for row in api.libraries()] == [selected["id"]]
    assert api.status()["library_id"] == selected["id"]


def test_candidate_http_flow_requires_token_and_registers_one(tmp_path, monkeypatch):
    # These synthetic db_storage fixtures use the encrypted-file adapter on every host.
    monkeypatch.setattr("noteford.platform_seam.library_source_kind", lambda platform=None: "wechat_files")
    root = tmp_path / "xwechat_files"
    _account(root, "wxid_first_ab12")
    _account(root, "wxid_second_cd34")
    config = tmp_path / "config.local.json"
    config.write_text(json.dumps({"project_root": str(tmp_path), "wechat_roots": []}), encoding="utf-8")
    api = MultiLibraryApi(config)
    monkeypatch.setattr(
        "noteford.service.candidate_roots", lambda _configured: [root]
    )
    monkeypatch.setattr(
        "noteford.service.scan_library_candidates",
        lambda roots: scan_library_candidates(roots, include_previews=False),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api, "test-token"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        con = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
        con.request("POST", "/api/v1/libraries/candidates", body="{}")
        denied = con.getresponse()
        assert denied.status == 401
        denied.read()

        headers = {"X-Noteford-Token": "test-token", "Content-Type": "application/json"}
        con.request("POST", "/api/v1/libraries/candidates", body="{}", headers=headers)
        response = con.getresponse()
        assert response.status == 200
        rows = json.loads(response.read())["data"]
        assert len(rows) == 2
        assert api.libraries() == []

        con.request(
            "POST", "/api/v1/libraries/register",
            body=json.dumps({"library_id": rows[1]["id"]}), headers=headers,
        )
        selected = con.getresponse()
        assert selected.status == 200
        selected.read()
        assert [row["id"] for row in api.libraries()] == [rows[1]["id"]]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_preview_reads_self_nickname_and_avatar_from_decrypted_metadata(tmp_path):
    contact = tmp_path / "contact.db"
    con = sqlite3.connect(contact)
    con.execute("CREATE TABLE contact(username TEXT, nick_name TEXT, alias TEXT)")
    con.execute("INSERT INTO contact VALUES(?,?,?)", ("wxid_me", "我的昵称", ""))
    con.commit()
    con.close()
    head = tmp_path / "head_image.db"
    con = sqlite3.connect(head)
    con.execute("CREATE TABLE head_image(username TEXT, image_buffer BLOB)")
    con.execute("INSERT INTO head_image VALUES(?,?)", ("wxid_me", b"\x89PNG\r\n\x1a\nfixture"))
    con.commit()
    con.close()

    assert _read_profile(contact, "wxid_me") == "我的昵称"
    assert _read_avatar(head, "wxid_me").startswith("data:image/png;base64,")


def test_catalog_identifies_self_before_message_indexing(tmp_path):
    account = "wxid_me_ab12"
    contact = tmp_path / "contact.db"
    con = sqlite3.connect(contact)
    con.execute("CREATE TABLE contact(username TEXT, alias TEXT, remark TEXT, nick_name TEXT)")
    con.execute("INSERT INTO contact VALUES(?,?,?,?)", ("wxid_me", "", "", "我的昵称"))
    con.commit()
    con.close()
    session = tmp_path / "session.db"
    con = sqlite3.connect(session)
    con.execute("CREATE TABLE SessionNoContactInfoTable(username TEXT, session_title TEXT)")
    con.execute("CREATE TABLE SessionTable(username TEXT, type INTEGER, last_timestamp INTEGER, sort_timestamp INTEGER)")
    con.commit()
    con.close()
    index = tmp_path / "index.db"
    ensure_index_schema(index)
    con = sqlite3.connect(index)
    ensure_metadata_schema(con)
    con.close()

    sync_metadata(index, account, session, contact, [])
    status = WeChatRepository(index, account).status()
    assert status["self_username"] == "wxid_me"
    assert status["self_display_name"] == "我的昵称"
