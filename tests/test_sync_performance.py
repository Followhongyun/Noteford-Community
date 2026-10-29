"""Synthetic regression coverage for bounded and restartable sync work."""
import json
import sqlite3
import struct
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from noteford import crypto, mac_live_sync as live
from noteford.file_copy import copy_snapshot
from noteford.source_cache import cached_source_stage
from test_mac_live_sync import setup_live


KEY = bytes(range(32))
SALT = b'12345678'


def wal_file(path, frames):
    with path.open('wb') as out:
        out.write(struct.pack('>IIII', 0x377f0682, 3007000, 4096, 1) + SALT + bytes(8))
        for number, commit, content, salt in frames:
            iv = bytes(range(16))
            plain = content.ljust(4016, b'\0')
            cipher = Cipher(algorithms.AES(KEY), modes.CBC(iv)).encryptor()
            out.write(struct.pack('>II', number, commit) + salt + bytes(8))
            out.write(cipher.update(plain) + cipher.finalize() + iv + bytes(64))


def test_wal_latest_committed_overflow_and_reset_tail(tmp_path, monkeypatch):
    db, wal = tmp_path/'db', tmp_path/'wal'
    db.write_bytes(bytes(4096 * 4))
    wal_file(wal, [(2, 0, b'old', SALT), (2, 4, b'\xffOVERFLOW', SALT),
                   (3, 0, b'uncommitted', SALT), (2, 4, b'reset tail', b'87654321')])
    # No whole-file reads, including the database copy.
    monkeypatch.setattr(Path, 'read_bytes', lambda *_: pytest.fail('whole file read'))
    assert crypto.merge_encrypted_wal(db, wal, KEY) == 1
    with db.open('rb') as handle:
        handle.seek(4096)
        assert handle.read(9) == b'\xffOVERFLOW'
        handle.seek(8192)
        assert handle.read(16) == bytes(16)


def test_no_commit_and_failure_preserve_database(tmp_path, monkeypatch):
    db, wal = tmp_path/'db', tmp_path/'wal'
    original = bytes(4096 * 2)
    db.write_bytes(original)
    wal_file(wal, [(2, 0, b'pending', SALT)])
    assert crypto.merge_encrypted_wal(db, wal, KEY) == 0
    wal_file(wal, [(2, 2, b'committed', SALT)])
    def fail(*_):
        raise ValueError('decrypt failed')
    monkeypatch.setattr(crypto, 'decrypt_page', fail)
    with pytest.raises(ValueError):
        crypto.merge_encrypted_wal(db, wal, KEY)
    assert db.read_bytes() == original
    assert not list(tmp_path.glob('*.tmp'))


def test_copy_never_links_writable_snapshot_to_base(tmp_path):
    base, target = tmp_path/'base', tmp_path/'copy'
    base.write_bytes(b'old')
    copy_snapshot(base, target)
    assert base.stat().st_ino != target.stat().st_ino
    target.write_bytes(b'new')
    assert base.read_bytes() == b'old'


def test_reuse_checkpoint_missing_base_and_repeated_arrivals(setup_live):
    archive, account, storage, decrypted = setup_live
    live.refresh_managed_archive(archive, account)
    source = storage/'message/message_0.db'
    wal = Path(str(source)+'-wal')
    for i in range(8):
        wal.write_text(str(i))
        assert live.refresh_managed_archive(archive, account)['changed'] == 1
    assert decrypted.count('message_0.db') == 1
    generations = list((archive/account/'message').glob('message_0-live-*'))
    assert len(generations) == 2
    assert len({(p/'.source-base').stat().st_ino for p in generations}) == 1
    for generation in generations:
        (generation/'.source-base').unlink()
    wal.write_text('missing cached base')
    live.refresh_managed_archive(archive, account)
    assert decrypted.count('message_0.db') == 2
    source.write_bytes(source.read_bytes() + b'checkpoint')
    wal.unlink()
    live.refresh_managed_archive(archive, account)
    assert decrypted.count('message_0.db') == 3
    assert live.refresh_managed_archive(archive, account)['changed'] == 0


def test_concurrent_refresh_is_rejected_and_staging_recovers(setup_live, monkeypatch):
    archive, account, storage, _ = setup_live
    entered, release = threading.Event(), threading.Event()
    decrypt = live.decrypt_database
    def blocked(*args):
        entered.set()
        assert release.wait(5)
        return decrypt(*args)
    monkeypatch.setattr(live, 'decrypt_database', blocked)
    failures = []
    def refresh():
        try:
            live.refresh_managed_archive(archive, account)
        except Exception as error:
            failures.append(error)
    worker = threading.Thread(target=refresh)
    worker.start()
    assert entered.wait(5)
    try:
        with pytest.raises(RuntimeError, match='data_root_in_use'):
            live.refresh_managed_archive(archive, account)
    finally:
        release.set()
        worker.join(5)
    assert not failures and not worker.is_alive()
    abandoned = archive/account/'message/.live-crashed'
    abandoned.mkdir()
    (abandoned/'partial').write_text('partial')
    # A changed file triggers recovery under the same OS-owned lease.
    (storage/'message/message_0.db-wal').write_text('after crash')
    assert live.refresh_managed_archive(archive, account)['changed'] == 1
    assert not abandoned.exists()


def test_source_cache_is_per_index_account_and_tracks_wal_and_failure(tmp_path):
    source, index = tmp_path/'source.db', tmp_path/'index.db'
    source.write_bytes(b'source')
    calls = []
    def action():
        calls.append(1)
        return {'count': 1}
    def run(account='one', path=index):
        return cached_source_stage(path, account, 'stage', [source], action)
    assert run()[1] is False
    assert run()[1] is True
    assert run('two')[1] is False
    assert run(path=tmp_path/'new-index.db')[1] is False
    Path(str(source)+'-wal').write_bytes(b'new transaction')
    assert run()[1] is False
    assert len(calls) == 4
    source.write_bytes(b'old record changed')
    def fail():
        raise RuntimeError('interrupted')
    with pytest.raises(RuntimeError):
        cached_source_stage(index, 'one', 'stage', [source], fail)
    assert run()[1] is False


def test_failed_later_database_never_prunes_manifest_recovery_generation(setup_live, monkeypatch):
    archive, account, storage, _ = setup_live
    live.refresh_managed_archive(archive, account)
    manifest = next((archive.parent/'state/mac-live').glob('*.json'))
    original = manifest.read_bytes()
    outputs = [archive/row['output'] for row in json.loads(original).values()]
    publish = live._publish_snapshot
    def fail_session(source, *args, **kwargs):
        if source.name == 'session.db':
            raise RuntimeError('interrupted later database')
        return publish(source, *args, **kwargs)
    monkeypatch.setattr(live, '_publish_snapshot', fail_session)
    for i in range(3):
        (storage/'message/message_0.db-wal').write_text(str(i))
        (storage/'session/session.db-wal').write_text(str(i))
        with pytest.raises(RuntimeError, match='interrupted later'):
            live.refresh_managed_archive(archive, account)
        assert manifest.read_bytes() == original
        assert all(path.is_file() for path in outputs)
    monkeypatch.setattr(live, '_publish_snapshot', publish)
    assert live.refresh_managed_archive(archive, account)['changed'] == 2
    assert len(list((archive/account/'message').glob('message_0-live-*'))) == 2


def test_unchanged_archive_skips_index_and_metadata_after_restart(tmp_path, monkeypatch):
    from test_mac_metadata import fixture_archive, refresh
    from noteford import indexer, metadata
    archive, account, _ = fixture_archive(tmp_path)
    project = tmp_path/'project'
    project.mkdir()
    refresh(archive, account, project)
    def fail(**_):
        pytest.fail('unchanged stage repeated')
    monkeypatch.setattr(indexer, 'index_messages', fail)
    monkeypatch.setattr(metadata, 'sync_metadata', fail)
    result = refresh(archive, account, project)
    assert all(row['skipped'] for row in result['raw_archives'])


def test_media_backlog_is_bounded_and_failed_retries_survive_restart(tmp_path):
    from test_automatic_media import setup
    from test_media_backfill import VideoResolver
    from noteford.media import MediaArtifact
    from noteford.media_backfill import prefetch_recent_media
    core, _, index = setup(tmp_path)
    with sqlite3.connect(index) as con:
        con.execute('UPDATE messages SET local_type=43')
        stamp = con.execute("SELECT create_time FROM messages WHERE uid='video'").fetchone()[0]
        con.executemany('''INSERT INTO messages(uid,account,source_db,chat_table,local_id,
            server_id,local_type,create_time,row_hash,indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            [(f'new-{i}', 'acct', 'message_0.db', 'Msg_follow', 100+i, str(100+i),
              43, stamp, 'v1', 'now') for i in range(250)])
    resolver = VideoResolver()
    def run(now=1):
        return prefetch_recent_media(index_path=index, account='acct', resolver=resolver,
            destination_root=tmp_path/'media', ranges=core._media_ranges(), max_attempts=7, now=now)
    assert run()['batch_limited']
    assert len(resolver.calls) == 7
    assert run()['attempted'] == 7
    assert len({uid for _, uid in resolver.calls}) == 14
    resolver.export_video = lambda *a: MediaArtifact('missing', 'video', None, None, None, False)
    # Limit the remaining queue to seven failures, then prove the next process
    # respects their persisted backoff without hiding changed message revisions.
    with sqlite3.connect(index) as con:
        con.execute('''DELETE FROM messages WHERE uid NOT IN
            (SELECT uid FROM messages ORDER BY create_time,uid LIMIT 21)''')
    assert run()['attempted'] <= 7
    assert run()['attempted'] == 0
    assert run(now=122)['attempted'] > 0


def test_new_message_and_old_record_revision_are_indexed_then_skipped(tmp_path):
    from test_mac_metadata import fixture_archive, refresh
    archive, account, _ = fixture_archive(tmp_path)
    project = tmp_path/'project'
    project.mkdir()
    refresh(archive, account, project)
    source = archive/account/'message_0.db'
    with sqlite3.connect(source) as con:
        table = con.execute("SELECT name FROM sqlite_master WHERE name LIKE 'Msg_%'").fetchone()[0]
        con.execute(f'UPDATE "{table}" SET message_content=? WHERE local_id=1', ('edited old',))
        con.execute(f'INSERT INTO "{table}" VALUES(3,103,102,1,?,2)', ('new arrival',))
    result = refresh(archive, account, project)
    assert result['raw_archives'][0]['indexed'] == 2
    with sqlite3.connect(project/'index.db') as con:
        assert con.execute('SELECT message_content FROM messages ORDER BY local_id').fetchall() == [
            ('edited old',), ('reply',), ('new arrival',)]
    assert refresh(archive, account, project)['raw_archives'][0]['skipped']


def test_source_race_retries_are_bounded_and_keep_last_good_snapshot(setup_live, monkeypatch):
    archive, account, storage, _ = setup_live
    live.refresh_managed_archive(archive, account)
    manifest = next((archive.parent/'state/mac-live').glob('*.json'))
    before = manifest.read_bytes()
    wal = storage/'message/message_0.db-wal'
    wal.write_text('new')
    copy = live.shutil.copyfile
    races = []
    def race(source, destination, *a, **kw):
        result = copy(source, destination, *a, **kw)
        if source == wal:
            races.append(1)
            wal.write_text('new' + str(len(races)))
        return result
    monkeypatch.setattr(live.shutil, 'copyfile', race)
    with pytest.raises(RuntimeError, match='source_busy'):
        live.refresh_managed_archive(archive, account)
    assert len(races) == 3
    assert manifest.read_bytes() == before
    assert not list(archive.rglob('.live-*'))
    monkeypatch.setattr(live.shutil, 'copyfile', copy)
    assert live.refresh_managed_archive(archive, account)['changed'] == 1


def test_cached_base_validation_failure_falls_back_to_full_decrypt(setup_live, monkeypatch):
    archive, account, storage, decrypted = setup_live
    live.refresh_managed_archive(archive, account)
    wal = storage/'message/message_0.db-wal'
    wal.write_text('new')
    checks = []
    check = live.quick_check
    def reject_first(path):
        checks.append(1)
        return len(checks) > 1 and check(path)
    monkeypatch.setattr(live, 'quick_check', reject_first)
    assert live.refresh_managed_archive(archive, account)['changed'] == 1
    assert len(checks) == 2
    assert decrypted.count('message_0.db') == 2


def test_writer_lease_is_released_after_process_crash(tmp_path):
    from noteford.runtime_lock import RuntimeLock
    script = '''import os,sys
from pathlib import Path
from noteford.runtime_lock import RuntimeLock
with RuntimeLock(Path(sys.argv[1])):
    os._exit(19)
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path)], timeout=5)
    assert result.returncode == 19
    with RuntimeLock(tmp_path):
        pass


def test_source_cache_closes_connections_without_waiting_for_gc(tmp_path):
    import gc
    import psutil
    source = tmp_path/'source'
    source.write_bytes(b'synthetic')
    proc = psutil.Process()
    if not hasattr(proc, 'num_fds'):
        pytest.skip('file descriptor counts unavailable')
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        before = proc.num_fds()
        for i in range(100):
            cached_source_stage(tmp_path/'index.db','fixture','stage',[source],lambda: {'ok': True})
        assert proc.num_fds() <= before + 1
    finally:
        if was_enabled:
            gc.enable()
