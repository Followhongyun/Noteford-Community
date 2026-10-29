from __future__ import annotations

import base64
import os
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable

from .image_crypto import image_extension
from .libraries import account_username, discover_account_directories, library_id_for
from .refresh import refresh_database


IMAGE_MIME = {"jpg": "image/jpeg", "png": "image/png", "gif": "image/gif", "webp": "image/webp"}


def candidate_roots(
    configured: Iterable[str] = (),
    *,
    roaming_root: Path | None = None,
    home: Path | None = None,
) -> list[Path]:
    home = home or Path.home()
    roaming_root = roaming_root or Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    paths = [Path(value) for value in configured if value]
    config_dir = roaming_root / "Tencent" / "xwechat" / "config"
    if config_dir.is_dir():
        for config_file in sorted(config_dir.glob("*.ini"))[:64]:
            try:
                if config_file.stat().st_size > 4096:
                    continue
                value = config_file.read_text(encoding="utf-8-sig").strip("\x00\r\n\t ")
                if value:
                    paths.append(Path(value))
            except (OSError, UnicodeError):
                continue
    paths.extend(
        [
            home / "Documents" / "WeChat Files",
            home / "Documents" / "Tencent Files",
            home / "Documents",
            home,
        ]
    )
    found: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        if not path.is_absolute() or str(path).startswith("\\\\"):
            continue
        try:
            root, accounts = discover_account_directories(path)
        except OSError:
            continue
        key = str(root).casefold()
        if accounts and key not in seen:
            seen.add(key)
            found.append(root)
    return found


def _read_profile(contact_db: Path, username: str) -> str | None:
    con = sqlite3.connect(f"file:{contact_db.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT nick_name, alias FROM contact WHERE username=? LIMIT 1",
            (username,),
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    for value in row:
        if isinstance(value, str) and value.strip():
            return value.strip()[:100]
    return None


def _read_avatar(head_db: Path, username: str) -> str | None:
    con = sqlite3.connect(f"file:{head_db.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT image_buffer FROM head_image WHERE username=? LIMIT 1",
            (username,),
        ).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        return None
    image = bytes(row[0])
    if len(image) > 1024 * 1024:
        return None
    mime = IMAGE_MIME.get(image_extension(image))
    if not mime:
        return None
    return f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"


def scan_library_candidates(
    roots: Iterable[Path],
    *,
    include_previews: bool = True,
) -> list[dict]:
    accounts: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for path in roots:
        try:
            root, names = discover_account_directories(path)
        except OSError:
            continue
        for account in names:
            library_id = library_id_for(root, account)
            if library_id not in seen:
                seen.add(library_id)
                accounts.append((root, account))

    results: list[dict] = []
    with TemporaryDirectory(prefix="noteford-discovery-") as temporary:
        work = Path(temporary)
        keys_path = work / "secrets" / "candidate_keys.json"
        keys_ready = False
        if accounts and include_previews:
            from .platform_seam import PlatformCapabilityError, acquire_database_key_candidates

            try:
                candidates = acquire_database_key_candidates()
                if candidates and not isinstance(candidates[0], dict):
                    from .winmem import save_candidates

                    save_candidates(keys_path, candidates)
                    keys_ready = True
            except (OSError, PlatformCapabilityError):
                pass

        for root, account in accounts:
            library_id = library_id_for(root, account)
            username = account_username(account)
            item = {
                "id": library_id,
                "account": account,
                "source_root": str(root),
                "display_name": username,
                "avatar_data_url": None,
                "preview_status": "unavailable",
            }
            if keys_ready:
                preview_root = work / library_id
                storage = root / account / "db_storage"
                try:
                    result = refresh_database(
                        storage / "contact" / "contact.db",
                        account,
                        preview_root,
                        keys_path,
                        preview_root / "index.db",
                        allow_memory_rescan=False,
                    )
                    nickname = _read_profile(Path(result.decrypted), username)
                    if nickname:
                        item["display_name"] = nickname
                        item["preview_status"] = "name"
                except (OSError, RuntimeError, ValueError, sqlite3.Error):
                    pass
                head = storage / "head_image" / "head_image.db"
                if head.is_file():
                    try:
                        result = refresh_database(
                            head,
                            account,
                            preview_root,
                            keys_path,
                            preview_root / "index.db",
                            allow_memory_rescan=False,
                        )
                        item["avatar_data_url"] = _read_avatar(Path(result.decrypted), username)
                    except (OSError, RuntimeError, ValueError, sqlite3.Error):
                        pass
                if item["avatar_data_url"] and item["preview_status"] == "name":
                    item["preview_status"] = "ready"
            results.append(item)
    return results
