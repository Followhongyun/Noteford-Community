"""Account-local, read-only red-envelope terminal states. No payment requests."""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from .crypto import decrypt_database, merge_encrypted_wal, quick_check
from .refresh import _match_or_refresh_key
from .snapshot import copy_database_snapshot


def terminal_state(hb_status: int, receive_status: int, hb_type: int) -> str | None:
    # Known ordinary/personal/group/exclusive envelope types only. Unknown codes
    # must not turn into an assertion that the owner cannot claim this packet.
    if hb_type not in (0, 1, 3):
        return None
    if receive_status == 2:
        return 'received'
    if hb_status == 4:
        return 'exhausted'
    if hb_status == 5:
        return 'expired'
    return None


def read_states(database: Path) -> dict[tuple[str, str], str]:
    con = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    try:
        result = {}
        duplicates = set()
        for session, sid, hb, received, kind in con.execute(
            'SELECT session_name,message_server_id,hb_status,receive_status,hb_type FROM redEnvelopeTable'
        ):
            key = (str(session), str(sid))
            state = terminal_state(hb, received, kind)
            if key in result:
                duplicates.add(key)
            result[key] = state
        return {key: value for key, value in result.items() if value and key not in duplicates and key[1] not in ('0', 'None')}
    finally:
        con.close()


class PaymentStateReader:
    def __init__(self, source: Path, runtime: Path, candidates: Path):
        self.source = source
        self.runtime = runtime.resolve()
        self.candidates = candidates
        self._lock = threading.Lock()
        self._checked = float('-inf')
        self._fingerprint = None
        self._states: dict[tuple[str, str], str] = {}

    def states(self) -> dict[tuple[str, str], str]:
        with self._lock:
            if time.monotonic() - self._checked < 10:
                return self._states.copy()
            self._checked = time.monotonic()
            try:
                paths = [self.source, Path(str(self.source) + '-wal')]
                fingerprint = tuple((p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else None for p in paths)
                if fingerprint[0] is None:
                    raise FileNotFoundError()
                if fingerprint == self._fingerprint:
                    return self._states.copy()
                if sum(item[0] for item in fingerprint if item) > 128 * 1024 * 1024:
                    raise ValueError('payment_snapshot_size_limit')
                self.runtime.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix='payment-state-', dir=self.runtime) as folder:
                    work = Path(folder).resolve()
                    if not work.is_relative_to(self.runtime):
                        raise ValueError('payment_snapshot_path')
                    snapshot = copy_database_snapshot(self.source, work / 'encrypted', retries=3)
                    encrypted = snapshot / self.source.name
                    key, _ = _match_or_refresh_key(encrypted, self.candidates, allow_memory_rescan=False)
                    database = work / 'payment.db'
                    decrypt_database(encrypted, database, key)
                    merge_encrypted_wal(database, Path(str(encrypted) + '-wal'), key)
                    if not quick_check(database):
                        raise ValueError('payment_snapshot_invalid')
                    states = read_states(database)
                self._states = states
                self._fingerprint = fingerprint
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                # Missing keys, failed snapshots and unknown schemas stay unknown.
                self._states = {}
                self._fingerprint = None
            return self._states.copy()
