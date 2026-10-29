import json
import sqlite3
from pathlib import Path

from noteford.assets import AvatarResolver


def test_avatar_resolver_exports_cached_head_image(tmp_path):
    project = tmp_path / "project"
    db = project / "data" / "decrypted" / "acct" / "head_image" / "head_image-current" / "head_image.db"
    db.parent.mkdir(parents=True)

    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE head_image(username TEXT, md5 TEXT, image_buffer BLOB, update_time INTEGER)"
    )
    jpeg = b"\xff\xd8\xff\xe0avatar-bytes\xff\xd9"
    con.execute(
        "INSERT INTO head_image VALUES(?,?,?,?)",
        ("gh_demo", "0123456789abcdef0123456789abcdef", jpeg, 1),
    )
    con.commit()
    con.close()

    state = project / "data" / "state" / "acct" / "head_image" / "head_image.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"decrypted": str(db.resolve())}), encoding="utf-8")

    resolver = AvatarResolver(project, "acct", project / "data" / "media-cache")
    out = resolver.export_many(["gh_demo", "missing"])
    assert "gh_demo" in out
    avatar = Path(out["gh_demo"])
    assert avatar.is_file()
    assert avatar.suffix == ".jpg"
    assert avatar.read_bytes() == jpeg
    assert "missing" not in out
