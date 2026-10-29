from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from .batch import export_daily_batch
from .crypto import decrypt_database, find_matching_key, merge_encrypted_wal, quick_check
from .image_crypto import (
    V2_MAGIC,
    derive_xor_key,
    find_recent_thumbnail_templates,
    save_image_keys,
    scan_image_aes_key,
)
from .indexer import index_messages, index_status
from .jobs import run_daily_job
from .libraries import load_root_config, registry_from_config
from .maintenance import prune_snapshot_generations, run_retention_policy
from .media import latest_decrypted_media_databases
from .selective import (
    follow_conversation,
    list_selections,
    pause_conversation,
    refresh_catalog,
    selection_status,
)
from .repository import WeChatRepository
from .service import load_config as load_app_config, serve as serve_api
from .snapshot import copy_database_snapshot


def _default_candidate_path() -> Path:
    return Path("secrets") / "candidate_keys.json"


def _refuse_other_platform(exc: BaseException) -> int:
    print(json.dumps({
        'ok': False,
        'error': 'unsupported_platform',
        'platform': sys.platform,
        'detail': str(exc),
    }, ensure_ascii=False, indent=2))
    return 2


def cmd_diagnose(_args: argparse.Namespace) -> int:
    # Shareable diagnostics must not include command lines or account paths.
    from .platform_seam import PlatformCapabilityError, list_host_wechat_processes
    from .winmem import UnsupportedPlatform

    try:
        rows = list_host_wechat_processes()
    except (PlatformCapabilityError, UnsupportedPlatform) as exc:
        return _refuse_other_platform(exc)
    print(json.dumps({'weixin_process_count': len(rows),
                      'python_version': sys.version.split()[0],
                      'platform': sys.platform}, ensure_ascii=False, indent=2))
    return 0 if rows else 2


def cmd_scan_keys(args: argparse.Namespace) -> int:
    from .platform_seam import PlatformCapabilityError, acquire_database_key_candidates
    from .winmem import UnsupportedPlatform, save_candidates

    config = load_app_config(args.config.resolve())
    out = args.out or Path(config["candidates_path"])
    try:
        candidates = acquire_database_key_candidates()
    except (PlatformCapabilityError, UnsupportedPlatform) as exc:
        return _refuse_other_platform(exc)
    if candidates and isinstance(candidates[0], dict):
        return _refuse_other_platform(PlatformCapabilityError(
            'scan-keys writes the Windows memory scan. '
            'On macOS use macos_database_key_source (keys.json / interpose).'
        ))
    save_candidates(out, candidates)
    public = [
        {"pid": item.pid, "fingerprint": item.fingerprint, "has_explicit_salt": bool(item.salt_hex)}
        for item in candidates
    ]
    print(
        json.dumps(
            {
                "candidate_count": len(candidates),
                "saved_to": str(out.resolve()),
                "candidates": public,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if candidates else 3


def cmd_scan_image_key(args: argparse.Namespace) -> int:
    config = load_app_config(args.config.resolve())
    project_root = Path(config["project_root"])
    account_root = Path(config["wechat_root"]) / str(config["account"])
    templates = find_recent_thumbnail_templates(account_root, keep=args.templates)
    if not templates:
        print(json.dumps({"ok": False, "reason": "no_image_templates"}, ensure_ascii=False))
        return 6

    sample = None
    for path in templates:
        try:
            if path.read_bytes()[:6] == V2_MAGIC:
                sample = path
                break
        except OSError:
            continue
    if sample is None:
        print(json.dumps({"ok": False, "reason": "no_v2_image_template"}, ensure_ascii=False))
        return 7

    xor_key = derive_xor_key(templates)
    if xor_key is None:
        print(json.dumps({"ok": False, "reason": "xor_key_not_inferred"}, ensure_ascii=False))
        return 8

    from .winmem import UnsupportedPlatform

    try:
        aes_key = scan_image_aes_key(sample)
    except UnsupportedPlatform as exc:
        return _refuse_other_platform(exc)
    if aes_key is None:
        print(
            json.dumps(
                {
                    "ok": False,
                    "reason": "aes_key_not_in_memory",
                    "note": "No process state was modified. The transient key may not currently be resident.",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 9

    out = project_root / "secrets" / "image_keys.json"
    fingerprints = save_image_keys(out, aes_key, xor_key)
    print(
        json.dumps(
            {
                "ok": True,
                "saved_to": str(out),
                **fingerprints,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    folder = copy_database_snapshot(args.db, args.out)
    print(json.dumps({"snapshot": str(folder.resolve())}, ensure_ascii=False, indent=2))
    return 0


def _load_candidates(path: Path) -> list[dict]:
    import sys
    if sys.platform == 'darwin':
        from .mac_key_storage import cached_candidates
        return cached_candidates(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload.get("candidates") or [])


def cmd_probe(args: argparse.Namespace) -> int:
    candidates = _load_candidates(args.keys)
    match = find_matching_key(args.db, candidates)
    if not match:
        print(json.dumps({"matched": False}, ensure_ascii=False, indent=2))
        return 4

    key, fingerprint = match
    decrypt_database(args.db, args.out, key)
    wal_path = Path(str(args.db) + "-wal")
    wal_frames = 0
    if wal_path.is_file():
        wal_frames = merge_encrypted_wal(args.out, wal_path, key)
    ok = quick_check(args.out)
    print(
        json.dumps(
            {
                "matched": True,
                "fingerprint": fingerprint,
                "decrypted_to": str(args.out.resolve()),
                "wal_frames_applied": wal_frames,
                "quick_check": ok,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if ok else 5


def cmd_index(args: argparse.Namespace) -> int:
    stats = index_messages(
        source_db_path=args.db,
        index_path=args.index,
        account=args.account,
        overlap_local_ids=args.overlap,
        force_full=args.full,
    )
    print(json.dumps(stats.__dict__, ensure_ascii=False, indent=2))
    return 0


def cmd_index_status(args: argparse.Namespace) -> int:
    print(
        json.dumps(
            index_status(args.index, account=args.account),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    config = load_app_config(args.config.resolve())
    project_root = Path(config["project_root"])
    wechat_root = Path(config["wechat_root"])
    account = str(config["account"])
    candidates = Path(config["candidates_path"])
    index_path = Path(config["index_path"])
    overlap = int(config.get("overlap_local_ids", 200))
    allow_rescan = bool(config.get("allow_memory_rescan", True))

    result = refresh_catalog(
        wechat_root=wechat_root,
        account=account,
        project_root=project_root,
        candidates_path=candidates,
        index_path=index_path,
        overlap_local_ids=overlap,
        allow_memory_rescan=allow_rescan,
        refresh_selected=True,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_daily_batch(args: argparse.Namespace) -> int:
    config = load_app_config(args.config.resolve())
    project_root = Path(config["project_root"])
    wechat_root = Path(config["wechat_root"])
    account = str(config["account"])
    candidates = Path(config["candidates_path"])
    index_path = Path(config["index_path"])
    overlap = int(config.get("overlap_local_ids", 200))
    allow_rescan = bool(config.get("allow_memory_rescan", True))

    if not args.no_refresh:
        refresh_catalog(
            wechat_root=wechat_root,
            account=account,
            project_root=project_root,
            candidates_path=candidates,
            index_path=index_path,
            overlap_local_ids=overlap,
            allow_memory_rescan=allow_rescan,
            refresh_selected=True,
        )

    until = int(args.until if args.until is not None else time.time())
    since = int(args.since if args.since is not None else until - args.hours * 3600)
    output_root = Path(
        config.get("batches_path") or (project_root / "data" / "batches")
    )
    result = export_daily_batch(
        index_path=index_path,
        account=account,
        since=since,
        until=until,
        output_root=output_root,
        max_chars=args.max_chars,
        wechat_root=wechat_root,
        media_db_paths=latest_decrypted_media_databases(project_root, account),
        media_cache_root=project_root / "data" / "media-cache",
        image_keys_path=project_root / "secrets" / "image_keys.json",
        voice_decoder_path=(
            Path(config["voice_decoder_path"]).resolve()
            if config.get("voice_decoder_path")
            else None
        ),
        selected_only=True,
    )
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0


def cmd_daily_job(args: argparse.Namespace) -> int:
    config = load_app_config(args.config.resolve())
    project_root = Path(config["project_root"])
    result = run_daily_job(
        project_root=project_root,
        wechat_root=Path(config["wechat_root"]),
        account=str(config["account"]),
        candidates_path=Path(config["candidates_path"]),
        index_path=Path(config["index_path"]),
        overlap_local_ids=int(config.get("overlap_local_ids", 200)),
        allow_memory_rescan=bool(config.get("allow_memory_rescan", True)),
        first_window_hours=args.first_window_hours,
        max_chars=args.max_chars,
        voice_decoder_path=(
            Path(config["voice_decoder_path"]).resolve()
            if config.get("voice_decoder_path")
            else None
        ),
        force=args.force,
    )
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0


def cmd_daily_all(args: argparse.Namespace) -> int:
    registry = registry_from_config(args.config.resolve())
    results = []
    failures = 0
    for record in registry.list():
        if record.state != "ready" or not record.initialized_at:
            results.append(
                {
                    "library_id": record.id,
                    "display_name": record.display_name,
                    "skipped": True,
                    "reason": "library_not_ready",
                }
            )
            continue

        config = registry.context(record.id).config_dict()
        index_path = Path(config["index_path"])
        selections = list_selections(index_path, config["account"])
        if not any(row.get("state") == "following" for row in selections):
            results.append(
                {
                    "library_id": record.id,
                    "display_name": record.display_name,
                    "skipped": True,
                    "reason": "no_following_conversations",
                }
            )
            continue

        try:
            job = run_daily_job(
                project_root=Path(config["project_root"]),
                wechat_root=Path(config["wechat_root"]),
                account=str(config["account"]),
                candidates_path=Path(config["candidates_path"]),
                index_path=index_path,
                overlap_local_ids=int(config.get("overlap_local_ids", 200)),
                allow_memory_rescan=bool(config.get("allow_memory_rescan", True)),
                first_window_hours=args.first_window_hours,
                max_chars=args.max_chars,
                voice_decoder_path=(
                    Path(config["voice_decoder_path"]).resolve()
                    if config.get("voice_decoder_path")
                    else None
                ),
                force=args.force,
            )
            cleanup = run_retention_policy(
                Path(config["project_root"]),
                str(config["account"]),
                encrypted_keep=2,
                decrypted_keep=1,
                batch_days=30,
                media_max_bytes=2 * 1024 * 1024 * 1024,
                runtime_days=7,
                apply=True,
            )
            results.append(
                {
                    "library_id": record.id,
                    "display_name": record.display_name,
                    "skipped": bool(job.skipped),
                    "job": job.__dict__,
                    "cleanup": cleanup.to_dict(),
                }
            )
        except Exception as exc:
            failures += 1
            results.append(
                {
                    "library_id": record.id,
                    "display_name": record.display_name,
                    "skipped": False,
                    "error": exc.__class__.__name__,
                }
            )

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def _repository_from_config(config_path: Path) -> tuple[dict, WeChatRepository]:
    config = load_app_config(config_path.resolve())
    list_selections(Path(config["index_path"]), config["account"])
    return config, WeChatRepository(Path(config["index_path"]), config["account"])


def cmd_app_status(args: argparse.Namespace) -> int:
    _config, repository = _repository_from_config(args.config)
    print(json.dumps(repository.status(), ensure_ascii=False, indent=2))
    return 0


def cmd_conversations(args: argparse.Namespace) -> int:
    _config, repository = _repository_from_config(args.config)
    rows = repository.list_conversations(
        limit=args.limit,
        offset=args.offset,
        query=args.query,
    )
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_messages(args: argparse.Namespace) -> int:
    config, repository = _repository_from_config(args.config)
    payload = repository.list_messages(
        args.conversation,
        limit=args.limit,
        before=args.before,
    )
    payload["selection"] = selection_status(
        Path(config["index_path"]),
        config["account"],
        args.conversation,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    _config, repository = _repository_from_config(args.config)
    payload = repository.search_messages(args.query, limit=args.limit)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_follow(args: argparse.Namespace) -> int:
    config = load_app_config(args.config.resolve())
    result = follow_conversation(
        wechat_root=Path(config["wechat_root"]),
        account=config["account"],
        project_root=Path(config["project_root"]),
        candidates_path=Path(config["candidates_path"]),
        index_path=Path(config["index_path"]),
        username=args.username,
        overlap_local_ids=int(config.get("overlap_local_ids", 200)),
        allow_memory_rescan=bool(config.get("allow_memory_rescan", True)),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    config = load_app_config(args.config.resolve())
    result = pause_conversation(
        Path(config["index_path"]),
        config["account"],
        args.username,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_selections(args: argparse.Namespace) -> int:
    config = load_app_config(args.config.resolve())
    rows = list_selections(Path(config["index_path"]), config["account"])
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_libraries(args: argparse.Namespace) -> int:
    registry = registry_from_config(args.config.resolve())
    print(json.dumps(registry.public_list(), ensure_ascii=False, indent=2))
    return 0


def cmd_library_switch(args: argparse.Namespace) -> int:
    registry = registry_from_config(args.config.resolve())
    record = registry.switch(args.library_id)
    print(
        json.dumps(
            record.public_dict(current=True),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_library_discover(args: argparse.Namespace) -> int:
    registry = registry_from_config(args.config.resolve())
    if args.wechat_root:
        registry.discover(args.wechat_root.resolve())
    else:
        root_config = load_root_config(args.config.resolve())
        for value in root_config.get("wechat_roots") or []:
            registry.discover(Path(value))
    print(json.dumps(registry.public_list(), ensure_ascii=False, indent=2))
    return 0


def cmd_library_rename(args: argparse.Namespace) -> int:
    registry = registry_from_config(args.config.resolve())
    record = registry.update(
        args.library_id,
        display_name=args.display_name,
        last_error=registry.get(args.library_id).last_error,
    )
    print(
        json.dumps(
            record.public_dict(current=(registry.current().id == record.id)),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_serve_api(args: argparse.Namespace) -> int:
    root_config = load_root_config(args.config.resolve())
    token_file = (
        args.token_file.resolve()
        if args.token_file
        else Path(root_config["project_root"]) / "secrets" / "api_token.json"
    )
    serve_api(args.config.resolve(), args.port, token_file)
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    config = load_app_config(args.config.resolve())
    project_root = Path(config["project_root"])
    result = run_retention_policy(
        project_root,
        str(config["account"]),
        encrypted_keep=args.encrypted_keep,
        decrypted_keep=args.decrypted_keep,
        batch_days=args.batch_days,
        media_max_bytes=int(args.media_max_gb * 1024 * 1024 * 1024),
        runtime_days=args.runtime_days,
        apply=args.apply,
    )
    print(
        json.dumps(
            result.to_dict(include_paths=args.verbose),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Windows WeChat 4.x snapshot/decryption toolkit"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("diagnose")
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("scan-keys")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.add_argument("--out", type=Path)
    p.set_defaults(func=cmd_scan_keys)

    p = sub.add_parser("scan-image-key")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.add_argument("--templates", type=int, default=24)
    p.set_defaults(func=cmd_scan_image_key)

    p = sub.add_parser("snapshot")
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--out", type=Path, default=Path("data/encrypted"))
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("probe")
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--keys", type=Path, default=_default_candidate_path())
    p.add_argument("--out", type=Path, default=Path("data/decrypted/probe.db"))
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("index")
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--account", required=True)
    p.add_argument("--index", type=Path, default=Path("data/index/wechat_index.db"))
    p.add_argument("--overlap", type=int, default=200)
    p.add_argument("--full", action="store_true")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("index-status")
    p.add_argument("--account")
    p.add_argument("--index", type=Path, default=Path("data/index/wechat_index.db"))
    p.set_defaults(func=cmd_index_status)

    p = sub.add_parser("refresh")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("daily-batch")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--since", type=int)
    p.add_argument("--until", type=int)
    p.add_argument("--max-chars", type=int, default=20000)
    p.add_argument("--no-refresh", action="store_true")
    p.set_defaults(func=cmd_daily_batch)

    p = sub.add_parser("daily-job")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.add_argument("--first-window-hours", type=int, default=24)
    p.add_argument("--max-chars", type=int, default=20000)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_daily_job)

    p = sub.add_parser("daily-all")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.add_argument("--first-window-hours", type=int, default=24)
    p.add_argument("--max-chars", type=int, default=20000)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_daily_all)

    p = sub.add_parser("app-status")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.set_defaults(func=cmd_app_status)

    p = sub.add_parser("conversations")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.add_argument("--query")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--offset", type=int, default=0)
    p.set_defaults(func=cmd_conversations)

    p = sub.add_parser("messages")
    p.add_argument("conversation")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.add_argument("--limit", type=int, default=80)
    p.add_argument("--before", type=int)
    p.set_defaults(func=cmd_messages)

    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("follow")
    p.add_argument("username")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.set_defaults(func=cmd_follow)

    p = sub.add_parser("pause")
    p.add_argument("username")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("selections")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.set_defaults(func=cmd_selections)

    p = sub.add_parser("libraries")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.set_defaults(func=cmd_libraries)

    p = sub.add_parser("library-switch")
    p.add_argument("library_id")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.set_defaults(func=cmd_library_switch)

    p = sub.add_parser("library-discover")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.add_argument("--wechat-root", type=Path)
    p.set_defaults(func=cmd_library_discover)

    p = sub.add_parser("library-rename")
    p.add_argument("library_id")
    p.add_argument("display_name")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.set_defaults(func=cmd_library_rename)

    p = sub.add_parser("serve-api")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.add_argument("--port", type=int, default=17890)
    p.add_argument("--token-file", type=Path)
    p.set_defaults(func=cmd_serve_api)

    p = sub.add_parser("cleanup")
    p.add_argument("--config", type=Path, default=Path("config.local.json"))
    p.add_argument("--encrypted-keep", type=int, default=2)
    p.add_argument("--decrypted-keep", type=int, default=1)
    p.add_argument("--batch-days", type=int, default=30)
    p.add_argument("--media-max-gb", type=float, default=2.0)
    p.add_argument("--runtime-days", type=int, default=7)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_cleanup)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
