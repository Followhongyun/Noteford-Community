import json
import sqlite3
from pathlib import Path

import pytest

from noteford.libraries import (
    LibraryRegistry,
    library_id_for,
    migrate_legacy_single_library,
)


def _make_account(root: Path, account: str):
    for rel in (
        Path(account) / "db_storage" / "session",
        Path(account) / "db_storage" / "contact",
    ):
        folder = root / rel
        folder.mkdir(parents=True, exist_ok=True)
    (root / account / "db_storage" / "session" / "session.db").write_bytes(b"x")
    (root / account / "db_storage" / "contact" / "contact.db").write_bytes(b"x")


def test_library_id_is_source_specific(tmp_path):
    account = "wxid_same"
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    assert library_id_for(root_a, account) != library_id_for(root_b, account)


def test_registry_discovers_multiple_accounts_and_switches(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    wechat = tmp_path / "wechat"
    _make_account(wechat, "wxid_a")
    _make_account(wechat, "wxid_b")

    config = {
        "project_root": str(app),
        "wechat_roots": [str(wechat)],
        "overlap_local_ids": 200,
        "allow_memory_rescan": False,
    }
    registry = LibraryRegistry(app, config)
    rows = registry.discover(wechat)
    assert len(rows) == 2
    assert len({row.id for row in rows}) == 2

    second = rows[1]
    registry.switch(second.id)
    assert registry.current().id == second.id

    context = registry.context(second.id)
    assert context.index_path == app / "libraries" / second.id / "data" / "index" / "wechat_index.db"
    assert context.candidates_path == app / "libraries" / second.id / "secrets" / "candidate_keys.json"


def test_legacy_migration_moves_workspace_without_merging(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    wechat = tmp_path / "wechat"
    account = "wxid_a"
    _make_account(wechat, account)

    legacy_index = app / "data" / "index" / "wechat_index.db"
    legacy_index.parent.mkdir(parents=True)
    con = sqlite3.connect(legacy_index)
    con.execute(
        """CREATE TABLE account_profile(
            account TEXT PRIMARY KEY,
            self_display_name TEXT
        )"""
    )
    con.execute(
        "INSERT INTO account_profile(account,self_display_name) VALUES(?,?)",
        (account, "My WeChat"),
    )
    con.commit()
    con.close()

    state_dir = app / "data" / "state" / account
    state_dir.mkdir(parents=True)
    (state_dir / "marker.txt").write_text("state", encoding="utf-8")

    media = app / "data" / "media-cache"
    media.mkdir(parents=True)
    (media / "a.jpg").write_bytes(b"img")

    secrets = app / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "candidate_keys.json").write_text(
        json.dumps({"candidates": []}),
        encoding="utf-8",
    )

    root_config = {
        "project_root": str(app),
        "wechat_root": str(wechat),
        "account": account,
        "overlap_local_ids": 200,
        "allow_memory_rescan": False,
    }
    registry = LibraryRegistry(app, root_config)
    registry.discover(wechat)
    result = migrate_legacy_single_library(
        app_root=app,
        root_config=root_config,
        registry=registry,
    )
    assert result["migrated"] is True

    library_id = library_id_for(wechat, account)
    context = registry.context(library_id)
    assert context.index_path.is_file()
    assert (context.data_root / "state" / account / "marker.txt").is_file()
    assert (context.data_root / "media-cache" / "a.jpg").is_file()
    import sys
    if sys.platform == 'darwin':
        from noteford.mac_key_storage import cached_secret
        assert cached_secret(context.candidates_path, 'candidates') == {'candidates': []}
        assert not context.candidates_path.exists()
    else:
        assert context.candidates_path.is_file()

    assert not legacy_index.exists()
    assert not media.exists()
    assert not (secrets / "candidate_keys.json").exists()

    record = registry.get(library_id)
    assert record.display_name == "My WeChat"
    assert record.state == "ready"
    assert record.initialized_at is not None

    again = migrate_legacy_single_library(
        app_root=app,
        root_config=root_config,
        registry=registry,
    )
    assert again["migrated"] is False
    assert again["reason"] == "already_migrated"


def test_multi_library_api_switches_isolated_empty_contexts(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from noteford import service
    from noteford.service import MultiLibraryApi
    monkeypatch.setattr(service, 'sys', SimpleNamespace(platform='linux'))

    app = tmp_path / "app"
    app.mkdir()
    wechat = tmp_path / "wechat"
    _make_account(wechat, "wxid_a")
    _make_account(wechat, "wxid_b")

    config_path = app / "config.local.json"
    config_path.write_text(
        json.dumps(
            {
                "project_root": str(app),
                "wechat_roots": [str(wechat)],
                "overlap_local_ids": 200,
                "allow_memory_rescan": False,
            }
        ),
        encoding="utf-8",
    )

    api = MultiLibraryApi(config_path)
    rows = api.libraries()
    assert len(rows) == 2
    first = next(row for row in rows if row["is_current"])
    second = next(row for row in rows if not row["is_current"])

    first_status = api.status()
    assert first_status["messages"] == 0
    assert first_status["library_id"] == first["id"]

    api.switch_library(second["id"], initialize=False)
    second_status = api.status()
    assert second_status["messages"] == 0
    assert second_status["library_id"] == second["id"]

    api.switch_library(first["id"], initialize=False)
    assert api.status()["library_id"] == first["id"]


def test_new_install_without_library_can_open_catalog(tmp_path):
    from noteford.service import MultiLibraryApi

    config_path = tmp_path / "config.local.json"
    config_path.write_text(
        json.dumps({"project_root": str(tmp_path), "wechat_roots": []}),
        encoding="utf-8",
    )
    api = MultiLibraryApi(config_path)
    assert api.libraries() == []
    assert api.status()["library_state"] == "empty"
    assert api.conversations() == []
    assert api.contacts() == []




def test_remove_library_deletes_local_data_and_prevents_rediscovery(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    wechat = tmp_path / "wechat"
    _make_account(wechat, "wxid_a")
    _make_account(wechat, "wxid_b")

    config = {
        "project_root": str(app),
        "wechat_roots": [str(wechat)],
        "overlap_local_ids": 200,
        "allow_memory_rescan": False,
    }
    registry = LibraryRegistry(app, config)
    rows = registry.discover(wechat)
    assert len(rows) == 2

    target = rows[0]
    context = registry.context(target.id)
    context.ensure_dirs()
    marker = context.data_root / "index" / "marker.txt"
    marker.write_text("local-data", encoding="utf-8")

    removed = registry.remove(
        target.id,
        delete_local_data=True,
        ignore_source=True,
    )
    assert removed["deleted_local_data"] is True
    assert removed["ignored_source"] is True
    assert not context.root.exists()
    assert target.id not in {row.id for row in registry.list()}

    registry.discover(wechat)
    assert target.id not in {row.id for row in registry.list()}
    assert len(registry.list()) == 1
