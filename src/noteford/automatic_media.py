"""Serial, restartable completion of followed image/video messages in current ranges."""
from __future__ import annotations

from contextlib import closing
import sqlite3
import threading
import time
from pathlib import Path

from .media_backfill import local_day_window
from .cdn_download import DownloadError

# A new message revision or explicit user action may try again; timers must not
# repeatedly request an unchanged resource rejected by the server.
BLOCKED = {'auto_size_limit', 'missing_download_reference', 'unsupported_download_format', 'too_large',
           'cdn_rejected', 'cdn_not_found', 'cdn_expired', 'integrity_failed', 'invalid_response', 'account_mismatch'}
MAX_ATTEMPTS = 3
REASONS = {
    'auto_size_limit': '超过自动下载大小设置，可手动下载',
    'local_original_unavailable': '本机尚未找到原文件',
    'missing_download_reference': '消息缺少下载信息',
    'unsupported_download_format': '暂不支持此媒体格式',
    'too_large': '文件超过下载大小限制',
    'cdn_rejected': '服务器拒绝下载请求',
    'cdn_not_found': '当前下载地址未返回文件',
    'cdn_expired': '云端文件已过期或被删除',
    'download_timeout': '下载超时',
    'integrity_failed': '文件完整性校验失败',
    'invalid_response': '服务器返回的数据无效',
    'account_mismatch': '下载凭据与当前账号不匹配',
    'session_unavailable': '未取得有效下载凭据',
    'network_unavailable': '网络连接失败',
    'insufficient_space': '磁盘空间不足',
    'storage_unavailable': '无法写入媒体缓存',
    'download_busy': '下载任务繁忙',
    'download_cancelled': '下载已中止',
    'media_unavailable': '尚未取得可用媒体文件',
}


def paused(status, attempts):
    return status in BLOCKED or (status not in {'ready', 'running', 'download_busy', 'download_cancelled'} and attempts >= MAX_ATTEMPTS)


class AutomaticMedia:
    def __init__(self, core):
        self.core = core
        self.path = core.project_root / 'data/automatic_media.sqlite'
        self._lock = threading.Lock()
        self.active = None
        self._policy_limit = None

    def _state(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.path, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute('''CREATE TABLE IF NOT EXISTS jobs (
            account TEXT NOT NULL, uid TEXT NOT NULL, username TEXT NOT NULL,
            timestamp INTEGER NOT NULL, row_hash TEXT NOT NULL, status TEXT NOT NULL,
            attempts INTEGER NOT NULL, retry_at REAL NOT NULL, path TEXT,
            bytes INTEGER, mtime INTEGER, PRIMARY KEY(account,uid))''')
        con.execute('''CREATE TABLE IF NOT EXISTS cooldown (
            account TEXT PRIMARY KEY, retry_at REAL NOT NULL, reason TEXT NOT NULL)''')
        con.execute('CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY)')
        if not con.execute("SELECT 1 FROM migrations WHERE name='unlimited-download-size'").fetchone():
            with con:
                con.execute("UPDATE jobs SET status='media_unavailable',attempts=0,retry_at=0 WHERE status='too_large'")
                con.execute("INSERT INTO migrations VALUES ('unlimited-download-size')")
        con.commit()
        return con

    def _rows(self, uid=None):
        # A fresh snapshot of settings and follow state is used for every file/chunk.
        core = self.core
        ranges = core._media_ranges()
        since, until = ranges.envelope() if ranges else local_day_window(
            int(core.config.get('media_prefetch_days') or 14))
        with closing(sqlite3.connect(Path(core.config['index_path']).resolve().as_uri()+'?mode=ro', uri=True)) as con:
            con.row_factory = sqlite3.Row
            query = '''SELECT m.uid,m.row_hash,m.create_time,c.username
                FROM messages m
                JOIN conversations c ON c.account=m.account AND c.chat_table=m.chat_table
                JOIN conversation_selection s ON s.account=c.account AND s.username=c.username
                LEFT JOIN message_media_policy p ON p.uid=m.uid
                WHERE COALESCE(p.automatic,1)=1 AND m.account=? AND s.state='following' AND m.local_type IN (3,43)
                AND (COALESCE(p.range_exempt,0)=1 OR (m.create_time>=? AND m.create_time<?'''
            params = [core.account, since, until]
            if ranges:
                con.create_function('in_media_range', 2, ranges.contains)
                query += ' AND in_media_range(c.username,m.create_time)'
            query += '))'
            if uid is not None:
                query += ' AND m.uid=?'
                params.append(uid)
            # Old failures back off, allowing later messages and conversations through.
            query += ' ORDER BY m.create_time,m.uid'
            yield from con.execute(query, params)

    def eligible(self, uid, row_hash):
        rows = self._rows(uid)
        try:
            row = next(rows, None)
            return row is not None and row['row_hash'] == row_hash
        finally:
            rows.close()

    def _candidate(self, now):
        con = self._state()
        rows = self._rows()
        try:
            limit = self.core.config.get('auto_media_max_mb', 0)
            if limit != self._policy_limit:
                with con:
                    con.execute("UPDATE jobs SET status='media_unavailable',attempts=0,retry_at=0 WHERE account=? AND status='auto_size_limit'", (self.core.account,))
                self._policy_limit = limit
            cooldown = con.execute('SELECT retry_at,reason FROM cooldown WHERE account=?', (self.core.account,)).fetchone()
            if cooldown and cooldown['retry_at'] > now:
                if cooldown['reason'] == 'session_unavailable' and cooldown['retry_at'] > now + 300:
                    with con:
                        con.execute('UPDATE cooldown SET retry_at=? WHERE account=?', (now + 300, self.core.account))
                        con.execute("UPDATE jobs SET retry_at=? WHERE account=? AND status='session_unavailable' AND retry_at>?", (now + 300, self.core.account, now + 300))
                return None
            for row in rows:
                # The primary key makes this a bounded lookup. Do not allocate
                # the account's entire download history for every next file.
                previous = con.execute('SELECT * FROM jobs WHERE account=? AND uid=?',
                                       (self.core.account, row['uid'])).fetchone()
                if previous and previous['row_hash'] == row['row_hash']:
                    if paused(previous['status'], previous['attempts']):
                        continue
                    if previous['status'] == 'ready' and previous['path']:
                        try:
                            stat = Path(previous['path']).stat()
                            if stat.st_size == previous['bytes'] and stat.st_mtime_ns == previous['mtime']:
                                continue
                        except OSError:
                            pass
                    elif previous['retry_at'] > now:
                        continue
                return dict(row), dict(previous) if previous else None
        finally:
            rows.close()
            con.close()
        return None

    def _record(self, row, status, attempts, retry_at, path=None):
        size = mtime = None
        if path:
            try:
                stat = Path(path).stat()
                size, mtime = stat.st_size, stat.st_mtime_ns
            except OSError:
                path = None
        con = self._state()
        try:
            with con:
                con.execute('''INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                    (self.core.account,row['uid'],row['username'],row['create_time'],row['row_hash'],
                     status,attempts,retry_at,path,size,mtime))
        finally:
            con.close()

    def run_one(self, stop: threading.Event, *, now=None):
        """At most one file, with no index transaction or refresh lock held during I/O."""
        if stop.is_set() or not self._lock.acquire(blocking=False):
            return False
        try:
            fixed_clock = now is not None
            now = time.time() if now is None else now
            candidate = self._candidate(now)
            if candidate is None:
                return False
            row, previous = candidate
            attempts = previous['attempts'] if previous and previous['row_hash'] == row['row_hash'] else 0
            def allowed():
                # Keep the current transfer resumable across a refresh: wait at
                # chunk boundaries instead of cancelling and downloading again.
                from .media_worker import refresh_active
                while refresh_active(self.core):
                    if stop.wait(.05):
                        return False
                return not stop.is_set() and self.eligible(row['uid'], row['row_hash'])
            if not allowed():
                return False
            self.active = row['username']
            self._record(row, 'running', attempts, now+30)
            try:
                from .download_policy import automatic
                with automatic(self.core.config):
                    result = self.core.recheck_media(row['uid'], should_continue=allowed)
                media = result['messages'][0]['media']
                error = media.get('download_error')
                if media['status'] == 'ready' and media.get('path') and not error:
                    self._record(row, 'ready', 0, 0, media['path'])
                    return True
                error = error or 'media_unavailable'
            except DownloadError as exc:
                error = str(exc) if str(exc) in REASONS else 'media_unavailable'
            except Exception:
                # No raw transport, config, message or credential details in job records.
                error = 'media_unavailable'
            attempts += 1
            if error in {'download_busy','download_cancelled'}:
                delay = 30
                attempts = max(0, attempts-1)
            elif error == 'session_unavailable':
                delay = 300
            elif error in {'missing_download_reference','unsupported_download_format','too_large'}:
                delay = 86400
            else:
                delay = min(21600, 60 * 2**min(attempts, 9))
            finished = now if fixed_clock else time.time()
            self._record(row, error, attempts, finished+delay)
            if error in {'account_mismatch','session_unavailable','network_unavailable',
                         'insufficient_space','storage_unavailable'}:
                con = self._state()
                try:
                    with con:
                        con.execute('INSERT OR REPLACE INTO cooldown VALUES(?,?,?)',
                                    (self.core.account, finished+max(300,delay), error))
                finally:
                    con.close()
            return True
        finally:
            self.active = None
            self._lock.release()

    def summary(self, username):
        ranges = self.core._media_ranges()
        since, until = ranges.bounds(username) if ranges else local_day_window(
            int(self.core.config.get('media_prefetch_days') or 14))
        if not self.path.exists():
            return {'running':False, 'completed':0, 'waiting':0, 'failures':[]}
        con = self._state()
        try:
            rows = con.execute('''SELECT uid,timestamp,status,attempts FROM jobs WHERE account=?
                AND username=? AND timestamp>=? AND timestamp<? ORDER BY timestamp DESC,uid''',
                (self.core.account,username,since,until)).fetchall()
            failures = [dict(uid=r['uid'], timestamp=r['timestamp'], reason=REASONS.get(r['status'], '媒体加载失败'),
                             code=r['status'], attempts=r['attempts'], paused=paused(r['status'],r['attempts']))
                        for r in rows if r['status'] not in {'ready','running'}]
            return {'running':self.active == username, 'completed':sum(r['status']=='ready' for r in rows),
                    'waiting':len(failures), 'failures':failures}
        finally:
            con.close()

    def record_manual_result(self, uid, media):
        # Manual loading must clear an old failure without enrolling manual-only media.
        con = self._state()
        try:
            old = con.execute('SELECT * FROM jobs WHERE account=? AND uid=?', (self.core.account,uid)).fetchone()
        finally:
            con.close()
        if old is None:
            # A confirmed manual expiry must survive reloads even when this
            # message opted out of automatic loading. It remains terminal.
            if media.get('download_error') == 'cdn_expired':
                with closing(sqlite3.connect(Path(self.core.config['index_path']).resolve().as_uri()+'?mode=ro', uri=True)) as source:
                    source.row_factory = sqlite3.Row
                    row = source.execute('''SELECT m.uid,m.row_hash,m.create_time,c.username
                        FROM messages m JOIN conversations c
                        ON c.account=m.account AND c.chat_table=m.chat_table
                        WHERE m.account=? AND m.uid=? AND (m.local_type & 4294967295) IN (3,43,49)''',
                        (self.core.account, uid)).fetchone()
                    if row is not None:
                        self._record(dict(row), 'cdn_expired', 1, time.time()+300)
            return
        row = dict(old)
        row['create_time'] = row['timestamp']
        ready = media.get('status') == 'ready' and media.get('path') and not media.get('download_error')
        self._record(row, 'ready' if ready else media.get('download_error', 'media_unavailable'),
                     0 if ready else old['attempts']+1, time.time()+300,
                     media.get('path') if ready else None)

    def expired_ids(self, uids):
        """Expose only confirmed failures for the current message revision/account."""
        if not uids or not self.path.is_file():
            return set()
        found = set()
        with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True)) as con:
            con.execute('ATTACH DATABASE ? AS source',
                        (Path(self.core.config['index_path']).resolve().as_uri()+'?mode=ro',))
            ids = list(set(uids))
            for start in range(0, len(ids), 400):
                batch = ids[start:start+400]
                rows = con.execute('''SELECT j.uid FROM jobs j JOIN source.messages m
                    ON j.account=m.account AND j.uid=m.uid AND j.row_hash=m.row_hash
                    WHERE j.account=? AND j.status='cdn_expired' AND j.uid IN ('''
                    + ','.join('?' for _ in batch)+')', [self.core.account, *batch])
                found.update(row[0] for row in rows)
        return found
