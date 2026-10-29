import hashlib
import json

import pytest

from noteford.downloaded_media import retain_download, retained_download
from noteford.media import MediaResolver


def save(root, kind="image"):
    data = b"verified acquired media"
    return retain_download(root, "account", "uid", kind, "png" if kind == "image" else "mp4",
                           data, verified_sha256=hashlib.sha256(data).hexdigest(),
                           verification="message_original" if kind == "image" else "cdn_variant")


@pytest.mark.parametrize("kind", ["image", "video"])
def test_retained_media_survives_absent_wechat_source(tmp_path, kind):
    root = tmp_path / "cache"
    path = save(root, kind)
    resolver = MediaResolver(tmp_path / "absent-wechat", "account")
    artifact = (resolver.export_image("Msg_abc", 0, "identifier", "uid", root)
                if kind == "image" else resolver.export_video(0, "identifier", "uid", root))
    assert artifact.status == "ready"
    assert artifact.path == str(path.resolve())
    resolver.force_copy = True
    assert retained_download(root, "account", "uid", kind)
    assert retained_download(root, "other-account", "uid", kind) is None
    assert retained_download(root, "account", "other-uid", kind) is None


def test_tampered_file_or_manifest_is_not_served(tmp_path):
    path = save(tmp_path)
    data = path.read_bytes()
    path.write_bytes(b"X" + data[1:])
    assert retained_download(tmp_path, "account", "uid", "image") is None
    path = save(tmp_path)
    manifest_path = path.parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sha256"] = "../../outside"
    manifest_path.write_text(json.dumps(manifest))
    assert retained_download(tmp_path, "account", "uid", "image") is None


def test_failed_integrity_does_not_replace_retained_media(tmp_path):
    path = save(tmp_path)
    with pytest.raises(ValueError, match="integrity"):
        retain_download(tmp_path, "account", "uid", "image", "png", b"bad",
                        verified_sha256="0" * 64, verification="message_original")
    assert retained_download(tmp_path, "account", "uid", "image")[0] == path
