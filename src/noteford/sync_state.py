"""Persistent sync receipts containing no source paths or message content."""
import json
import math
import threading
from datetime import datetime, timezone
from pathlib import Path


_STATES = {'idle', 'running', 'ready', 'partial', 'failed', 'interrupted'}
_STAGES = {'catalog', 'avatar_sync', 'media_prefetch', 'cleanup', 'complete'}


def _iso(value):
    if not isinstance(value, str):
        return None
    try:
        datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return value


def read_public_sync_state(path: Path) -> dict:
    """Read a persisted sync receipt without mutating it or exposing free text."""
    try:
        raw = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return {'state': 'idle', 'recent_receipts': []}
    if not isinstance(raw, dict):
        return {'state': 'idle', 'recent_receipts': []}

    public = {}
    if raw.get('state') in _STATES:
        public['state'] = raw['state']
    for key in (
        'started_at', 'finished_at', 'last_success_at', 'stage_started_at',
        'deadline_at', 'deadline_exceeded_at', 'archive_success_at',
    ):
        if (value := _iso(raw.get(key))) is not None:
            public[key] = value
    if 'error' in raw and raw.get('error') in SyncState.ERRORS:
        public['error'] = raw.get('error')
    if raw.get('stage') in _STAGES:
        public['stage'] = raw['stage']
    if raw.get('deferred_stage') in _STAGES:
        public['deferred_stage'] = raw['deferred_stage']
    failed = raw.get('failed_conversations')
    if type(failed) is int and failed >= 0:
        public['failed_conversations'] = failed

    receipts = []
    raw_receipts = raw.get('recent_receipts')
    if not isinstance(raw_receipts, list):
        raw_receipts = []
    for item in raw_receipts[-SyncState.RECEIPT_LIMIT:]:
        if not isinstance(item, dict):
            continue
        started = _iso(item.get('started_at'))
        finished = _iso(item.get('finished_at'))
        duration = item.get('duration_seconds')
        count = item.get('failed_conversations')
        if (
            started is None or finished is None
            or type(duration) not in (int, float)
            or not math.isfinite(duration)
        ):
            continue
        receipts.append({
            'trigger': item.get('trigger') if item.get('trigger') in SyncState.TRIGGERS else 'unknown',
            'started_at': started,
            'finished_at': finished,
            'duration_seconds': max(0, round(float(duration), 3)),
            'outcome': item.get('outcome') if item.get('outcome') in SyncState.OUTCOMES else 'failed',
            'error': item.get('error') if item.get('error') in SyncState.ERRORS else 'sync_failed',
            'failed_conversations': count if type(count) is int and count >= 0 else 0,
        })
    public['recent_receipts'] = receipts
    public.setdefault('state', 'idle')
    return public


class SyncState:
    RECEIPT_LIMIT = 20
    TRIGGERS = {'startup', 'interval', 'resume', 'login_return', 'source_change', 'manual', 'retry', 'ai_request'}
    OUTCOMES = {'success', 'partial', 'failed', 'interrupted'}
    ERRORS = {
        None, 'interrupted', 'key_unavailable', 'source_unavailable', 'source_permission_required',
        'sync_failed', 'selected_refresh_partial', 'deadline_reached',
    }

    def __init__(self, root: Path):
        self.path = root / 'data' / 'state' / 'sync.json'
        self.lock = threading.RLock()
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            self.value = {key: value.get(key) for key in (
                'state', 'started_at', 'finished_at', 'last_success_at', 'error',
                'stage', 'stage_started_at', 'deadline_at',
                'deadline_exceeded_at', 'deferred_stage',
                'failed_conversations', 'archive_success_at')}
            receipts = value.get('recent_receipts')
            self.value['recent_receipts'] = receipts[-self.RECEIPT_LIMIT:] if isinstance(receipts, list) else []
        except (OSError, ValueError, AttributeError):
            self.value = {'state': 'idle', 'recent_receipts': []}
        if self.value.get('state') == 'running':
            self.update(state='interrupted', error='interrupted')

    def read(self):
        with self.lock:
            return dict(self.value)

    def update(self, **values):
        with self.lock:
            value = {**self.value, **values}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            with temporary.open('w', encoding='utf-8') as output:
                json.dump(value, output)
                output.flush()
            temporary.replace(self.path)
            self.value = value

    def record_receipt(
        self, *, trigger, started_at, finished_at, duration_seconds,
        outcome, error=None, failed_conversations=0,
    ):
        receipt = {
            'trigger': trigger if trigger in self.TRIGGERS else 'unknown',
            'started_at': str(started_at),
            'finished_at': str(finished_at),
            'duration_seconds': max(0, round(float(duration_seconds), 3)),
            'outcome': outcome if outcome in self.OUTCOMES else 'failed',
            'error': error if error in self.ERRORS else 'sync_failed',
            'failed_conversations': max(0, int(failed_conversations)),
        }
        with self.lock:
            receipts = [*self.value.get('recent_receipts', []), receipt]
            self.update(recent_receipts=receipts[-self.RECEIPT_LIMIT:])


def now_iso():
    return datetime.now(timezone.utc).isoformat()
