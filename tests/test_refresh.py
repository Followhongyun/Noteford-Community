from pathlib import Path

from noteford.refresh import (
    discover_auxiliary_databases,
    discover_message_databases,
)


def test_discover_message_databases_includes_biz_partitions(tmp_path):
    message_dir = tmp_path / "acct" / "db_storage" / "message"
    message_dir.mkdir(parents=True)
    for name in (
        "message_0.db",
        "biz_message_0.db",
        "media_0.db",
        "message_resource.db",
        "message_fts.db",
    ):
        (message_dir / name).write_bytes(b"")

    found = [path.name for path in discover_message_databases(tmp_path, "acct")]
    assert found == ["biz_message_0.db", "message_0.db"]


def test_discover_auxiliary_databases_includes_all_other_database_types(tmp_path):
    storage = tmp_path / "acct" / "db_storage"
    for category, name in (
        ("message", "message_resource.db"),
        ("hardlink", "hardlink.db"),
        ("head_image", "head_image.db"),
        ("sns", "sns.db"),
    ):
        folder = storage / category
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(b"")

    found = {
        str(path.relative_to(storage)).replace("\\", "/")
        for path in discover_auxiliary_databases(tmp_path, "acct")
    }
    assert found == {
        "message/message_resource.db",
        "hardlink/hardlink.db",
        "sns/sns.db",
    }
