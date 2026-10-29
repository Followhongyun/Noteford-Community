"""Download counts for indexed media, scoped to one account and message revision."""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path


def empty_summary():
    return dict(total=0, downloaded=0, not_downloaded=0, expired=0)


def _has_original(status, path):
    # A retained original can still need decoding. Thumbnails are never originals.
    original = status in {'ready', 'ready_audio', 'encrypted_original', 'encoded_voice',
                          'wxgf_container', 'unsupported_image_container',
                          'unsupported_multiframe_image'} or str(status).startswith('image_decode')
    if not original or not path:
        return False
    file = Path(path)
    if status not in {'ready', 'ready_audio', 'encoded_voice', 'encrypted_original'}:
        if file.stem.endswith(('_thumb', '_t')):
            return False
    try:
        return file.is_file() and file.stat().st_size > 0
    except OSError:
        return False


def remember_media(index_path: Path, account: str, items):
    """Keep successful local reads visible in counts, including manual/older media.

    This records existing copies only; it never resolves or downloads a file and
    does not enroll a message in the automatic download queue.
    """
    originals = [(item['uid'], item['type']['kind'], item['media']) for item in items
                 if item.get('type', {}).get('kind') in {'image', 'video', 'voice', 'file'}
                 and _has_original((item.get('media') or {}).get('status'),
                                   (item.get('media') or {}).get('path'))]
    if not originals:
        return
    try:
        with closing(sqlite3.connect(index_path, timeout=1)) as con, con:
            con.execute('''CREATE TABLE IF NOT EXISTS media_exports (
                uid TEXT PRIMARY KEY, row_hash TEXT NOT NULL, kind TEXT NOT NULL,
                status TEXT NOT NULL, path TEXT, attempted_at TEXT NOT NULL)''')
            for uid, kind, media in originals:
                con.execute('''INSERT INTO media_exports(uid,row_hash,kind,status,path,attempted_at)
                    SELECT uid,row_hash,?,?,?,? FROM messages WHERE account=? AND uid=?
                    ON CONFLICT(uid) DO UPDATE SET row_hash=excluded.row_hash,
                        kind=excluded.kind,status=excluded.status,path=excluded.path,
                        attempted_at=excluded.attempted_at
                    WHERE media_exports.row_hash!=excluded.row_hash
                        OR media_exports.status!=excluded.status
                        OR media_exports.path IS NOT excluded.path''',
                    (kind, media['status'], media['path'], datetime.now(timezone.utc).isoformat(),
                     account, uid))
    except sqlite3.Error:
        # Bookkeeping must not prevent opening a successfully resolved attachment.
        pass


class MediaDownloadSummary:
    def __init__(self, index_path: Path, account: str, jobs_path: Path):
        self.index_path = index_path
        self.account = account
        self.jobs_path = jobs_path
        self._cached = None
        self._checked = float('-inf')
        self._lock = threading.Lock()

    def read(self):
        # Status is polled every two seconds. Do not stat every retained file on
        # each poll; recheck deletions within 5 seconds.
        with self._lock:
            if time.monotonic() - self._checked < 5:
                return self._cached.copy() if self._cached is not None else None
            try:
                result = self._read()
            except (OSError, sqlite3.Error):
                result = None  # Unknown is not zero and never evidence of expiry.
            self._checked = time.monotonic()
            self._cached = result
            return result.copy() if result is not None else None

    def _read(self):
        counts = empty_summary()
        with closing(sqlite3.connect(self.index_path.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            con.row_factory = sqlite3.Row
            has_exports = con.execute("SELECT 1 FROM sqlite_master WHERE name='media_exports'").fetchone()
            exports = 'media_exports' if has_exports else '(SELECT NULL uid,NULL row_hash,NULL status,NULL path)'
            jobs = '(SELECT NULL account,NULL uid,NULL row_hash,NULL status,NULL path)'
            if self.jobs_path.is_file():
                con.execute('ATTACH DATABASE ? AS downloads', (self.jobs_path.resolve().as_uri() + '?mode=ro',))
                if con.execute("SELECT 1 FROM downloads.sqlite_master WHERE name='jobs'").fetchone():
                    jobs = 'downloads.jobs'
            rows = con.execute(f'''SELECT m.uid,
                    e.status export_status,e.path export_path,j.status job_status,j.path job_path
                FROM messages m
                LEFT JOIN {exports} e ON e.uid=m.uid AND e.row_hash=m.row_hash
                LEFT JOIN {jobs} j ON j.account=m.account AND j.uid=m.uid AND j.row_hash=m.row_hash
                WHERE m.account=? AND ((m.local_type & 4294967295) IN (3,34,43)
                    OR m.local_type=?)''', (self.account, (6 << 32) | 49))
            # The index stores app subtype in the high 32 bits (6 = file).
            # Links, payment cards, stickers and record containers aren't files.
            for row in rows:
                counts['total'] += 1
                if (_has_original(row['export_status'], row['export_path'])
                        or _has_original(row['job_status'], row['job_path'])):
                    counts['downloaded'] += 1
                elif row['job_status'] == 'cdn_expired':
                    counts['expired'] += 1
                else:
                    counts['not_downloaded'] += 1
        return counts
