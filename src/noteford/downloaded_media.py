"""Account/message-bound, integrity-checked copies acquired independently of WeChat cache."""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path


_EXTENSIONS = {"image": {"png", "jpg", "gif", "webp"}, "video": {"mp4"}}


def _folder(root: Path, account: str, uid: str) -> Path:
    identity = hashlib.sha256((account + "\0" + uid).encode()).hexdigest()
    return root / "downloaded" / identity


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def retain_download(root: Path, account: str, uid: str, kind: str, extension: str,
                    data: bytes, *, verified_sha256: str, verification: str) -> Path:
    """Retain a previously verified acquisition; this function performs no network requests."""
    if extension not in _EXTENSIONS.get(kind, set()) or not data:
        raise ValueError("invalid_download_type")
    if verification not in {"message_original", "cdn_variant"}:
        raise ValueError("invalid_download_verification")
    digest = hashlib.sha256(data).hexdigest()
    if digest != verified_sha256:
        raise ValueError("download_integrity_mismatch")
    folder = _folder(root, account, uid)
    path = folder / (digest + "." + extension)
    _atomic_write(path, data)
    manifest = {"version": 1, "account": account, "uid": uid, "kind": kind,
                "extension": extension, "sha256": digest, "bytes": len(data),
                "verification": verification}
    _atomic_write(folder / "manifest.json", json.dumps(manifest).encode())
    return path


def retained_download(root: Path, account: str, uid: str, kind: str):
    folder = _folder(root, account, uid)
    try:
        manifest_path = folder / "manifest.json"
        if manifest_path.stat().st_size > 4096:
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or any(manifest.get(k) != v for k, v in
                {"version": 1, "account": account, "uid": uid, "kind": kind}.items()):
            return None
        digest = manifest.get("sha256", "")
        extension = manifest.get("extension")
        verification = manifest.get("verification")
        if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or extension not in _EXTENSIONS.get(kind, set())
                or verification not in {"message_original", "cdn_variant"}):
            return None
        path = folder / (digest + "." + extension)
        if not path.resolve().is_relative_to(root.resolve()):
            return None
        if path.stat().st_size != manifest.get("bytes"):
            return None
        with path.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != digest:
                return None
        return path, manifest["bytes"], verification
    except (OSError, ValueError, TypeError):
        return None


def file_digest(path, algorithm='sha256', should_continue=None):
    digest = hashlib.new(algorithm)
    with path.open('rb') as handle:
        while True:
            if should_continue is not None and not should_continue():
                from .cdn_download import DownloadError
                raise DownloadError('download_cancelled')
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def promote_download(source, folder, extension, should_continue=None):
    """Consume a private verified transfer on the same filesystem, without a RAM copy."""
    size = source.stat().st_size
    digest = file_digest(source, should_continue=should_continue)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (digest + '.' + extension)
    source.replace(path)
    return path, digest, size


def retain_download_file(root, account, uid, kind, extension, source, *, verification, should_continue=None):
    if extension not in _EXTENSIONS.get(kind, set()) or source.stat().st_size <= 0:
        raise ValueError('invalid_download_type')
    if verification not in {'message_original', 'cdn_variant'}:
        raise ValueError('invalid_download_verification')
    folder = _folder(root, account, uid)
    path, digest, size = promote_download(source, folder, extension, should_continue)
    manifest = {'version': 1, 'account': account, 'uid': uid, 'kind': kind,
                'extension': extension, 'sha256': digest, 'bytes': size, 'verification': verification}
    _atomic_write(folder / 'manifest.json', json.dumps(manifest).encode())
    return path
