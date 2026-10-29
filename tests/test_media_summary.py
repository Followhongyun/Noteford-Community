import sqlite3
from contextlib import closing

from noteford.indexer import _connect_index
from noteford.metadata import ensure_metadata_schema
from noteford.media_summary import MediaDownloadSummary, remember_media


def seed(tmp_path):
    index = tmp_path / 'index.sqlite'
    with closing(_connect_index(index)) as con, con:
        ensure_metadata_schema(con)
        for account in ['mine', 'other']:
            con.execute('''INSERT INTO conversations(account,chat_table,username,display_name,
                is_group,session_type,last_timestamp,sort_timestamp) VALUES(?,?,?,?,0,0,0,0)''',
                (account, 'Msg_x', account, account))
        con.execute('''CREATE TABLE media_exports(uid TEXT PRIMARY KEY,row_hash TEXT,
            kind TEXT,status TEXT,path TEXT,attempted_at TEXT,failures INTEGER DEFAULT 0,retry_at REAL DEFAULT 0)''')
    jobs = tmp_path / 'jobs.sqlite'
    with closing(sqlite3.connect(jobs)) as con, con:
        con.execute('''CREATE TABLE jobs(account TEXT,uid TEXT,row_hash TEXT,status TEXT,path TEXT,
            PRIMARY KEY(account,uid))''')
    return index, jobs


def message(index, uid, kind=3, *, account='mine', content=None):
    with closing(sqlite3.connect(index)) as con, con:
        con.execute('''INSERT INTO messages(uid,account,source_db,chat_table,local_type,
            create_time,row_hash,indexed_at,message_content) VALUES(?,?,?,? ,?,1,'v1','now',?)''',
            (uid, account, 'source', 'Msg_x', kind, content))


def exported(index, uid, status, path=None, revision='v1'):
    with closing(sqlite3.connect(index)) as con, con:
        con.execute('INSERT OR REPLACE INTO media_exports(uid,row_hash,kind,status,path) VALUES(?,?,?,?,?)',
                    (uid, revision, 'image', status, str(path) if path else None))


def job(jobs, uid, status, path=None, *, account='mine', revision='v1'):
    with closing(sqlite3.connect(jobs)) as con, con:
        con.execute('INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?)',
                    (account, uid, revision, status, str(path) if path else None))


def test_all_indexed_media_partitioned_without_false_expiry(tmp_path):
    index, jobs = seed(tmp_path)
    original = tmp_path / 'original.jpg'
    original.write_bytes(b'original')
    ids = ['never-tried', 'thumbnail', 'encrypted-thumbnail', 'expired', 'network', 'space',
           'not-found', 'unsupported', 'ready-job', 'ready-export', 'encrypted-original',
           'encoded-voice', 'removed-copy', 'stale-export', 'stale-expiry', 'wrong-account',
           'recovered', 'processing', 'thumb-processing', 'missing-reference']
    for uid in ids:
        message(index, uid, 34 if uid == 'encoded-voice' else 3)
    message(index, 'old-unfollowed-video', 43)  # No selection or recent-day filter.
    message(index, 'file', (6 << 32) | 49, content='<msg><appmsg><type>6</type><title>a.pdf</title></appmsg></msg>')
    message(index, 'typed-file', (6 << 32) | 49)
    for uid, kind, content in [('text', 1, None), ('emoji', 47, None), ('link', 49, '<msg><appmsg><type>5</type></appmsg></msg>'),
                               ('record', (19 << 32) | 49, None)]:
        message(index, uid, kind, content=content)
    message(index, 'other-user', account='other')
    exported(index, 'other-user', 'ready', original)
    exported(index, 'thumbnail', 'ready_thumbnail', original)
    exported(index, 'encrypted-thumbnail', 'encrypted_thumbnail', original)
    exported(index, 'ready-export', 'ready', original)
    exported(index, 'file', 'ready', original)
    exported(index, 'encrypted-original', 'encrypted_original', original)
    exported(index, 'encoded-voice', 'encoded_voice', original)
    exported(index, 'removed-copy', 'ready', tmp_path / 'gone')
    exported(index, 'stale-export', 'ready', original, 'v0')
    exported(index, 'processing', 'image_decode_failed', original)
    thumb = tmp_path / 'image_thumb.wxgf'
    thumb.write_bytes(b'thumb')
    exported(index, 'thumb-processing', 'image_decode_failed', thumb)
    job(jobs, 'ready-job', 'ready', original)
    job(jobs, 'expired', 'cdn_expired')
    job(jobs, 'stale-expiry', 'cdn_expired', revision='v0')
    job(jobs, 'wrong-account', 'cdn_expired', account='other')
    for uid, reason in [('network', 'network_unavailable'), ('space', 'insufficient_space'),
                        ('not-found', 'cdn_not_found'), ('unsupported', 'unsupported_download_format'),
                        ('missing-reference', 'missing_download_reference')]:
        job(jobs, uid, reason)
    job(jobs, 'recovered', 'cdn_expired')
    exported(index, 'recovered', 'ready', original)
    result = MediaDownloadSummary(index, 'mine', jobs).read()
    assert result == dict(total=23, downloaded=7, not_downloaded=15, expired=1)
    assert result['total'] == sum(result[key] for key in ('downloaded', 'not_downloaded', 'expired'))


def test_local_reads_count_without_starting_downloads_and_recheck_deleted_copies(tmp_path, monkeypatch):
    index, jobs = seed(tmp_path)
    message(index, 'manual', 34)
    voice = tmp_path / 'voice.silk'
    voice.write_bytes(b'voice')
    item = dict(uid='manual', type={'kind': 'voice'}, media={'status': 'encoded_voice', 'path': str(voice)})
    remember_media(index, 'other', [item])
    assert MediaDownloadSummary(index, 'mine', jobs).read()['downloaded'] == 0
    remember_media(index, 'mine', [item])
    summary = MediaDownloadSummary(index, 'mine', jobs)
    assert summary.read()['downloaded'] == 1
    with closing(sqlite3.connect(jobs)) as con:
        assert con.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
    clock = summary._checked
    voice.unlink()
    monkeypatch.setattr('noteford.media_summary.time.monotonic', lambda: clock + 6)
    assert summary.read() == dict(total=1, downloaded=0, not_downloaded=1, expired=0)


def test_unattempted_media_count_without_cache_tables_and_failure_is_unknown(tmp_path):
    index, jobs = seed(tmp_path)
    message(index, 'never')
    jobs.unlink()
    with closing(sqlite3.connect(index)) as con, con:
        con.execute('DROP TABLE media_exports')
    assert MediaDownloadSummary(index, 'mine', jobs).read() == dict(total=1, downloaded=0, not_downloaded=1, expired=0)
    assert not jobs.exists()
    assert MediaDownloadSummary(tmp_path / 'missing', 'mine', jobs).read() is None
