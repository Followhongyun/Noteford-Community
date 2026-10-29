import hashlib
import sqlite3
import threading

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from noteford import mac_cdn, mac_emoticon, mac_completion
from noteford.cdn_download import DownloadError


def encrypted(data, key):
    pad = 16 - len(data) % 16
    enc = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    return enc.update(data + bytes([pad]) * pad) + enc.finalize()


def fixture(tmp_path, monkeypatch):
    uin = '12345678'
    name = 'wxid_fixture'
    account = name + '_' + hashlib.md5(uin.encode()).hexdigest()[:4]
    root = tmp_path / 'readonly' / account
    root.mkdir(parents=True)
    stats = tmp_path / 'stats'
    stats.mkdir()
    (stats / f'key_{uin}_fixture.statistic').touch()
    monkeypatch.setattr(mac_emoticon, 'kvcomm_roots', lambda: [stats])
    config = {'account': account, 'project_root': str(tmp_path / 'noteford')}
    key = hashlib.md5((uin + name + 'EMOTICON').encode()).digest()
    data = b'GIF89a' + b'original animation fixture' * 30
    digest = hashlib.md5(data).hexdigest()
    return config, root, key, data, digest


def test_local_encrypted_sticker_without_url_keeps_exact_original(tmp_path, monkeypatch):
    config, root, key, data, digest = fixture(tmp_path, monkeypatch)
    source = root / 'cache/2026-10/Emoticon' / digest[:2] / digest
    source.parent.mkdir(parents=True)
    ciphertext = encrypted(data, key)
    source.write_bytes(ciphertext)
    monkeypatch.setattr(mac_cdn, 'live_root', lambda _: root)
    monkeypatch.setattr(mac_cdn, 'fetch', lambda *a, **k: pytest.fail('no network needed'))
    path = mac_cdn.download_emoji(config, 'sticker', {'md5': digest})
    assert path.read_bytes() == data and source.read_bytes() == ciphertext
    assert mac_emoticon.read_local({**config, 'account': 'another_' + config['account'].rsplit('_', 1)[-1]}, root, digest) is None
    source.write_bytes(ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]))
    assert mac_emoticon.read_local(config, root, digest) is None
    with pytest.raises(DownloadError, match='download_cancelled'):
        mac_emoticon.read_local(config, root, digest, should_continue=lambda: False)


def test_store_package_uses_verified_index_offset_and_checks_full_message_hash(tmp_path, monkeypatch):
    config, root, key, data, digest = fixture(tmp_path, monkeypatch)
    database = tmp_path / 'emoticon.db'
    product = 'com.tencent.fixture.pack'
    prefix = b'other sticker bytes' * 9
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE kStoreEmoticonFilesTable(package_id_,md5_,emoticon_offset_,emoticon_size_)')
        db.execute('INSERT INTO kStoreEmoticonFilesTable VALUES(?,?,?,?)', (product, digest, len(prefix), len(data)))
    monkeypatch.setattr(mac_emoticon, 'latest_decrypted_database', lambda *a: database)
    name = hashlib.md5(product.encode()).hexdigest()
    source = root / 'business/emoticon/PersistStore' / name[:2] / name
    source.parent.mkdir(parents=True)
    ciphertext = encrypted(prefix + data + b'next sticker', key)
    source.write_bytes(ciphertext)
    assert mac_emoticon.read_local(config, root, digest) == data
    assert source.read_bytes() == ciphertext
    with sqlite3.connect(database) as db:
        db.execute('UPDATE kStoreEmoticonFilesTable SET emoticon_offset_=emoticon_offset_+1')
    assert mac_emoticon.read_local(config, root, digest) is None
    with sqlite3.connect(database) as db:
        db.execute('UPDATE kStoreEmoticonFilesTable SET emoticon_size_=-1')
    assert mac_emoticon.read_local(config, root, digest) is None


def test_sticker_symlinks_size_limits_and_digest_mismatch_are_rejected(tmp_path, monkeypatch):
    config, root, key, data, digest = fixture(tmp_path, monkeypatch)
    source = root / 'business/emoticon/Persist' / digest[:2] / digest
    source.parent.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.write_bytes(data)
    source.symlink_to(outside)
    assert mac_emoticon.read_local(config, root, digest) is None
    source.unlink()
    source.write_bytes(encrypted(data + b'changed', key))
    assert mac_emoticon.read_local(config, root, digest) is None
    monkeypatch.setattr(mac_emoticon, 'MAX_BYTES', 32)
    assert mac_emoticon.read_local(config, root, digest) is None
    assert mac_emoticon.read_local(config, root, '../../outside') is None


def test_preview_upgrade_retries_after_restart_without_hammering_or_ignoring_optout(tmp_path, monkeypatch):
    from test_automatic_media import setup
    core, calls, index = setup(tmp_path)
    monkeypatch.setattr(mac_completion, 'live_root', lambda _: tmp_path / 'source')
    worker = mac_completion.MacAutomaticMedia(core)
    stop = threading.Event()
    while worker.run_one(stop, now=100):
        pass
    row = dict(next(worker._rows('image')))
    worker._record(row, 'local_original_unavailable', 5, 200)
    restarted = mac_completion.MacAutomaticMedia(core)
    assert not restarted.run_one(stop, now=201)
    assert not restarted.summary('follow')['failures'][0]['paused']
    with sqlite3.connect(index) as db:
        db.execute('UPDATE message_media_policy SET automatic=0 WHERE uid=?', ('image',))
    assert not restarted.run_one(stop, now=4000)
    with sqlite3.connect(index) as db:
        db.execute('UPDATE message_media_policy SET automatic=1 WHERE uid=?', ('image',))
    assert restarted.run_one(stop, now=4001) and calls[-1] == 'image'
    assert not mac_completion.MacAutomaticMedia(core).run_one(stop, now=9000)


def test_upgrade_requeues_old_missing_sticker_and_preview_only_once(tmp_path, monkeypatch):
    from test_automatic_media import setup
    core, calls, index = setup(tmp_path)
    monkeypatch.setattr(mac_completion, 'live_root', lambda _: tmp_path / 'source')
    worker = mac_completion.MacAutomaticMedia(core)
    row = dict(next(worker._rows('image')))
    worker._record(row, 'missing_download_reference', 1, 80000)
    with worker._state() as db:
        db.execute('PRAGMA user_version=2')
    worker = mac_completion.MacAutomaticMedia(core)
    with worker._state() as db:
        result = db.execute('SELECT status,attempts,retry_at FROM jobs').fetchone()
        assert tuple(result) == ('media_unavailable', 0, 0)
    worker._record(row, 'missing_download_reference', 1, 80000)
    worker = mac_completion.MacAutomaticMedia(core)
    with worker._state() as db:
        assert db.execute('SELECT status FROM jobs').fetchone()[0] == 'missing_download_reference'
