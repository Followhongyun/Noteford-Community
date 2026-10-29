from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .batch import BatchResult, export_daily_batch
from .media import latest_decrypted_media_databases
from .selective import refresh_catalog

# Compatibility name for older tests/callers. It now means selective catalog
# refresh, never the old all-message-databases refresh.
refresh_account = refresh_catalog


@dataclass
class DailyJobResult:
    skipped: bool
    reason: str | None
    local_date: str
    since: int | None
    until: int | None
    batch: dict | None
    job_id: str | None
    job_directory: str | None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding='utf-8'))


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def enqueue_batch_job(
    project_root: Path,
    account: str,
    local_date: str,
    batch: BatchResult,
) -> tuple[str, Path]:
    jobs_root = project_root / 'data' / 'jobs'
    staging = jobs_root / 'Staging'
    ready = jobs_root / 'Ready'
    staging.mkdir(parents=True, exist_ok=True)
    ready.mkdir(parents=True, exist_ok=True)

    conversations = Path(batch.conversations_file)
    manifest = Path(batch.manifest_file)
    conversations_hash = _sha256(conversations)
    manifest_hash = _sha256(manifest)
    source_id = hashlib.sha256(
        f'windows-noteford:{account}'.encode('utf-8')
    ).hexdigest()
    task_id = hashlib.sha256(
        f'{account}:{batch.since}:{batch.until}:{conversations_hash}'.encode('utf-8')
    ).hexdigest()

    base_job_id = f'wechat-daily-{local_date}'
    job_id = base_job_id
    destination = ready / job_id
    if destination.is_dir():
        existing = _read_json(destination / 'job.json')
        if existing.get('event', {}).get('taskId') == task_id:
            return job_id, destination
        # A forced same-day catch-up must never silently point at the older
        # Ready job. Publish a deterministic suffixed job for the new interval.
        job_id = f'{base_job_id}-{task_id[:12]}'
        destination = ready / job_id
        if destination.is_dir():
            existing = _read_json(destination / 'job.json')
            if existing.get('event', {}).get('taskId') == task_id:
                return job_id, destination
            raise RuntimeError('daily job id collision with different task identity')

    partial = staging / f'{job_id}.partial'
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)

    try:
        manifest_summary = json.loads(manifest.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        manifest_summary = {}

    payload = {
        'schema': 2,
        'job_id': job_id,
        'kind': 'wechat_daily_digest',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'account': account,
        'event': {
            'version': 1,
            'taskId': task_id,
            'taskType': 'wechat.daily.digest',
            'sourceId': source_id,
            'sourceVersion': conversations_hash,
            'rulesVersion': 'wechat-daily-v1',
            'deliveryId': job_id,
            'contentRef': {
                'kind': 'wechat.daily.batch',
                'taskId': task_id,
            },
        },
        'requested_executor': {
            'session_mode': 'new',
            'role_id': 'wechat-intelligence',
            'display_name': '微信情报员',
            'skill_id': 'wechat-daily-digest',
        },
        'batch': {
            'directory': batch.directory,
            'conversations_file': batch.conversations_file,
            'conversations_sha256': conversations_hash,
            'manifest_file': batch.manifest_file,
            'manifest_sha256': manifest_hash,
            'message_count': batch.message_count,
            'conversation_count': batch.conversation_count,
            'since': batch.since,
            'until': batch.until,
            'media_status_counts': manifest_summary.get('media_status_counts', {}),
            'unexpanded_media_messages': int(
                manifest_summary.get('unexpanded_media_messages', 0) or 0
            ),
        },
        'contract': {
            'input_is_read_only': True,
            'may_send_wechat_messages': False,
            'may_modify_wechat': False,
            'result_expected': 'daily_digest',
        },
        'botdock_compatibility': {
            'status': 'pending_task_type_support',
            'current_p5_task_type': 'nexus.article.translation',
            'required_task_type': 'wechat.daily.digest',
            'reader_job_must_not_be_treated_as_translation': True,
        },
    }
    _write_json_atomic(partial / 'job.json', payload)
    os.replace(partial, destination)
    return job_id, destination


def run_daily_job(
    *,
    project_root: Path,
    wechat_root: Path,
    account: str,
    candidates_path: Path,
    index_path: Path,
    overlap_local_ids: int = 200,
    allow_memory_rescan: bool = True,
    first_window_hours: int = 24,
    max_chars: int = 20_000,
    voice_decoder_path: Path | None = None,
    force: bool = False,
    now: int | None = None,
) -> DailyJobResult:
    project_root = project_root.resolve()
    now = int(now if now is not None else time.time())
    local_date = datetime.fromtimestamp(now).astimezone().date().isoformat()
    state_path = project_root / 'data' / 'state' / 'daily_job.json'
    state = _read_json(state_path)

    if not force and state.get('last_local_date') == local_date:
        return DailyJobResult(
            skipped=True,
            reason='already_queued_today',
            local_date=local_date,
            since=None,
            until=None,
            batch=None,
            job_id=state.get('last_job_id'),
            job_directory=state.get('last_job_directory'),
        )

    # Refresh is a precondition. A failed source read cannot advance the daily
    # cursor; the next run must retry the same interval.
    refresh_account(
        wechat_root=wechat_root,
        account=account,
        project_root=project_root,
        candidates_path=candidates_path,
        index_path=index_path,
        overlap_local_ids=overlap_local_ids,
        allow_memory_rescan=allow_memory_rescan,
        refresh_selected=True,
    )

    last_until = state.get('last_until')
    since = int(last_until) + 1 if last_until is not None else now - first_window_hours * 3600
    until = now
    batch = export_daily_batch(
        index_path=index_path,
        account=account,
        since=since,
        until=until,
        output_root=project_root / 'data' / 'batches',
        max_chars=max_chars,
        wechat_root=wechat_root,
        media_db_paths=latest_decrypted_media_databases(project_root, account),
        media_cache_root=project_root / 'data' / 'media-cache',
        image_keys_path=project_root / 'secrets' / 'image_keys.json',
        voice_decoder_path=voice_decoder_path,
        selected_only=True,
    )

    job_id = None
    job_directory = None
    if batch.message_count:
        job_id, job_path = enqueue_batch_job(
            project_root=project_root,
            account=account,
            local_date=local_date,
            batch=batch,
        )
        job_directory = str(job_path.resolve())

    # Advance only after batch creation and, when non-empty, atomic Ready
    # publication. This is the reader's durable handoff boundary.
    new_state = {
        'schema': 1,
        'last_local_date': local_date,
        'last_until': until,
        'last_success_at': datetime.now(timezone.utc).isoformat(),
        'last_job_id': job_id,
        'last_job_directory': job_directory,
        'last_message_count': batch.message_count,
    }
    _write_json_atomic(state_path, new_state)

    return DailyJobResult(
        skipped=False,
        reason='no_messages' if batch.message_count == 0 else None,
        local_date=local_date,
        since=since,
        until=until,
        batch=asdict(batch),
        job_id=job_id,
        job_directory=job_directory,
    )
