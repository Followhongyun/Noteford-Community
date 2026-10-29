from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .refresh import KeyUnavailable
from .runtime_lock import RuntimeLock
from .sync_state import SyncState, now_iso
from .login_status import account_login_status
from .assets import AvatarResolver, latest_decrypted_database, sync_followed_avatars
from .payment_states import PaymentStateReader
from .discovery import candidate_roots, scan_library_candidates
from .libraries import (
    LibraryRegistry,
    load_root_config,
    registry_from_config,
    resolve_library_config,
)
from .selective import (
    follow_conversation,
    list_selections,
    pause_conversation,
    refresh_catalog,
    selection_status,
)
from .repository import WeChatRepository
from .indexer import ensure_index_schema
from .media import MediaResolver, latest_decrypted_media_databases
from .media_resources import ImageResourceLookup
from .cdn_download import download_media, DownloadError
from .automatic_media import AutomaticMedia
from .media_ranges import MediaRanges
from .media_backfill import local_day_window, prefetch_recent_media, recover_image_key
from .media_summary import MediaDownloadSummary, empty_summary, remember_media
from .maintenance import prune_snapshot_generations
from .metadata import ensure_metadata_schema


def _refresh_library(**kwargs):
    """Windows tests patch ``refresh_catalog``. macOS goes through the seam."""

    from .platform_seam import library_source_kind, refresh_host_library

    if library_source_kind() == "decrypted_archive":
        return refresh_host_library(**kwargs)
    return refresh_catalog(**kwargs)


def _follow_library(**kwargs):
    from .platform_seam import library_source_kind

    if library_source_kind() == "decrypted_archive":
        from .decrypted_archive import follow_decrypted_conversation

        return follow_decrypted_conversation(**kwargs)
    return follow_conversation(**kwargs)


MAX_BODY = 64 * 1024


def _unix_iso(value: float) -> str:
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=value)).isoformat()


def load_config(path: Path) -> dict:
    """Resolve the currently selected LibraryContext for CLI compatibility."""
    return resolve_library_config(path.resolve())


def load_or_create_token(path: Path) -> str:
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        token = payload.get("token")
        if payload.get("version") == 1 and isinstance(token, str) and len(token) >= 32:
            return token
        raise ValueError("invalid API token file")

    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"version": 1, "token": token}, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)
    return token


from .media_loading import set_enabled as set_media_loading, allows_original, mark_manual


class LocalApi:
    def __init__(self, config: dict):
        self.config = config
        index_path = Path(config["index_path"])
        index_path.parent.mkdir(parents=True, exist_ok=True)
        ensure_index_schema(index_path)
        if 'auto_media_loading' in config:
            set_media_loading(index_path, config['auto_media_loading'])
        bootstrap = sqlite3.connect(index_path)
        try:
            ensure_metadata_schema(bootstrap)
            bootstrap.commit()
        finally:
            bootstrap.close()
        self.repository = WeChatRepository(
            index_path,
            config["account"],
        )
        self.refresh_lock = threading.Lock()
        self.project_root = Path(config["project_root"])
        self.sync_state = SyncState(self.project_root)
        self.wechat_root = Path(config["wechat_root"])
        self.account = config["account"]
        self.payment_states = PaymentStateReader(
            self.wechat_root / self.account / 'db_storage' / 'general' / 'general.db',
            self.project_root / 'data' / 'runtime',
            Path(config.get('candidates_path') or self.project_root / 'secrets' / 'candidate_keys.json'),
        )
        if sys.platform == 'darwin':
            from .mac_special import MacPaymentStateReader
            self.payment_states = MacPaymentStateReader(self.project_root, self.account)
        self.image_resources = ImageResourceLookup(
            self.wechat_root / self.account, index_path,
            self.project_root / 'data' / 'runtime',
            Path(config.get('candidates_path') or self.project_root / 'secrets' / 'candidate_keys.json'),
        )
        self.media_cache_root = self.project_root / "data" / "media-cache"
        self.automatic_media = AutomaticMedia(self)
        if sys.platform == 'darwin':
            from .mac_completion import MacAutomaticMedia
            self.automatic_media = MacAutomaticMedia(self)
        self.media_summary = MediaDownloadSummary(index_path, self.account, self.automatic_media.path)
        self.image_keys_path = self.project_root / "secrets" / "image_keys.json"
        self.voice_decoder_path = (
            Path(config["voice_decoder_path"]).resolve()
            if config.get("voice_decoder_path")
            else None
        )
        # Forward-only metadata migration. This also creates the persistent
        # conversation_selection table used by API/CLI/Desktop.
        list_selections(Path(config["index_path"]), self.account)

    def _media_resolver(self) -> MediaResolver:
        if sys.platform == 'darwin':
            from .mac_media import media_resolver
            resolver = media_resolver(self.wechat_root, self.account, self.project_root,
                                      Path(self.config['index_path']), self.image_keys_path)
            # Reading an already-local Mac original is not a network download.
            # Always prefer it, including older messages outside download ranges.
            return resolver
        resolver = MediaResolver(
            self.wechat_root,
            self.account,
            media_db_paths=latest_decrypted_media_databases(
                self.project_root, self.account
            ),
            image_keys_path=self.image_keys_path,
            image_resources=self.image_resources,
            voice_decoder_path=self.voice_decoder_path,
        )

        resolver.original_allowed = lambda uid: allows_original(Path(self.config['index_path']), uid)
        return resolver

    def _avatar_resolver(self) -> AvatarResolver:
        return AvatarResolver(
            self.project_root,
            self.account,
            self.media_cache_root,
        )

    def _avatar_fields(self, usernames) -> dict:
        names = sorted({str(name) for name in usernames if name})
        resolver = self._avatar_resolver()
        local = resolver.export_many(names)
        from .platform_seam import library_source_kind
        if library_source_kind() == 'decrypted_archive':
            from .mac_metadata import avatar_fields
            return avatar_fields(resolver, names, local)
        return {name: {'avatar_path': path} for name, path in local.items()}

    def status(self) -> dict:
        payload = self.repository.status()
        state_path = self.project_root / 'data' / 'state' / 'hourly_refresh.json'
        try:
            archive = json.loads(state_path.read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            archive = {}
        payload['sync'] = self.sync_state.read()
        payload['archive_last_success_at'] = archive.get('last_success_at')
        payload['archive_message_databases'] = int(archive.get('message_databases') or 0)
        try:
            payload['avatar_sync'] = json.loads((self.project_root / 'data' / 'state' / 'avatar_sync.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            payload['avatar_sync'] = None
        payload['recent_media_ready'] = 0
        payload['recent_media_limited'] = 0
        ranges = self._media_ranges()
        since, until = ranges.envelope() if ranges else local_day_window(int(self.config.get('media_prefetch_days') or 14))
        try:
            con = sqlite3.connect(
                f'file:{Path(self.config["index_path"]).resolve().as_posix()}?mode=ro',
                uri=True,
            )
            try:
                con.create_function('media_in_range', 2, ranges.contains if ranges else lambda name, timestamp: True)
                rows = con.execute(
                    '''SELECT e.status, COUNT(*) FROM media_exports e
                       JOIN messages m ON m.uid=e.uid
                       JOIN conversations c ON c.account=m.account AND c.chat_table=m.chat_table
                       WHERE m.account=? AND m.create_time>=? AND m.create_time<?
                       AND media_in_range(c.username,m.create_time)
                       GROUP BY e.status''',
                    (self.account, since, until),
                ).fetchall()
                for status, count in rows:
                    key = 'recent_media_ready' if status in {'ready', 'ready_audio'} else 'recent_media_limited'
                    payload[key] += int(count)
            finally:
                con.close()
        except (OSError, sqlite3.Error):
            pass
        payload['media_downloads'] = self.media_summary.read()
        return payload

    def conversations(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        query: str | None = None,
        selection_state: str | None = None,
    ) -> list[dict]:
        rows = self.repository.list_conversations(
            limit=limit,
            offset=offset,
            query=query,
            selection_state=selection_state,
        )
        avatars = self._avatar_fields(
            row.get("username") for row in rows
        )
        for row in rows:
            row.update(avatars.get(row.get("username"), {"avatar_path": None}))
        return rows

    def contacts(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        query: str | None = None,
    ) -> list[dict]:
        rows = self.repository.list_contacts(
            limit=limit,
            offset=offset,
            query=query,
        )
        avatars = self._avatar_fields(
            row.get("username") for row in rows
        )
        for row in rows:
            row.update(avatars.get(row.get("username"), {"avatar_path": None}))
        return rows

    def search(
        self,
        query: str,
        *,
        limit: int = 50,
    ) -> dict:
        payload = self.repository.search_messages(query, limit=limit)
        usernames: list[str | None] = []
        for item in payload.get("items", []):
            usernames.extend(
                [
                    item.get("chat", {}).get("username"),
                    item.get("sender", {}).get("username"),
                ]
            )
        avatars = self._avatar_fields(usernames)
        for item in payload.get("items", []):
            chat = item.get("chat") or {}
            sender = item.get("sender") or {}
            chat.update(avatars.get(chat.get("username"), {"avatar_path": None}))
            sender.update(avatars.get(sender.get("username"), {"avatar_path": None}))
        return payload

    def messages(
        self,
        username: str,
        *,
        limit: int = 80,
        before: int | None = None,
        before_uid: str | None = None,
        around_uid: str | None = None,
        after_uid: str | None = None,
        at_time: int | None = None,
        category: str | None = None,
        query: str | None = None,
        from_day: int | None = None,
        to_day: int | None = None,
        sender: str | None = None,
        filter_offset: int = 0,
    ) -> dict:
        payload = self.repository.list_messages(
            username,
            limit=limit,
            before=before,
            before_uid=before_uid,
            around_uid=around_uid,
            after_uid=after_uid,
            at_time=at_time,
            category=category,
            query=query,
            from_day=from_day,
            to_day=to_day,
            sender=sender,
            filter_offset=filter_offset,
        )
        chat_username = payload["conversation"]["username"]
        envelope_ids = self.repository.red_envelope_ids(chat_username)
        if envelope_ids:
            states = self.payment_states.states()
            payload['payment_states'] = {uid: states.get((chat_username, sid)) for uid, sid in envelope_ids.items()}
            for item in payload['messages']:
                if item['type']['kind'] == 'red_envelope':
                    item['payment'] = {
                        **(item.get('payment') or {}),
                        'local_state': states.get((chat_username, str(item['source']['server_id']))),
                    }
        avatar_usernames = [chat_username]
        avatar_usernames.extend(
            item.get("sender", {}).get("username")
            for item in payload["messages"]
        )
        if sys.platform == 'darwin':
            avatar_usernames.extend(
                target.get('sender', {}).get('username') for item in payload['messages']
                if (target := (item.get('quote') or {}).get('target'))
            )
        avatars = self._avatar_fields(avatar_usernames)
        payload["conversation"].update(avatars.get(chat_username, {"avatar_path": None}))
        payload["selection"] = selection_status(
            Path(self.config["index_path"]),
            self.account,
            username,
        )
        resolver = (
            self._media_resolver()
            if payload["selection"]["state"] in {"following", "paused"}
            else None
        )
        quote_targets = {target['uid']: target for item in payload['messages']
                         if (target := (item.get('quote') or {}).get('target'))}
        expired_ids = self.automatic_media.expired_ids(
            [item['uid'] for item in [*payload['messages'], *quote_targets.values()]]) if resolver else set()
        for item in [*payload['messages'], *quote_targets.values()]:
            sender = item.get("sender") or {}
            sender.update(avatars.get(sender.get("username"), {"avatar_path": None}))
            if sys.platform == 'darwin' and resolver is not None:
                from .mac_completion import enrich
                enrich(self, item)
            media = item.get("media")
            if not media or resolver is None:
                continue
            kind = item["type"]["kind"]
            source = item["source"]
            artifact = None
            if kind == "image":
                artifact = resolver.export_image(
                    source["table"],
                    item["timestamp"],
                    media.get("md5"),
                    item["uid"],
                    self.media_cache_root,
                )
            elif kind == "video":
                artifact = resolver.export_video(
                    item["timestamp"],
                    media.get("md5"),
                    item["uid"],
                    self.media_cache_root,
                )
            elif kind == "voice":
                artifact = resolver.export_voice(
                    chat_username,
                    source.get("server_id"),
                    item["uid"],
                    self.media_cache_root,
                )
            elif kind == "file":
                artifact = resolver.export_file(
                    item["timestamp"],
                    item.get("title"),
                    item["uid"],
                    self.media_cache_root,
                )
            if artifact is not None:
                item["media"] = artifact.to_dict()
                if artifact.status != 'ready' and item['uid'] in expired_ids:
                    item['media']['download_error'] = 'cdn_expired'
                elif sys.platform == 'darwin' and kind in {'image', 'video'}:
                    from .mac_media import media_error
                    if error := media_error(artifact):
                        item['media']['download_error'] = error
                if sys.platform == 'darwin':
                    from .mac_completion import enrich
                    enrich(self, item)
        remember_media(Path(self.config['index_path']), self.account,
                       [*payload['messages'], *quote_targets.values()])
        return payload

    def recheck_media(self, uid: str, *, should_continue=None) -> dict:
        if sys.platform == 'darwin':
            from .mac_completion import recheck
            result = recheck(self, uid, should_continue=should_continue)
            remember_media(Path(self.config['index_path']), self.account, result['messages'])
            return result
        found = self.repository.message_by_uid(uid)
        if found is None:
            raise ValueError('message_not_found')
        item = found['message']
        selected = selection_status(Path(self.config['index_path']), self.account, found['conversation']['username'])
        if selected['state'] not in {'following', 'paused'}:
            raise ValueError('conversation_not_followed')
        kind = item['type']['kind']
        if kind not in {'image', 'video'}:
            raise ValueError('unsupported_media_kind')
        resolver = self._media_resolver()
        resolver.force_copy = True
        resolver.original_allowed = None
        args = (item['timestamp'], (item.get('media') or {}).get('md5'), uid, self.media_cache_root)
        artifact = (resolver.export_image(item['source']['table'], *args)
                    if kind == 'image' else resolver.export_video(*args))
        download_error = None
        if artifact.status != 'ready' and sys.platform == 'darwin':
            from .mac_media import media_error
            download_error = media_error(artifact)
        elif artifact.status != 'ready':
            try:
                download_media(self.config, uid, kind,
                               **({'should_continue':should_continue} if should_continue else {}))
                artifact = (resolver.export_image(item['source']['table'], *args)
                            if kind == 'image' else resolver.export_video(*args))
            except DownloadError as exc:
                download_error = str(exc)
        if artifact.status == 'ready' and should_continue is None:
            mark_manual(Path(self.config['index_path']), uid)
        item['media'] = artifact.to_dict()
        if download_error:
            item['media']['download_error'] = download_error
        if should_continue is None:
            self.automatic_media.record_manual_result(uid, item['media'])
        remember_media(Path(self.config['index_path']), self.account, [item])
        return {'conversation': found['conversation'], 'selection': selected, 'messages': [item]}

    def group_members(self, username: str) -> dict:
        contact_db = latest_decrypted_database(
            self.project_root, self.account, 'contact', 'contact',
        )
        if contact_db is None:
            return {'available': False, 'members': []}
        members = self.repository.group_members(username, contact_db)
        avatars = self._avatar_fields(
            member['username'] for member in members
        )
        for member in members:
            member.update(avatars.get(member['username'], {'avatar_path': None}))
        return {'available': True, 'members': members}



    def follow(self, username: str) -> dict:
        with self.refresh_lock:
            from .platform_seam import library_source_kind

            # Mac already indexes the complete local archive in the background.
            # Following must remain usable while the live WeChat source is
            # unavailable (for example after an ad-hoc update needs FDA again).
            # Windows still captures its raw tables before selective indexing.
            if library_source_kind() != "decrypted_archive":
                _refresh_library(
                    wechat_root=self.wechat_root,
                    account=self.account,
                    project_root=self.project_root,
                    candidates_path=Path(self.config["candidates_path"]),
                    index_path=Path(self.config["index_path"]),
                    overlap_local_ids=int(self.config.get("overlap_local_ids", 200)),
                    allow_memory_rescan=bool(self.config.get("allow_memory_rescan", True)),
                    refresh_selected=False,
                )
            result = _follow_library(
                wechat_root=self.wechat_root,
                account=self.account,
                project_root=self.project_root,
                candidates_path=Path(self.config["candidates_path"]),
                index_path=Path(self.config["index_path"]),
                username=username,
                overlap_local_ids=int(self.config.get("overlap_local_ids", 200)),
                allow_memory_rescan=bool(self.config.get("allow_memory_rescan", True)),
            )
            result["media_prefetch"] = self._prefetch_followed_media(
                username=username, force_image_scan=True
            )
        return result

    def _media_ranges(self):
        added = self.config.get('library_added_at')
        return MediaRanges(self.project_root, added) if added else None

    def _prefetch_followed_media(
        self, *, username: str | None = None, force_image_scan: bool = False
    ) -> dict:
        try:
            image_key_status = ('ready' if self._media_resolver().image_keys else 'unavailable') if sys.platform == 'darwin' else recover_image_key(
                account_root=self.wechat_root / self.account,
                key_path=self.image_keys_path,
                state_path=self.project_root / 'data' / 'state' / 'image_key_scan.json',
                force=force_image_scan,
            )
        except (OSError, ValueError) as exc:
            image_key_status = f'error:{exc.__class__.__name__}'
        try:
            payload = prefetch_recent_media(
                index_path=Path(self.config['index_path']),
                account=self.account,
                resolver=self._media_resolver(),
                destination_root=self.media_cache_root,
                username=username,
                days=int(self.config.get('media_prefetch_days') or 14),
                ranges=self._media_ranges(),
            )
            return {'ok': True, 'image_key_status': image_key_status, **payload}
        except (OSError, ValueError, sqlite3.Error) as exc:
            return {'ok': False, 'image_key_status': image_key_status,
                    'error': exc.__class__.__name__}

    def pause(self, username: str) -> dict:
        return pause_conversation(
            Path(self.config["index_path"]),
            self.account,
            username,
        )

    def selections(self) -> list[dict]:
        return list_selections(
            Path(self.config["index_path"]),
            self.account,
        )


    def refresh(
        self, *, trigger: str = 'manual', deadline_monotonic=None,
        deadline_at=None,
    ) -> dict:
        if not self.refresh_lock.acquire(blocking=False):
            return {"ok": False, "error": "refresh_busy"}
        started_at = now_iso()
        started_monotonic = time.monotonic()

        def complete_receipt(outcome, error=None, failed_conversations=0):
            finished_at = now_iso()
            self.sync_state.record_receipt(
                trigger=trigger, started_at=started_at, finished_at=finished_at,
                duration_seconds=time.monotonic() - started_monotonic,
                outcome=outcome, error=error,
                failed_conversations=failed_conversations,
            )
            return finished_at

        def stage(name):
            self.sync_state.update(stage=name, stage_started_at=now_iso())

        def current_deadline_monotonic():
            return deadline_monotonic() if callable(deadline_monotonic) else deadline_monotonic

        def current_deadline_at():
            return deadline_at() if callable(deadline_at) else deadline_at

        def deadline_reached(next_stage, archive_success):
            deadline = current_deadline_monotonic()
            if deadline is None or time.monotonic() < deadline:
                return None
            finished = now_iso()
            self.sync_state.update(
                state='ready', finished_at=finished, last_success_at=finished,
                error='deadline_reached', stage='complete',
                deadline_at=current_deadline_at(),
                deadline_exceeded_at=finished, deferred_stage=next_stage,
                failed_conversations=0, archive_success_at=archive_success,
            )
            self.sync_state.record_receipt(
                trigger=trigger, started_at=started_at, finished_at=finished,
                duration_seconds=time.monotonic() - started_monotonic,
                outcome='success', error='deadline_reached', failed_conversations=0,
            )
            return finished

        try:
            self.sync_state.update(
                state='running', started_at=started_at, error=None,
                stage='catalog', stage_started_at=started_at,
                deadline_at=current_deadline_at(), deadline_exceeded_at=None,
                deferred_stage=None, failed_conversations=0,
            )
            result = _refresh_library(
                wechat_root=self.wechat_root,
                account=self.account,
                project_root=self.project_root,
                candidates_path=Path(self.config["candidates_path"]),
                index_path=Path(self.config["index_path"]),
                overlap_local_ids=int(self.config.get("overlap_local_ids", 200)),
                allow_memory_rescan=bool(self.config.get("allow_memory_rescan", True)),
                refresh_selected=True,
            )
            archive_success = (result.get('archive_status') or {}).get('last_success_at')
            failed_conversations = sum(
                isinstance(item, dict) and item.get('ok') is False
                for item in (result.get('selected_refresh') or [])
            )
            if failed_conversations:
                finished = complete_receipt(
                    'partial', 'selected_refresh_partial', failed_conversations,
                )
                self.sync_state.update(
                    state='partial', finished_at=finished,
                    error='selected_refresh_partial', stage='complete',
                    failed_conversations=failed_conversations,
                    archive_success_at=archive_success,
                )
                return {
                    'ok': False, 'error': 'selected_refresh_partial',
                    'archive_ok': True, 'complete': False,
                    'failed_conversations': failed_conversations,
                    'result': result, 'status': self.repository.status(),
                }

            if deadline_reached('avatar_sync', archive_success):
                return {
                    'ok': True, 'archive_ok': True, 'complete': True,
                    'maintenance_deferred': 'avatar_sync', 'result': result,
                    'status': self.repository.status(),
                }
            stage('avatar_sync')
            try:
                avatar_sync = sync_followed_avatars(Path(self.config['index_path']), self._avatar_resolver())
            except (OSError, ValueError, sqlite3.Error) as exc:
                avatar_sync = {'error': exc.__class__.__name__}
            media_prefetch = None
            if deadline_reached('media_prefetch', archive_success):
                return {
                    'ok': True, 'archive_ok': True, 'complete': True,
                    'maintenance_deferred': 'media_prefetch', 'result': result,
                    'avatar_sync': avatar_sync, 'status': self.repository.status(),
                }
            stage('media_prefetch')
            if self.repository.status().get('following_conversations'):
                media_prefetch = self._prefetch_followed_media()
            if deadline_reached('cleanup', archive_success):
                return {
                    'ok': True, 'archive_ok': True, 'complete': True,
                    'maintenance_deferred': 'cleanup', 'result': result,
                    'avatar_sync': avatar_sync, 'media_prefetch': media_prefetch,
                    'status': self.repository.status(),
                }
            stage('cleanup')
            cleanup = prune_snapshot_generations(
                self.project_root, self.account,
                encrypted_keep=2, decrypted_keep=1, apply=True,
            )
            finished = complete_receipt('success')
            self.sync_state.update(
                state='ready', finished_at=finished, last_success_at=finished,
                error=None, stage='complete', failed_conversations=0,
                archive_success_at=archive_success, deferred_stage=None,
            )
            return {
                "ok": True,
                "archive_ok": True,
                "complete": True,
                "result": result,
                "media_prefetch": media_prefetch,
                "avatar_sync": avatar_sync,
                "snapshot_generations_removed": cleanup.generations_removed,
                "status": self.repository.status(),
            }
        except Exception as exc:
            # Exception text may contain paths, account IDs or source data.
            category = ('source_permission_required' if getattr(exc, 'code', None) == 'source_permission_required' else
                        'key_unavailable' if isinstance(exc, KeyUnavailable) else
                        'source_unavailable' if isinstance(exc, FileNotFoundError) else 'sync_failed')
            try:
                finished = complete_receipt('failed', category)
                self.sync_state.update(
                    state='failed', finished_at=finished, error=category,
                )
            except OSError:
                pass
            raise
        finally:
            self.refresh_lock.release()
            wake = getattr(self, '_automatic_wake', None)
            if wake is not None:
                wake.set()


class MultiLibraryApi:
    def __init__(self, config_path: Path):
        self.config_path = config_path.resolve()
        self.root_config = load_root_config(self.config_path)
        self.registry = registry_from_config(self.config_path)
        if sys.platform == 'darwin':
            from .mac_accounts import MacAccounts
            self.mac_accounts = MacAccounts(self)
        self._cores: dict[str, LocalApi] = {}
        self._candidate_cache: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._automatic_stop = threading.Event()
        self._automatic_wake = threading.Event()
        self._automatic_thread = None
        self._periodic_stop = threading.Event()
        self._periodic_thread: threading.Thread | None = None

    def _active_records(self):
        if sys.platform == 'darwin':
            try:
                return [self.registry.current()]
            except KeyError:
                return []
        return self.registry.list()

    def start_automatic_media(self) -> None:
        if self._automatic_thread and self._automatic_thread.is_alive():
            return
        self._automatic_stop.clear()
        from .media_worker import run_media_worker
        def loop():
            run_media_worker(self._active_records, self._core,
                             self._automatic_stop, self._automatic_wake)
        self._automatic_thread = threading.Thread(target=loop, name='automatic-media', daemon=True)
        self._automatic_thread.start()

    def stop_automatic_media(self) -> None:
        self._automatic_stop.set()
        self._automatic_wake.set()
        if self._automatic_thread:
            self._automatic_thread.join(timeout=2)

    def start_periodic_refresh(self) -> None:
        if self._periodic_thread and self._periodic_thread.is_alive():
            return

        def loop() -> None:
            retry_delay = 1.0
            trigger = 'startup'

            def retry(category):
                nonlocal retry_delay, trigger
                self._periodic_error = category
                self._periodic_round_running = False
                self._periodic_next_at = time.time() + retry_delay
                self._periodic_stop.wait(retry_delay)
                retry_delay = min(30.0, retry_delay * 2)
                trigger = 'retry'

            while not self._periodic_stop.is_set():
                if sys.platform == "darwin":
                    try:
                        from .mac_auto import shared_auto_job
                        if shared_auto_job().is_running():
                            self._periodic_stop.wait(1)
                            continue
                        from .mac_entry import arm_macos_key_check

                        arm_macos_key_check()
                    except Exception:
                        pass  # A key check must not stop the archive refresh.
                try:
                    interval = self.settings()['sync_interval_seconds']
                except Exception:
                    retry('settings_unavailable')
                    continue

                round_started_wall = time.time()
                round_started_monotonic = time.monotonic()
                deadline_wall = round_started_wall + interval
                deadline_monotonic = round_started_monotonic + interval
                self._periodic_round_started_wall = round_started_wall
                self._periodic_round_started_monotonic = round_started_monotonic
                self._periodic_deadline_monotonic = deadline_monotonic
                self._periodic_last_started = round_started_wall
                self._periodic_next_at = deadline_wall
                self._periodic_trigger = trigger
                self._periodic_round_running = True
                try:
                    records = self._active_records()
                except Exception:
                    retry('registry_unavailable')
                    continue

                retry_delay = 1.0
                self._periodic_error = None
                for record in records:
                    if self._periodic_stop.is_set():
                        break
                    try:
                        core = self._core(record.id)
                        payload = core.refresh(
                            trigger=trigger,
                            deadline_monotonic=lambda: self._periodic_deadline_monotonic,
                            deadline_at=lambda: _unix_iso(self._periodic_next_at),
                        )
                        if not payload.get('ok'):
                            continue
                        status = payload['status']
                        self.registry.update(
                            record.id,
                            display_name=status.get('self_display_name') or record.display_name,
                            state='ready',
                            initialized=True,
                            last_error=None,
                        )
                    except Exception as exc:
                        try:
                            self.registry.update(record.id, last_error=exc.__class__.__name__)
                        except (OSError, ValueError, KeyError):
                            pass  # Storage failure must not permanently kill the scheduler.
                self._periodic_last_completed = time.time()
                self._periodic_round_running = False
                trigger = 'interval'
                while not self._periodic_stop.is_set():
                    try:
                        interval = self.settings()['sync_interval_seconds']
                    except Exception:
                        self._periodic_error = 'settings_unavailable'
                        self._periodic_stop.wait(retry_delay)
                        retry_delay = min(30.0, retry_delay * 2)
                        continue
                    retry_delay = 1.0
                    deadline_wall = round_started_wall + interval
                    deadline_monotonic = round_started_monotonic + interval
                    self._periodic_next_at = deadline_wall
                    self._periodic_deadline_monotonic = deadline_monotonic
                    wall_remaining = deadline_wall - time.time()
                    monotonic_remaining = deadline_monotonic - time.monotonic()
                    if wall_remaining <= 0 or monotonic_remaining <= 0:
                        trigger = 'resume' if wall_remaining <= 0 < monotonic_remaining else 'interval'
                        break
                    # New messages and login/network recovery are read at the
                    # next deadline. Only explicit manual/AI requests bypass it.
                    self._periodic_stop.wait(min(1.0, wall_remaining, monotonic_remaining))

        self._periodic_stop.clear()
        self._periodic_round_running = False
        self._periodic_next_at = None
        self._periodic_error = None
        self._periodic_thread = threading.Thread(
            target=loop,
            name="noteford-periodic-refresh",
            daemon=True,
        )
        self._periodic_thread.start()

    def stop_periodic_refresh(self) -> None:
        self._periodic_stop.set()
        thread = self._periodic_thread
        if thread and thread.is_alive():
            thread.join(timeout=2)
        if not thread or not thread.is_alive():
            self._periodic_thread = None

    def _context_config(self, library_id: str | None = None) -> dict:
        config = self.registry.context(library_id).config_dict()
        for key, value in self.root_config.items():
            if key in {'media_prefetch_days', 'auto_media_loading', 'auto_media_max_mb', 'parse_stickers'}:
                config[key] = value
        return config

    def settings(self) -> dict:
        stored = int(self.root_config.get("auto_refresh_interval_seconds") or 300)
        seconds = 10 if stored == 10 else min(86400, max(300, ((stored + 299) // 300) * 300))
        return {
            'auto_media_loading': self.root_config.get('auto_media_loading', True),
            'auto_media_max_mb': self.root_config.get('auto_media_max_mb', 0),
            'parse_stickers': self.root_config.get('parse_stickers', True) is not False,
            "media_prefetch_days": int(self.root_config.get('media_prefetch_days') or 14),
            "sync_interval_seconds": seconds,
            "sync_interval_minutes": seconds / 60 if seconds == 10 else seconds // 60,
        }



    def update_auto_media_loading(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('auto_media_loading_must_be_boolean')
        with self._lock:
            old = self.root_config.get('auto_media_loading', True)
            changed = []
            try:
                for record in self.registry.list():
                    core = self._core(record.id)
                    set_media_loading(Path(core.config['index_path']), enabled)
                    changed.append(core)
                payload = json.loads(self.config_path.read_text(encoding='utf-8'))
                payload['auto_media_loading'] = enabled
                temporary = self.config_path.with_suffix('.media-loading.tmp')
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
                os.replace(temporary, self.config_path)
            except Exception:
                for core in changed:
                    set_media_loading(Path(core.config['index_path']), old)
                raise
            self.root_config['auto_media_loading'] = enabled
            for core in changed:
                core.config['auto_media_loading'] = enabled
        return self.settings()

    def update_auto_media_limit(self, limit):
        if type(limit) is not int or limit not in {0, 20, 30, 50}:
            raise ValueError('invalid_auto_media_limit')
        with self._lock:
            payload = json.loads(self.config_path.read_text(encoding='utf-8'))
            payload['auto_media_max_mb'] = limit
            temporary = self.config_path.with_suffix('.media-limit.tmp')
            try:
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
                os.replace(temporary, self.config_path)
            finally:
                temporary.unlink(missing_ok=True)
            self.root_config['auto_media_max_mb'] = limit
            for core in self._cores.values():
                core.config['auto_media_max_mb'] = limit
            self._automatic_wake.set()
        return self.settings()

    def update_sticker_parsing(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError('parse_stickers_must_be_boolean')
        with self._lock:
            payload = json.loads(self.config_path.read_text(encoding='utf-8'))
            payload['parse_stickers'] = enabled
            temporary = self.config_path.with_suffix('.stickers.tmp')
            try:
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
                os.replace(temporary, self.config_path)
            finally:
                temporary.unlink(missing_ok=True)
            # Existing workers hold these config objects. Publish only after the
            # durable write succeeds; chunk callbacks observe a disabled setting.
            self.root_config['parse_stickers'] = enabled
            for core in self._cores.values():
                core.config['parse_stickers'] = enabled
            self._automatic_wake.set()
        return self.settings()

    def update_sync_interval(self, minutes: int) -> dict:
        if type(minutes) is not int or minutes < 5 or minutes > 1440 or minutes % 5:
            raise ValueError('同步间隔必须为 5 分钟至 24 小时，且为 5 分钟的整数倍')
        return self.update_sync_interval_seconds(minutes * 60)

    def update_sync_interval_seconds(self, seconds: int) -> dict:
        if type(seconds) is not int or (seconds != 10 and (seconds < 300 or seconds > 86400 or seconds % 300)):
            raise ValueError('同步间隔可选 10 秒，或 5 分钟至 24 小时的 5 分钟整数倍')
        with self._lock:
            payload = json.loads(self.config_path.read_text(encoding='utf-8'))
            payload['auto_refresh_interval_seconds'] = seconds
            temporary = self.config_path.with_suffix('.sync.tmp')
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(temporary, self.config_path)
            self.root_config['auto_refresh_interval_seconds'] = seconds
            started_wall = getattr(self, '_periodic_round_started_wall', None)
            started_monotonic = getattr(self, '_periodic_round_started_monotonic', None)
            if started_wall is not None and started_monotonic is not None:
                self._periodic_next_at = started_wall + seconds
                self._periodic_deadline_monotonic = started_monotonic + seconds
        return self.settings()

    def conversation_media_range(self, library_id, username, *, update=None):
        if not isinstance(library_id, str) or not library_id or not isinstance(username, str) or not username:
            raise ValueError('library_and_conversation_required')
        core = self._core(library_id)
        if core.repository.conversation(username) is None:
            raise ValueError('conversation_not_found')
        with self._lock:
            ranges = core._media_ranges()
            if ranges is None:
                raise ValueError('account_added_date_unavailable')
            value = ranges.get(username)
            if update is not None:
                value = ranges.save(username, update.get('start'), update.get('end'))
        if update is not None:
            def prefetch():
                with core.refresh_lock:
                    core._prefetch_followed_media(username=username)
            threading.Thread(target=prefetch, name='conversation-media-range', daemon=True).start()
            self._automatic_wake.set()
        return {**value, 'library_id': library_id, 'conversation': username,
                'automatic': core.automatic_media.summary(username)}

    def update_media_window(self, days: int) -> dict:
        if self.registry.list():
            raise ValueError('请在会话顶部设置此会话的媒体日期范围')
        if type(days) is not int or not 1 <= days <= 365:
            raise ValueError('media_prefetch_days_must_be_1_to_365')
        payload = json.loads(self.config_path.read_text(encoding='utf-8'))
        payload['media_prefetch_days'] = days
        temporary = self.config_path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, self.config_path)
        self.root_config['media_prefetch_days'] = days
        with self._lock:
            for core in self._cores.values():
                core.config['media_prefetch_days'] = days

        def prefetch() -> None:
            for record in self.registry.list():
                try:
                    core = self._core(record.id)
                    with core.refresh_lock:
                        if core.repository.status().get('following_conversations'):
                            core._prefetch_followed_media()
                except Exception:
                    # The next hourly refresh retries this account.
                    continue

        threading.Thread(target=prefetch, name='noteford-media-window', daemon=True).start()
        return self.settings()


    def _current_record(self):
        if sys.platform == 'darwin':
            from .mac_accounts import request_library
            library_id = request_library.get()
            if library_id:
                return self.registry.get(library_id)
        return self.registry.current()

    def _core(self, library_id: str | None = None) -> LocalApi:
        record = self.registry.get(library_id) if library_id else self._current_record()
        with self._lock:
            core = self._cores.get(record.id)
            if core is None:
                core = LocalApi(self._context_config(record.id))
                core._automatic_wake = self._automatic_wake
                self._cores[record.id] = core
            return core

    def _enrich_library(self, record) -> dict:
        item = record.public_dict(
            current=(record.id == self.registry.current().id)
        )
        try:
            core = self._core(record.id)
            status = core.status()
            item["messages"] = int(status.get("messages") or 0)
            item["conversations"] = int(status.get("conversations") or 0)
            item["following_conversations"] = int(
                status.get("following_conversations") or 0
            )
            self_username = status.get("self_username")
            if self_username:
                avatars = core._avatar_fields([self_username])
                item.update(avatars.get(self_username, {"avatar_path": None}))
            else:
                item["avatar_path"] = None
        except Exception:
            item["messages"] = 0
            item["conversations"] = 0
            item["following_conversations"] = 0
            item["avatar_path"] = None
        return item

    def libraries(self) -> list[dict]:
        return [self._enrich_library(record) for record in self._active_records()]

    def discover_candidates(self, wechat_root: str | None = None) -> list[dict]:
        from .workspace_access import require_source
        if wechat_root:
            require_source(Path(self.root_config['project_root']), Path(wechat_root))
        if os.environ.get('NOTEFORD_SMOKE') == '1':
            return []
        from .platform_seam import discover_library_candidates, library_source_kind

        if library_source_kind() == "decrypted_archive":
            rows = discover_library_candidates(
                wechat_root=Path(wechat_root) if wechat_root else None,
                configured=self.root_config.get("wechat_roots") or [],
            )
        else:
            roots = (
                [Path(wechat_root)]
                if wechat_root
                else candidate_roots(self.root_config.get("wechat_roots") or [])
            )
            from .workspace_access import source_allowed
            roots = [root for root in roots if source_allowed(Path(self.root_config['project_root']), root)]
            rows = scan_library_candidates(roots)
        with self._lock:
            for item in rows:
                previous = self._candidate_cache.get(item["id"])
                if (
                    previous
                    and item["preview_status"] == "unavailable"
                    and previous["preview_status"] != "unavailable"
                ):
                    item["display_name"] = previous["display_name"]
                    item["avatar_data_url"] = previous["avatar_data_url"]
                    item["preview_status"] = previous["preview_status"]
            self._candidate_cache = {item["id"]: item for item in rows}
        return rows

    def register_candidate(self, library_id: str) -> dict:
        with self._lock:
            candidate = self._candidate_cache.get(library_id)
        if candidate is None:
            raise ValueError("candidate_not_found")
        already_registered = any(
            record.id == library_id for record in self.registry.list()
        )
        from .platform_seam import library_source_kind, register_discovered_library

        if (
            library_source_kind() == "decrypted_archive"
            or candidate.get("source_kind") == "decrypted_archive"
        ):
            record = register_discovered_library(self.registry, candidate)
        else:
            record = self.registry.register_account(
                Path(candidate["source_root"]), candidate["account"]
            )
        if not already_registered and candidate["preview_status"] in {"name", "ready"}:
            record = self.registry.update(
                record.id,
                display_name=candidate["display_name"],
                last_error=record.last_error,
            )
        return self._enrich_library(record)

    def discover_libraries(self, wechat_root: str | None = None) -> list[dict]:
        from .workspace_access import require_source
        if wechat_root:
            require_source(Path(self.root_config['project_root']), Path(wechat_root))
        from .platform_seam import discover_library_candidates, library_source_kind, register_discovered_library

        if library_source_kind() == "decrypted_archive":
            rows = discover_library_candidates(
                wechat_root=Path(wechat_root) if wechat_root else None,
                configured=self.root_config.get("wechat_roots") or [],
            )
            for item in rows:
                register_discovered_library(self.registry, item)
            return self.libraries()
        roots: list[Path] = []
        if wechat_root:
            roots.append(Path(wechat_root))
        else:
            roots.extend(Path(value) for value in self.root_config.get("wechat_roots") or [])
        for root in roots:
            self.registry.discover(root)
        return self.libraries()

    def switch_library(self, library_id: str, *, initialize: bool = True) -> dict:
        record = self.registry.switch(library_id)
        core = self._core(record.id)
        initialized_now = False
        error = None
        if initialize and not record.initialized_at:
            try:
                initial = core.refresh()
                if not initial.get('ok'):
                    raise RuntimeError('refresh_busy')
                status = core.status()
                display_name = (
                    status.get("self_display_name")
                    or record.display_name
                )
                self.registry.update(
                    record.id,
                    display_name=display_name,
                    state="ready",
                    initialized=True,
                    last_error=None,
                )
                initialized_now = True
            except Exception as exc:
                error = exc.__class__.__name__
                self.registry.update(
                    record.id,
                    state="locked",
                    last_error=error,
                )
        record = self.registry.get(record.id)
        return {
            "library": self._enrich_library(record),
            "status": self.status(),
            "initialized_now": initialized_now,
            "initialization_error": error,
        }

    def rename_library(self, library_id: str, display_name: str) -> dict:
        record = self.registry.update(
            library_id,
            display_name=display_name,
            last_error=self.registry.get(library_id).last_error,
        )
        return self._enrich_library(record)

    def status(self) -> dict:
        try:
            record = self._current_record()
        except KeyError:
            return {
                "library_id": None,
                "library_display_name": "尚未添加微信资料库",
                "library_state": "empty",
                "messages": 0,
                "conversations": 0,
                "contacts": 0,
                "following_conversations": 0,
                "paused_conversations": 0,
                "managed_messages": 0,
                "legacy_unselected_messages": 0,
                "latest_timestamp": 0,
                "latest_time": None,
                "self_identity_resolved": False,
                "self_display_name": None,
                "self_confidence": 0,
                "archive_last_success_at": None,
                "archive_message_databases": 0,
                "recent_media_ready": 0,
                "recent_media_limited": 0,
                "media_downloads": empty_summary(),
            }
        payload = self._core(record.id).status()
        payload["library_id"] = record.id
        payload["library_display_name"] = record.display_name
        payload["library_state"] = record.state
        if sys.platform == 'darwin':
            from .mac_accounts import account_source
            payload['wechat_login'] = account_login_status(account_source(record), record.account)
        else:
            payload['wechat_login'] = account_login_status(record.wechat_root, record.account)
        thread = self._periodic_thread
        scheduled = bool(thread and thread.is_alive() and not self._periodic_stop.is_set())
        payload['sync']['scheduler_running'] = scheduled
        try:
            interval_seconds = self.settings()['sync_interval_seconds']
            interval_minutes = interval_seconds / 60
        except Exception:
            interval_minutes = None
            interval_seconds = None
        payload['sync']['interval_minutes'] = interval_minutes
        payload['sync']['interval_seconds'] = interval_seconds
        next_at = getattr(self, '_periodic_next_at', None)
        payload['sync']['next_at'] = (
            _unix_iso(next_at)
            if scheduled and next_at is not None else None
        )
        payload['sync']['scheduler_error'] = (
            getattr(self, '_periodic_error', None)
            or ('settings_unavailable' if interval_minutes is None else None)
        )
        payload['sync']['round_running'] = bool(getattr(self, '_periodic_round_running', False))
        payload['sync']['trigger'] = getattr(self, '_periodic_trigger', None)
        return payload

    def conversations(self, **kwargs):
        if not self.registry.list():
            return []
        return self._core().conversations(**kwargs)

    def contacts(self, **kwargs):
        if not self.registry.list():
            return []
        return self._core().contacts(**kwargs)

    def search(self, *args, **kwargs):
        return self._core().search(*args, **kwargs)

    def messages(self, *args, **kwargs):
        record = self._current_record()
        payload = self._core(record.id).messages(*args, **kwargs)
        payload["library_id"] = record.id
        return payload

    def group_members(self, username: str) -> dict:
        return self._core().group_members(username)



    def follow(self, username: str):
        return self._core().follow(username)

    def pause(self, username: str):
        return self._core().pause(username)

    def selections(self):
        return self._core().selections()


    def refresh(self):
        record = self._current_record()
        core = self._core(record.id)
        payload = core.refresh()
        if not payload.get("ok"):
            return payload
        status = core.status()
        display_name = status.get("self_display_name") or record.display_name
        self.registry.update(
            record.id,
            display_name=display_name,
            state="ready",
            initialized=True,
            last_error=None,
        )
        payload["status"] = self.status()
        return payload


def make_handler(api, token: str):
    from .workspace_access import WorkspaceAccess
    root_config = getattr(api, 'root_config', getattr(api, 'config', {}))
    workspace_access = WorkspaceAccess(Path(root_config.get('project_root', '.')))
    from .mcp_sync import SyncRequests
    sync_requests = SyncRequests(api, api.config_path) if hasattr(api, "config_path") else None
    class Handler(BaseHTTPRequestHandler):
        server_version = "Noteford/0.1"
        sys_version = ""

        def log_message(self, _format: str, *_args) -> None:
            return

        def _json(self, status: int, payload: object) -> None:
            try:
                workspace_access.check()
            except PermissionError:
                status, payload = HTTPStatus.UNAUTHORIZED, {'ok': False, 'error': 'workspace_locked'}
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            try:
                workspace_access.check()
            except PermissionError:
                return False
            presented = self.headers.get("X-Noteford-Token", "")
            return bool(presented) and hmac.compare_digest(presented, token)

        def _require_auth(self) -> bool:
            if self._authorized():
                return True
            self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return False

        @staticmethod
        def _one(params: dict[str, list[str]], key: str, default: str | None = None) -> str | None:
            values = params.get(key)
            return values[0] if values else default

        def _dispatch(self, callback):
            library_id = self.headers.get('X-Noteford-Library')
            if sys.platform != 'darwin' or not library_id or not self._authorized():
                return callback()
            from .mac_accounts import library_scope
            try:
                with library_scope(api.registry, library_id):
                    return callback()
            except KeyError:
                self._json(HTTPStatus.NOT_FOUND, {'ok': False, 'error': 'library_not_found'})

        def do_GET(self) -> None:
            return self._dispatch(self._get)

        def do_POST(self) -> None:
            return self._dispatch(self._post)

        def _get(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/api/v1/health":
                self._json(HTTPStatus.OK, {"ok": True})
                return
            if not self._require_auth():
                return
            if parsed.path == "/api/v1/mac/wechat":
                from .mac_entry import route_status

                self._json(HTTPStatus.OK, route_status())
                return
            if parsed.path == "/api/v1/mac/auto-capture":
                from .mac_entry import route_auto_capture_snapshot

                snap = route_auto_capture_snapshot()
                if snap.get("error") in {"windows_key_path_unchanged", "darwin_required"} and "phase" not in snap:
                    self._json(HTTPStatus.OK, snap)
                else:
                    self._json(HTTPStatus.OK, {"ok": True, "data": snap})
                return

            params = parse_qs(parsed.query, keep_blank_values=False)
            try:
                if parsed.path == '/api/v1/mac/accounts' and sys.platform == 'darwin':
                    self._json(HTTPStatus.OK, {'ok': True, 'data': api.mac_accounts.snapshot()})
                    return
                if parsed.path == '/api/v1/avatars':
                    library = self._one(params, 'library_id')
                    names = json.loads(self._one(params, 'usernames', '[]'))
                    if not library or not isinstance(names, list) or len(names) > 2000 or any(not isinstance(n, str) or len(n) > 256 for n in names):
                        raise ValueError('invalid_avatar_request')
                    avatars = api._core(library)._avatar_fields(names)
                    self._json(HTTPStatus.OK, {'ok': True, 'data': [{'username': n, **fields} for n, fields in avatars.items()]})
                    return

                if parsed.path == "/api/v1/status":
                    self._json(HTTPStatus.OK, {"ok": True, "data": api.status()})
                    return

                if parsed.path == "/api/v1/content":
                    from .content_items import list_materials
                    result = list_materials(api._core().repository,
                        link_messages=self._one(params, 'link_messages', '') == 'true',
                        category=self._one(params, 'category', ''), query=self._one(params, 'query', ''),
                        allowed_conversations=[self._one(params, 'conversation')] if self._one(params, 'conversation') else None,
                        offset=int(self._one(params, 'offset', '0')), limit=int(self._one(params, 'limit', '50')))
                    self._json(HTTPStatus.OK, {'ok': True, 'data': result})
                    return

                if parsed.path == '/api/v1/media/progress':
                    from .download_progress import snapshot
                    core = api._core(self._one(params, 'library_id'))
                    value = snapshot(core.project_root, core.account, self._one(params, 'request_id'))
                    value.pop('updated', None)
                    self._json(HTTPStatus.OK, {'ok': True, 'data': value})
                    return

                if parsed.path == '/api/v1/conversation-media-range':
                    result = api.conversation_media_range(self._one(params, 'library_id'), self._one(params, 'conversation'))
                    self._json(HTTPStatus.OK, {'ok': True, 'data': result})
                    return

                if parsed.path == "/api/v1/settings":
                    self._json(HTTPStatus.OK, {"ok": True, "data": api.settings()})
                    return


                if parsed.path == "/api/v1/conversations":
                    rows = api.conversations(
                        limit=int(self._one(params, "limit", "100") or 100),
                        offset=int(self._one(params, "offset", "0") or 0),
                        query=self._one(params, "q"),
                        selection_state=self._one(params, "state"),
                    )
                    self._json(HTTPStatus.OK, {"ok": True, "data": rows})
                    return

                if parsed.path == "/api/v1/messages":
                    username = self._one(params, "conversation")
                    if not username:
                        self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "conversation_required"})
                        return
                    before_raw = self._one(params, "before")
                    from_raw = self._one(params, 'from_day')
                    to_raw = self._one(params, 'to_day')
                    payload = api.messages(
                        username,
                        limit=int(self._one(params, "limit", "80") or 80),
                        before=int(before_raw) if before_raw else None,
                        before_uid=self._one(params, "before_uid"),
                        around_uid=self._one(params, "around_uid"),
                        after_uid=self._one(params, "after_uid"),
                        at_time=int(self._one(params, 'at_time')) if self._one(params, 'at_time') else None,
                        category=self._one(params, 'category'),
                        query=self._one(params, 'q'),
                        from_day=int(from_raw) if from_raw else None,
                        to_day=int(to_raw) if to_raw else None,
                        sender=self._one(params, 'sender'),
                        filter_offset=int(self._one(params, 'filter_offset', '0') or 0),
                    )
                    self._json(HTTPStatus.OK, {"ok": True, "data": payload})
                    return

                if parsed.path == '/api/v1/conversations/members':
                    username = self._one(params, 'conversation')
                    if not username:
                        self._json(HTTPStatus.BAD_REQUEST, {
                            'ok': False, 'error': 'conversation_required',
                        })
                        return
                    self._json(HTTPStatus.OK, {
                        'ok': True, 'data': api.group_members(username),
                    })
                    return

                if parsed.path == "/api/v1/contacts":
                    rows = api.contacts(
                        limit=int(self._one(params, "limit", "200") or 200),
                        offset=int(self._one(params, "offset", "0") or 0),
                        query=self._one(params, "q"),
                    )
                    self._json(HTTPStatus.OK, {"ok": True, "data": rows})
                    return

                if parsed.path == "/api/v1/search":
                    query = self._one(params, "q", "") or ""
                    payload = api.search(
                        query,
                        limit=int(self._one(params, "limit", "50") or 50),
                    )
                    self._json(HTTPStatus.OK, {"ok": True, "data": payload})
                    return




                if parsed.path == "/api/v1/selections":
                    self._json(
                        HTTPStatus.OK,
                        {"ok": True, "data": api.selections()},
                    )
                    return

                if parsed.path == "/api/v1/libraries":
                    self._json(
                        HTTPStatus.OK,
                        {"ok": True, "data": api.libraries()},
                    )
                    return

                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            except KeyError as exc:
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
            except (ValueError, TypeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_request", "message": str(exc)})
            except Exception:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "internal_error"})

        def _post(self) -> None:
            parsed = urlparse(self.path)
            if not self._require_auth():
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > MAX_BODY:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "body_too_large"})
                return
            body = b""
            if length:
                body = self.rfile.read(length)
            payload_json: dict = {}
            if body:
                try:
                    parsed_body = json.loads(body.decode("utf-8"))
                    if isinstance(parsed_body, dict):
                        payload_json = parsed_body
                except (UnicodeDecodeError, ValueError):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "invalid_json"},
                    )
                    return



            if parsed.path in {"/api/v1/mac/wechat", "/api/v1/mac/auto-capture"}:
                from .workspace_access import validate_capture_paths
                try:
                    validate_capture_paths(payload_json, workspace_access.root, public=True)
                except PermissionError:
                    self._json(HTTPStatus.FORBIDDEN, {'ok': False, 'error': 'workspace_paths_managed'})
                    return
            if parsed.path == "/api/v1/mac/wechat":
                from .mac_entry import route_action

                self._json(HTTPStatus.OK, route_action(payload_json))
                return
            if parsed.path == "/api/v1/mac/auto-capture":
                from .mac_entry import route_auto_capture_start

                started = route_auto_capture_start(payload_json)
                if started.get("error") in {"windows_key_path_unchanged", "darwin_required", "databases_not_found"} and "started" not in started:
                    self._json(HTTPStatus.OK, started)
                else:
                    self._json(HTTPStatus.OK, {"ok": True, "data": started})
                return

            if parsed.path in ('/api/v1/mac/accounts', '/api/v1/mac/accounts/connect', '/api/v1/mac/accounts/cancel') and sys.platform == 'darwin':
                try:
                    if parsed.path.endswith('/cancel'):
                        self._json(HTTPStatus.OK, {'ok': True, 'data': api.mac_accounts.cancel()})
                        return
                    data = (api.mac_accounts.connect(payload_json.get('library_id'), single=True)
                            if parsed.path.endswith('/connect') else api.mac_accounts.configure(payload_json.get('follow_wechat')))
                    self._json(HTTPStatus.OK, {'ok': True, 'data': data})
                except (ValueError, KeyError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {'ok': False, 'error': 'account_request_rejected', 'message': str(exc)})
                except Exception:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {'ok': False, 'error': 'account_request_failed'})
                return

            if parsed.path == '/api/v1/media/recheck':
                try:
                    library = payload_json.get('library_id')
                    uid = payload_json.get('uid')
                    if not isinstance(library, str) or not library or not isinstance(uid, str) or not uid or len(uid) > 512:
                        raise ValueError('library_and_message_required')
                    core = api._core(library)
                    request_id = payload_json.get('request_id')
                    if request_id:
                        from .download_progress import track
                        with track(core.project_root, core.account, request_id):
                            result = core.recheck_media(uid)
                    else:
                        result = core.recheck_media(uid)
                    result['library_id'] = library
                    self._json(HTTPStatus.OK, {'ok': True, 'data': result})
                except (ValueError, KeyError, TypeError):
                    self._json(HTTPStatus.BAD_REQUEST, {'ok': False, 'error': 'media_recheck_rejected'})
                except (OSError, sqlite3.Error):
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {'ok': False, 'error': 'media_read_failed'})
                return

            if parsed.path == '/api/v1/conversation-media-range':
                try:
                    result = api.conversation_media_range(payload_json.get('library_id'), payload_json.get('conversation'), update=payload_json)
                    self._json(HTTPStatus.OK, {'ok': True, 'data': result})
                except (ValueError, KeyError, TypeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {'ok': False, 'error': 'invalid_media_range', 'message': str(exc)})
                return

            if parsed.path == '/api/v1/media-window':
                try:
                    result = api.update_media_window(payload_json.get('days'))
                    self._json(HTTPStatus.OK, {'ok': True, 'data': result})
                except ValueError as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {
                        'ok': False, 'error': 'invalid_media_window', 'message': str(exc),
                    })
                return

            if parsed.path == "/api/v1/libraries/candidates":
                try:
                    root = str(payload_json.get("wechat_root") or "").strip() or None
                    self._json(
                        HTTPStatus.OK,
                        {"ok": True, "data": api.discover_candidates(root)},
                    )
                except Exception:
                    self._json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"ok": False, "error": "candidate_discovery_failed"},
                    )
                return

            if parsed.path == "/api/v1/libraries/register":
                try:
                    result = api.register_candidate(str(payload_json.get("library_id") or ""))
                    self._json(HTTPStatus.OK, {"ok": True, "data": result})
                except (KeyError, ValueError) as exc:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "invalid_candidate", "message": str(exc)},
                    )
                return

            if parsed.path == "/api/v1/settings/auto-media-loading":
                try:
                    result = api.update_auto_media_loading(payload_json.get('enabled'))
                    self._json(HTTPStatus.OK, {'ok': True, 'data': result})
                except ValueError as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {'ok': False, 'error': 'invalid_media_loading', 'message': str(exc)})
                return

            if parsed.path == "/api/v1/settings/auto-media-limit":
                try:
                    result = api.update_auto_media_limit(payload_json.get('max_mb'))
                    self._json(HTTPStatus.OK, {'ok': True, 'data': result})
                except ValueError:
                    self._json(HTTPStatus.BAD_REQUEST, {'ok': False, 'error': 'invalid_auto_media_limit'})
                except OSError:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {'ok': False, 'error': 'settings_save_failed'})
                return

            if parsed.path == "/api/v1/settings/parse-stickers":
                try:
                    result = api.update_sticker_parsing(payload_json.get('enabled'))
                    self._json(HTTPStatus.OK, {'ok': True, 'data': result})
                except ValueError:
                    self._json(HTTPStatus.BAD_REQUEST, {'ok': False, 'error': 'invalid_sticker_parsing'})
                except OSError:
                    self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {'ok': False, 'error': 'settings_save_failed'})
                return

            if parsed.path == "/api/v1/settings/sync-interval":
                try:
                    if 'seconds' in payload_json:
                        result = api.update_sync_interval_seconds(payload_json.get('seconds'))
                    else:
                        result = api.update_sync_interval(payload_json.get('minutes'))
                    self._json(HTTPStatus.OK, {'ok': True, 'data': result})
                except ValueError as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {'ok': False, 'error': 'invalid_sync_interval', 'message': str(exc)})
                return

            if parsed.path == "/api/v1/mcp/refresh":
                try:
                    if sync_requests is None:
                        raise ValueError('sync_service_unavailable')
                    self._json(HTTPStatus.OK, sync_requests.submit(payload_json))
                except PermissionError:
                    self._json(HTTPStatus.FORBIDDEN, {'accepted': False, 'error': 'sync_not_authorized'})
                except (ValueError, KeyError):
                    self._json(HTTPStatus.BAD_REQUEST, {'accepted': False, 'error': 'invalid_sync_request'})
                return

            if parsed.path == "/api/v1/refresh":
                try:
                    payload = api.refresh()
                except Exception:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {'ok': False, 'error': 'sync_failed'})
                    return
                status = HTTPStatus.OK if payload.get("ok") else HTTPStatus.CONFLICT
                self._json(status, payload)
                return

            if parsed.path == "/api/v1/libraries/discover":
                root = str(payload_json.get("wechat_root") or "").strip() or None
                try:
                    result = api.discover_libraries(root)
                    self._json(HTTPStatus.OK, {"ok": True, "data": result})
                except Exception as exc:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "ok": False,
                            "error": "library_discovery_failed",
                            "message": exc.__class__.__name__,
                        },
                    )
                return

            if parsed.path == "/api/v1/libraries/switch":
                library_id = str(payload_json.get("library_id") or "").strip()
                if not library_id:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "library_id_required"},
                    )
                    return
                try:
                    result = api.switch_library(
                        library_id,
                        initialize=bool(payload_json.get("initialize", True)),
                    )
                    self._json(HTTPStatus.OK, {"ok": True, "data": result})
                except KeyError:
                    self._json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": "library_not_found"},
                    )
                return

            if parsed.path == "/api/v1/libraries/rename":
                library_id = str(payload_json.get("library_id") or "").strip()
                display_name = str(payload_json.get("display_name") or "").strip()
                if not library_id or not display_name:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "library_id_and_display_name_required"},
                    )
                    return
                try:
                    result = api.rename_library(library_id, display_name)
                    self._json(HTTPStatus.OK, {"ok": True, "data": result})
                except KeyError:
                    self._json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": "library_not_found"},
                    )
                return

            if parsed.path == "/api/v1/conversations/follow":
                username = str(payload_json.get("username") or "").strip()
                if not username:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "username_required"},
                    )
                    return
                try:
                    result = api.follow(username)
                    self._json(HTTPStatus.OK, {"ok": True, "data": result})
                except KeyError:
                    self._json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": "conversation_not_found"},
                    )
                except Exception as exc:
                    self._json(
                        HTTPStatus.CONFLICT,
                        {
                            "ok": False,
                            "error": "selective_decrypt_failed",
                            "message": exc.__class__.__name__,
                        },
                    )
                return

            if parsed.path == "/api/v1/conversations/pause":
                username = str(payload_json.get("username") or "").strip()
                if not username:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "username_required"},
                    )
                    return
                try:
                    result = api.pause(username)
                    self._json(HTTPStatus.OK, {"ok": True, "data": result})
                except KeyError:
                    self._json(
                        HTTPStatus.NOT_FOUND,
                        {"ok": False, "error": "conversation_not_found"},
                    )
                return

            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    return Handler


def serve(config_path: Path, port: int, token_file: Path) -> None:
    root = Path(load_root_config(config_path.resolve())['project_root'])
    with RuntimeLock(root):
        _serve(config_path, port, token_file)


def _serve(config_path: Path, port: int, token_file: Path) -> None:
    token = load_or_create_token(token_file)
    api = MultiLibraryApi(config_path)
    if sys.platform == "darwin":
        try:
            from .mac_entry import arm_macos_key_check

            arm_macos_key_check()
        except Exception:
            pass  # Startup key check is advisory; the service still comes up.
    api.start_periodic_refresh()
    api.start_automatic_media()
    server = ThreadingHTTPServer(("127.0.0.1", int(port)), make_handler(api, token))
    server.daemon_threads = True
    actual_port = int(server.server_address[1])
    bridge = Path(api.root_config['project_root']) / 'data/runtime/mcp-service.json'
    bridge.parent.mkdir(parents=True, exist_ok=True)
    bridge_temp = bridge.with_suffix('.tmp')
    bridge_temp.write_text(json.dumps({'port': actual_port, 'token_file': str(token_file.resolve())}))
    bridge_temp.chmod(0o600)
    bridge_temp.replace(bridge)
    print(
        json.dumps(
            {
                "ready": True,
                "host": "127.0.0.1",
                "port": actual_port,
                "token_file": str(token_file.resolve()),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        api.stop_automatic_media()
        api.stop_periodic_refresh()
        server.server_close()
        bridge.unlink(missing_ok=True)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in ('--mcp-skill', '--mcp-auto'):
        from .skill_connection import run
        return run(sys.argv[2:], serve=sys.argv[1] == '--mcp-auto')
    if len(sys.argv) > 1 and sys.argv[1] == "--mcp-remote":
        from .remote_mcp import run_remote
        return run_remote(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "--mcp":
        from .local_pairing import run_local_mcp
        return run_local_mcp(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "--mcp-local-admin":
        from .local_pairing import run_admin
        return run_admin(sys.argv[2:])
    parser = argparse.ArgumentParser(description="Noteford loopback API service")
    parser.add_argument("--config", type=Path, default=Path("config.local.json"))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--token-file", type=Path, default=Path("secrets/api_token.json"))
    args = parser.parse_args()
    serve(args.config.resolve(), args.port, args.token_file.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
