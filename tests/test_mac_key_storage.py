"""Synthetic secrets only: migration, failure retention and capture lifecycle."""
import json
from pathlib import Path

import pytest

from noteford.mac_capture import CaptureConfig, CaptureError, DB_KEY_ACCOUNT, MemoryKeyStore
from noteford.mac_key_storage import (
    cached_secret, capture_session, cleanup_stale_capture_files,
    database_payload, migrate_cache_path,
)


def record(account='a', byte='ab'):
    return {'relative_path': account + '/db_storage/session/session.db', 'key_hex': byte * 32, 'salt_hex': 'cd' * 16}


def legacy(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def test_database_migration_preserves_accounts_and_has_no_file_fallback(tmp_path):
    path = tmp_path / 'keys.json'
    store = MemoryKeyStore()
    store.put(DB_KEY_ACCOUNT, json.dumps({'keys': [record('a')], 'observed_accounts': ['a']}).encode())
    legacy(path, {'keys': [record('b')], 'observed_accounts': ['b']})
    result = database_payload(path, store)
    assert {x['relative_path'] for x in result['keys']} == {record('a')['relative_path'], record('b')['relative_path']}
    assert result['observed_accounts'] == ['a', 'b']
    assert not path.exists()
    assert database_payload(path, store) == result
    database_payload(path, store, captured=[record('a', 'ef')])
    assert not path.exists()
    assert len(json.loads(store.get(DB_KEY_ACCOUNT))['keys']) == 2


@pytest.mark.parametrize('failure', ['read', 'write', 'readback'])
def test_failed_migration_retains_legacy(tmp_path, failure):
    path = tmp_path / 'keys.json'
    legacy(path, {'keys': [record()]})
    before = path.read_bytes()
    class Broken:
        def get(self, account):
            if failure == 'read':
                raise CaptureError('keychain_read_failed')
            return None
        def put(self, account, value):
            if failure == 'write':
                raise CaptureError('keychain_write_failed')
    with pytest.raises(CaptureError):
        database_payload(path, Broken())
    assert path.read_bytes() == before


def test_conflict_and_symlink_do_not_delete_or_override(tmp_path):
    store = MemoryKeyStore()
    path = tmp_path / 'keys.json'
    database_payload(path, store, captured=[record()])
    legacy(path, {'keys': [record(byte='ef')]})
    with pytest.raises(CaptureError, match='key_migration_conflict'):
        database_payload(path, store)
    assert path.exists()
    alias = tmp_path / 'alias.json'
    alias.symlink_to(path)
    with pytest.raises(CaptureError, match='key_file_symlink'):
        database_payload(alias, store)
    assert path.exists()


@pytest.mark.parametrize('kind,payload', [('image', {'aes_key_ascii': 'bad', 'xor_key': 1}), ('candidates', {'candidates': [{'key_hex': 'bad'}]})])
def test_invalid_cache_is_not_deleted(tmp_path, kind, payload):
    path = tmp_path / 'legacy.json'
    legacy(path, payload)
    with pytest.raises(CaptureError, match='key_store_invalid'):
        cached_secret(path, kind, store=MemoryKeyStore())
    assert path.exists()


def test_image_cache_scope_and_registry_move_without_file(tmp_path):
    source = tmp_path / 'secrets/image_keys.json'
    destination = tmp_path / 'libraries/a/secrets/image_keys.json'
    other = tmp_path / 'libraries/b/secrets/image_keys.json'
    payload = {'aes_key_ascii': 'abcdefghijklmnop', 'xor_key': 7}
    legacy(source, payload)
    assert cached_secret(source, 'image') == payload
    assert not source.exists()
    migrate_cache_path(source, destination, 'image')
    assert cached_secret(destination, 'image') == payload
    assert cached_secret(other, 'image') is None
    assert not destination.exists()


@pytest.mark.parametrize('failure', [None, RuntimeError, KeyboardInterrupt])
def test_capture_cleanup_on_success_error_and_cancel(tmp_path, failure):
    config = CaptureConfig(official_app=tmp_path / 'official.app', work_root=tmp_path, db_root=tmp_path / 'db')
    @capture_session
    def run(config):
        for name in ('keys.pending.json', 'keys.partial.json', 'capture-events.jsonl'):
            (tmp_path / name).write_text('synthetic secret')
        cleanup_stale_capture_files(tmp_path)  # Must leave active capture alone.
        assert (tmp_path / 'keys.pending.json').exists()
        if failure:
            raise failure()
    if failure:
        with pytest.raises(failure):
            run(config)
    else:
        run(config)
    assert not list(tmp_path.glob('*.json*'))


def test_stale_cleanup_keeps_migration_source_and_unrelated_data(tmp_path):
    for name in ('keys.json', 'keys.partial.json', 'unrelated.json'):
        (tmp_path / name).write_text('synthetic')
    cleanup_stale_capture_files(tmp_path)
    assert (tmp_path / 'keys.json').exists()
    assert (tmp_path / 'unrelated.json').exists()
    assert not (tmp_path / 'keys.partial.json').exists()


def test_orphan_atomic_write_migrates_before_cleanup(tmp_path):
    path = tmp_path / 'image_keys.json'
    residue = path.with_suffix('.json.tmp')
    payload = {'aes_key_ascii': 'abcdefghijklmnop', 'xor_key': 8}
    legacy(residue, payload)
    store = MemoryKeyStore()
    assert cached_secret(path, 'image', store=store) == payload
    assert not residue.exists()
    assert not path.exists()
    assert cached_secret(path, 'image', store=store) == payload


def test_concurrent_reads_queue_instead_of_failing(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered, release, second_started = Event(), Event(), Event()
    class SlowStore(MemoryKeyStore):
        calls = 0
        def get(self, account):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                assert release.wait(5)
            return super().get(account)
    store = SlowStore()
    path = tmp_path / 'keys.json'
    def second():
        second_started.set()
        return database_payload(path, store)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(database_payload, path, store)
        assert entered.wait(5)
        other = pool.submit(second)
        assert second_started.wait(5)
        with pytest.raises(TimeoutError):
            other.result(timeout=.05)
        release.set()
        assert first.result(timeout=5) == other.result(timeout=5)


def test_hook_cleanup_removes_directory_to_prevent_late_recreation(tmp_path):
    from noteford.mac_key_storage import cleanup_hook_capture
    directory = tmp_path / 'hook-capture-123-test'
    directory.mkdir()
    output = directory / 'candidates.jsonl'
    output.write_text('synthetic')
    cleanup_hook_capture(output)
    with pytest.raises(FileNotFoundError):
        output.write_text('late writer')


def test_workspace_migrates_inactive_library_caches_without_touching_data(tmp_path):
    from noteford.mac_key_storage import migrate_workspace_caches
    store = MemoryKeyStore()
    path = tmp_path / 'libraries/a/secrets/image_keys.json'
    payload = {'aes_key_ascii': 'abcdefghijklmnop', 'xor_key': 8}
    legacy(path, payload)
    data = tmp_path / 'libraries/a/archive.db'
    data.write_bytes(b'unchanged archive')
    assert migrate_workspace_caches(tmp_path, store) == 1
    assert not path.exists()
    assert cached_secret(path, 'image', store=store) == payload
    assert data.read_bytes() == b'unchanged archive'
    assert migrate_workspace_caches(tmp_path, store) == 0
