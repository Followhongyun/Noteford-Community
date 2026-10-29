import sqlite3
import pytest
from datetime import datetime
from pathlib import Path

from noteford.media import MediaResolver
from noteford import media as media_module


def test_video_resource_alias_requires_hash_and_respects_loading_policy(tmp_path):
    import hashlib
    from types import SimpleNamespace
    root = tmp_path / 'wechat'
    folder = root / 'acct/msg/video/2026-09'
    folder.mkdir(parents=True)
    data = b'\x00\x00\x00\x18ftypisom' + b'video payload'
    digest = hashlib.md5(data).hexdigest()
    corrupt = folder / 'wrong.mp4'
    corrupt.write_bytes(b'wrong content')
    valid = folder / 'alias.mp4'
    valid.write_bytes(data)
    outside = tmp_path / 'other-account.mp4'
    outside.write_bytes(data)
    lookup = SimpleNamespace(find_video=lambda *a, **kw: (digest, [outside, corrupt, valid]))
    resolver = MediaResolver(root, 'acct', image_resources=lookup)
    timestamp = int(datetime(2026, 9, 13).timestamp())
    (folder / 'message-id.jpg').write_bytes(b'thumbnail')
    resolver.original_allowed = lambda uid: False
    assert resolver.export_video(timestamp, 'message-id', 'uid', tmp_path / 'cache').status == 'thumbnail_only'
    resolver.original_allowed = None
    result = resolver.export_video(timestamp, 'message-id', 'uid', tmp_path / 'cache')
    assert result.status == 'ready'
    assert Path(result.path).read_bytes() == data
    assert Path(result.path).name == 'alias.mp4'
    valid.write_bytes(b'now corrupt')
    resolver.force_copy = True
    assert resolver.export_video(timestamp, 'message-id', 'uid', tmp_path / 'cache').status == 'thumbnail_only'


def test_recheck_upgrades_thumbnail_and_replaces_same_size_cache(tmp_path):
    timestamp = int(datetime(2026, 9, 21, 12).timestamp())
    folder = tmp_path / 'wechat' / 'acct' / 'msg' / 'attach' / 'hash' / '2026-09' / 'Img'
    folder.mkdir(parents=True)
    (folder / 'abc_t.dat').write_bytes(b'\xff\xd8\xffthumb')
    resolver = MediaResolver(tmp_path / 'wechat', 'acct')
    cache = tmp_path / 'cache'
    assert resolver.export_image('Msg_hash', timestamp, 'abc', 'uid', cache).status == 'ready_thumbnail'
    source = folder / 'abc_h.dat'
    source.write_bytes(b'\xff\xd8\xfffirst')
    original = resolver.export_image('Msg_hash', timestamp, 'abc', 'uid', cache)
    assert original.status == 'ready'
    source.write_bytes(b'\xff\xd8\xfflater')
    resolver.export_image('Msg_hash', timestamp, 'abc', 'uid', cache)
    assert Path(original.path).read_bytes() == source.read_bytes()
    video = tmp_path / 'wechat' / 'acct' / 'msg' / 'video' / '2026-09'
    video.mkdir(parents=True)
    (video / 'abc.jpg').write_bytes(b'\xff\xd8\xffthumb')
    assert resolver.export_video(timestamp, 'abc', 'uid', cache).status == 'thumbnail_only'
    source = video / 'abc.mp4'
    source.write_bytes(b'first-video')
    full = resolver.export_video(timestamp, 'abc', 'uid', cache)
    assert full.status == 'ready'
    source.write_bytes(b'later-video')
    resolver.force_copy = True
    resolver.export_video(timestamp, 'abc', 'uid', cache)
    assert Path(full.path).read_bytes() == source.read_bytes()


def test_recheck_uses_index_identity_and_rejects_unfollowed(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from noteford.service import LocalApi
    from noteford import service
    monkeypatch.setattr(service.sys, 'platform', 'win32')
    resolver = MediaResolver(tmp_path / 'wechat', 'acct')
    item = {'uid': 'id', 'timestamp': 0, 'type': {'kind': 'image'}, 'media': {'md5': None}, 'source': {'table': 'Msg_hash'}}
    recorded = []
    core = SimpleNamespace(repository=SimpleNamespace(message_by_uid=lambda uid: {'conversation': {'username': 'chat'}, 'message': item} if uid == 'id' else None),
                           config={'index_path': str(tmp_path / 'index')}, account='acct', _media_resolver=lambda: resolver, media_cache_root=tmp_path / 'cache',
                           automatic_media=SimpleNamespace(record_manual_result=lambda uid, media: recorded.append((uid, media))))
    monkeypatch.setattr(service, 'selection_status', lambda *args: {'state': 'following'})
    def unavailable(*args):
        raise service.DownloadError('missing_download_reference')
    monkeypatch.setattr(service, 'download_media', unavailable)
    assert LocalApi.recheck_media(core, 'id')['messages'][0]['media']['status'] == 'missing_id'
    assert recorded[0][0] == 'id'
    with pytest.raises(ValueError, match='message_not_found'):
        LocalApi.recheck_media(core, 'another-account-id')
    monkeypatch.setattr(service, 'selection_status', lambda *args: {'state': 'unfollowed'})
    with pytest.raises(ValueError, match='conversation_not_followed'):
        LocalApi.recheck_media(core, 'id')


def test_media_resolver_exports_read_only_local_artifacts(tmp_path):
    account = 'acct'
    root = tmp_path / 'wechat'
    account_root = root / account
    timestamp = int(datetime(2026, 9, 21, 12, 0, 0).timestamp())
    month = '2026-09'
    chat_hash = '0123456789abcdef0123456789abcdef'
    chat_table = 'Msg_' + chat_hash
    md5 = 'fedcba9876543210fedcba9876543210'

    image = account_root / 'msg' / 'attach' / chat_hash / month / 'Img' / f'{md5}_t.dat'
    image.parent.mkdir(parents=True)
    image.write_bytes(b'encrypted-image')

    video = account_root / 'msg' / 'video' / month / f'{md5}.mp4'
    video.parent.mkdir(parents=True)
    video.write_bytes(b'video-bytes')

    attachment = account_root / 'msg' / 'file' / month / 'report.pdf'
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b'pdf-bytes')

    media_db = tmp_path / 'media_0.db'
    con = sqlite3.connect(media_db)
    con.execute('CREATE TABLE Name2Id(user_name TEXT)')
    con.execute('INSERT INTO Name2Id(user_name) VALUES(?)', ('wxid_friend',))
    chat_id = con.execute('SELECT rowid FROM Name2Id WHERE user_name=?', ('wxid_friend',)).fetchone()[0]
    con.execute(
        'CREATE TABLE VoiceInfo(chat_name_id INTEGER, create_time INTEGER, local_id INTEGER, svr_id INTEGER, voice_data BLOB, data_index INTEGER)'
    )
    con.execute(
        'INSERT INTO VoiceInfo VALUES(?,?,?,?,?,?)',
        (chat_id, timestamp, 1, 12345, b'\x02#!SILK_V3voice', 0),
    )
    con.commit()
    con.close()

    out = tmp_path / 'media-cache'
    resolver = MediaResolver(root, account, media_db_paths=[media_db])

    img = resolver.export_image(chat_table, timestamp, md5, 'u1', out)
    assert img.status == 'encrypted_thumbnail'
    assert img.encrypted is True
    assert Path(img.path).read_bytes() == b'encrypted-image'

    vid = resolver.export_video(timestamp, md5, 'u2', out)
    assert vid.status == 'ready'
    assert Path(vid.path).read_bytes() == b'video-bytes'

    voice = resolver.export_voice('wxid_friend', 12345, 'u3', out)
    assert voice.status == 'encoded_voice'
    assert Path(voice.path).read_bytes().startswith(b'\x02#!SILK_V3')

    file = resolver.export_file(timestamp, 'report.pdf', 'u4', out)
    assert file.status == 'ready'
    assert Path(file.path).read_bytes() == b'pdf-bytes'


@pytest.mark.parametrize('decoder_status', ['unsupported_image_container', 'image_decoder_unavailable'])
def test_wxgf_original_falls_back_to_viewable_jpeg_thumbnail(tmp_path, monkeypatch, decoder_status):
    account = 'acct'
    timestamp = int(datetime(2026, 9, 21, 12).timestamp())
    md5 = 'a' * 32
    folder = (tmp_path / 'wechat' / account / 'msg' / 'attach'
              / 'hash' / '2026-09' / 'Img')
    folder.mkdir(parents=True)
    (folder / f'{md5}.dat').write_bytes(b'original')
    (folder / f'{md5}_t.dat').write_bytes(b'thumbnail')
    monkeypatch.setattr(media_module, 'load_image_keys', lambda _path: (b'a' * 16, 1))
    monkeypatch.setattr(
        media_module, 'decrypt_v2_bytes',
        lambda contents, *_args: b'wxgf' if contents == b'original' else b'\xff\xd8\xffjpeg',
    )
    # Exercise both fallback reasons independently of host-installed FFmpeg.
    monkeypatch.setattr(media_module, 'decode_wxgf', lambda *_args: (None, decoder_status))
    resolver = MediaResolver(tmp_path / 'wechat', account, image_keys_path=tmp_path / 'key')

    artifact = resolver.export_image('Msg_hash', timestamp, md5, 'uid', tmp_path / 'cache')
    assert artifact.status == 'ready_thumbnail'
    assert artifact.original_status == decoder_status
    assert Path(artifact.path).read_bytes().startswith(b'\xff\xd8\xff')
    assert (tmp_path / 'cache' / 'images' / f'{md5}.wxgf').is_file()

def test_content_index_recovers_original_under_other_message_name(tmp_path):
    import hashlib
    from types import SimpleNamespace
    timestamp = int(datetime(2026, 10, 11, 12).timestamp())
    folder = tmp_path / 'wechat' / 'acct' / 'msg' / 'attach' / 'chat' / '2026-10' / 'Img'
    folder.mkdir(parents=True)
    (folder / 'abc_t.dat').write_bytes(b'\xff\xd8\xffthumb')
    original = tmp_path / 'wechat' / 'acct' / 'msg' / 'attach' / 'other' / '2026-09' / 'Img' / 'different_h.dat'
    original.parent.mkdir(parents=True)
    plain = b'\xff\xd8\xfforiginal'
    original.write_bytes(plain)
    resolver = MediaResolver(tmp_path / 'wechat', 'acct')
    resolver.image_resources = SimpleNamespace(find_image=lambda uid, force=False: (hashlib.md5(plain).hexdigest(), [original]))
    result = resolver.export_image('Msg_chat', timestamp, 'abc', 'uid', tmp_path / 'cache')
    assert result.status == 'ready'
    assert Path(result.path).name == 'abc.jpg'
    assert Path(result.path).read_bytes() == plain
    assert original.read_bytes() == plain


def test_resource_mismatch_keeps_thumbnail(tmp_path):
    from types import SimpleNamespace
    timestamp = int(datetime(2026, 10, 11, 12).timestamp())
    folder = tmp_path / 'wechat' / 'acct' / 'msg' / 'attach' / 'chat' / '2026-10' / 'Img'
    folder.mkdir(parents=True)
    (folder / 'abc_t.dat').write_bytes(b'\xff\xd8\xffthumb')
    wrong = folder / 'wrong_h.dat'
    wrong.write_bytes(b'\xff\xd8\xffdifferent')
    resolver = MediaResolver(tmp_path / 'wechat', 'acct')
    resolver.image_resources = SimpleNamespace(find_image=lambda uid, force=False: ('a' * 32, [wrong]))
    result = resolver.export_image('Msg_chat', timestamp, 'abc', 'uid', tmp_path / 'cache')
    assert result.status == 'ready_thumbnail'
    assert result.original_status == 'missing'

def test_resource_cannot_recover_from_another_account(tmp_path):
    import hashlib
    from types import SimpleNamespace
    original = tmp_path / 'wechat' / 'other' / 'original.dat'
    original.parent.mkdir(parents=True)
    plain = b'\xff\xd8\xfforiginal'
    original.write_bytes(plain)
    resolver = MediaResolver(tmp_path / 'wechat', 'acct')
    resolver.image_resources = SimpleNamespace(find_image=lambda uid, force=False: (hashlib.md5(plain).hexdigest(), [original]))
    result = resolver.export_image('Msg_hash', 0, 'abc', 'uid', tmp_path / 'cache')
    assert result.status == 'missing'
    assert not (tmp_path / 'cache').exists()
