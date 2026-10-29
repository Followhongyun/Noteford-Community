"""Mac special-message metadata from Noteford's read-only SQLite snapshots."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .assets import latest_decrypted_database
from .payment_states import read_states
from .solitaire import native_memberships


class MacPaymentStateReader:
    def __init__(self, project: Path, account: str):
        self.project, self.account = project, account
        self._lock = threading.Lock()
        self._fingerprint = None
        self._states = {}

    def states(self) -> dict[tuple[str, str], str]:
        with self._lock:
            try:
                source = latest_decrypted_database(self.project, self.account, 'general', 'general')
                if source is None:
                    raise FileNotFoundError()
                stat = source.stat()
                fingerprint = (str(source), stat.st_size, stat.st_mtime_ns)
                if fingerprint != self._fingerprint:
                    # Reuse Windows' verified terminal-state semantics, without
                    # attempting to decrypt an already-plaintext Mac archive.
                    self._states = read_states(source)
                    self._fingerprint = fingerprint
                return self._states.copy()
            except (OSError, ValueError, sqlite3.Error):
                self._states, self._fingerprint = {}, None
                return {}


def refresh_solitaire(database: Path | None, index: Path, account: str) -> dict:
    from .refresh import _write_json_atomic
    links = {}
    status = 'unavailable'
    if database is not None:
        try:
            links = native_memberships(database, index, account)
            status = 'ready'
        except (OSError, ValueError, TypeError, sqlite3.Error):
            pass
    # Failures clear stale membership; equal prose is never used as an identity.
    _write_json_atomic(index.with_name('solitaire-memberships.json'), {'account': account, 'links': links})
    return {'status': status, 'linked': len(links)}
