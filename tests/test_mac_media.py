import hashlib
import base64
import os
import sqlite3
import struct
import wave
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from noteford import mac_media
from noteford.image_crypto import V2_MAGIC, decrypt_v2_bytes
from noteford.mac_metadata import register_sources
from noteford.media import latest_decrypted_media_databases


def image_fixture(tmp_path, uin='12345678'):
    username = 'wxid_fixture'
    account = username + '_' + hashlib.md5(uin.encode()).hexdigest()[:4]
    root = tmp_path/'wechat'/account
    folder = root/'msg/attach' / ('a'*32) / '2026-10/Img'
    folder.mkdir(parents=True)
    key = hashlib.md5((uin+username).encode()).hexdigest()[:16].encode()
    plain = b'\x89PNG\r\n\x1a\n' + b'fixture bytes' * 4
    size = 16
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    encrypted = encryptor.update(plain[:size] + b'\x10'*16) + encryptor.finalize()
    data = V2_MAGIC + struct.pack('<LL',size,0) + b'\x00' + encrypted + plain[size:]
    (folder/('b'*32+'_t.dat')).write_bytes(data)
    cache = tmp_path/'kvcomm';cache.mkdir()
    (cache/f'key_{uin}_fixture.statistic').touch()
    return root,cache,plain,key


def test_mac_image_derivation_validates_real_ciphertext_and_keeps_source_readonly(tmp_path):
    root,cache,plain,key = image_fixture(tmp_path)
    sample = next(root.rglob('*.dat'));before=sample.read_bytes()
    destination=tmp_path/'noteford/secrets/image_keys.json'
    keys=mac_media.local_image_keys(root,destination,[cache])
    assert keys == (key,12345678 & 255)
    assert decrypt_v2_bytes(before,*keys) == plain
    # Windows CI does not expose POSIX owner/group permission bits.
    import sys
    if sys.platform == 'darwin':
        assert not destination.exists()
    elif os.name != 'nt':
        assert destination.stat().st_mode & 0o777 == 0o600
    assert mac_media.local_image_keys(root,destination,[]) == keys
    assert sample.read_bytes() == before


def test_mac_image_key_is_never_guessed_from_another_account_or_bad_probe(tmp_path):
    root,cache,_,_=image_fixture(tmp_path)
    destination=tmp_path/'keys.json'
    sample=next(root.rglob('*.dat'))
    data=bytearray(sample.read_bytes());data[16]^=1;sample.write_bytes(data)
    assert mac_media.local_image_keys(root,destination,[cache]) is None
    assert not destination.exists()


def test_offline_import_does_not_read_live_account_or_cache(tmp_path,monkeypatch):
    monkeypatch.setattr(mac_media,'managed_archive',lambda:tmp_path/'managed')
    monkeypatch.setattr(mac_media,'_storage',lambda _:pytest.fail('live account opened'))
    monkeypatch.setattr(mac_media,'local_image_keys',lambda *_:pytest.fail('live key discovery'))
    resolver=mac_media.media_resolver(tmp_path/'import','account',tmp_path/'project',tmp_path/'index.db',tmp_path/'keys')
    assert resolver.account_root == tmp_path/'import/account'


def test_mac_voice_sources_are_registered_with_current_snapshot(tmp_path):
    project=tmp_path/'project';source=tmp_path/'media_0.db'
    with sqlite3.connect(source) as con:con.execute('CREATE TABLE VoiceInfo(voice_data)')
    register_sources(project,'account',tmp_path/'session.db',tmp_path/'contact.db',None,[source])
    assert latest_decrypted_media_databases(project,'account') == [source]
    assert latest_decrypted_media_databases(project,'another') == []


def test_bundled_mac_voice_decoder_produces_playable_wav(tmp_path):
    decoder=mac_media.MacVoiceDecoder()
    if not decoder.available:
        pytest.skip('Mac native helper is built separately before packaging')
    silk=tmp_path/'voice.silk';wav=tmp_path/'audio.wav'
    # Generated 1-second 440 Hz sine tone, never a real message or recording.
    silk.write_bytes(base64.b64decode((Path(__file__).parent/'fixtures/mac-tone.silk.b64').read_bytes()))
    before=silk.read_bytes()
    result=decoder.decode(silk,wav)
    assert result.ok
    with wave.open(str(wav),'rb') as output:
        assert output.getframerate()==24000 and output.getnchannels()==1
        assert 22000 <= output.getnframes() <= 26000
    assert silk.read_bytes()==before


def test_mac_voice_decoder_rejects_invalid_packets_without_replacing_audio(tmp_path):
    decoder=mac_media.MacVoiceDecoder()
    if not decoder.available:
        pytest.skip('Mac native helper is built separately before packaging')
    silk=tmp_path/'bad.silk';wav=tmp_path/'audio.wav'
    wav.write_bytes(b'previous audio')
    for data in (b'not silk', b'\x02#!SILK_V3\x00\x20short', b'#!SILK_V3'):
        silk.write_bytes(data)
        assert not decoder.decode(silk,wav).ok
        assert wav.read_bytes()==b'previous audio'


def test_mac_recheck_preserves_preview_and_does_not_use_windows_download_probe(tmp_path,monkeypatch):
    from types import SimpleNamespace
    from noteford import service
    from noteford.media import MediaArtifact
    monkeypatch.setattr(service.sys,'platform','darwin')
    monkeypatch.setattr(service,'selection_status',lambda *a:{'state':'following'})
    monkeypatch.setattr(service,'download_media',lambda *a,**k:pytest.fail('Windows CDN probe'))
    from noteford import mac_completion
    from noteford.cdn_download import DownloadError
    monkeypatch.setattr(mac_completion, 'message_xml', lambda *a: None)
    def unavailable(*a, **k):
        raise DownloadError('network_unavailable')
    monkeypatch.setattr(mac_completion, 'download_media', unavailable)
    artifact=MediaArtifact('ready_thumbnail','image','preview.png',None,10,False)
    item={'uid':'fixture','timestamp':0,'source':{'table':'Msg_fixture'},'type':{'kind':'image'},'media':{}}
    core=SimpleNamespace(
        config={'index_path':str(tmp_path/'index.db')},account='account',media_cache_root=tmp_path/'cache',
        repository=SimpleNamespace(message_by_uid=lambda _:{'message':item,'conversation':{'username':'chat'}}),
        _media_resolver=lambda:SimpleNamespace(export_image=lambda *a:artifact),
        automatic_media=SimpleNamespace(record_manual_result=lambda *a:None))
    media=service.LocalApi.recheck_media(core,'fixture')['messages'][0]['media']
    assert media['status']=='ready_thumbnail' and media['path']=='preview.png'
    assert media['download_error']=='network_unavailable'


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_mac_message_read_automatically_upgrades_thumbnail_even_outside_download_range(tmp_path, monkeypatch, kind):
    import copy
    from datetime import datetime
    from types import SimpleNamespace
    from noteford import service
    from noteford.media import MediaResolver
    timestamp = int(datetime(2026, 10, 17).timestamp())
    digest = 'b' * 32
    root = tmp_path/'source/account'
    folder = root/('msg/attach/' + 'a'*32 + '/2026-10/Img' if kind == 'image' else 'msg/video/2026-10')
    folder.mkdir(parents=True)
    preview = b'\x89PNG\r\n\x1a\nfixture preview'
    thumbnail = folder/(digest + ('_t.dat' if kind == 'image' else '.jpg'))
    thumbnail.write_bytes(preview)
    resolver = MediaResolver(tmp_path/'source', 'account')
    monkeypatch.setattr(service.sys, 'platform', 'darwin')
    monkeypatch.setattr(mac_media, 'media_resolver', lambda *a: resolver)
    monkeypatch.setattr(service, 'allows_original', lambda *a: pytest.fail('download range limited a local Mac read'))
    monkeypatch.setattr(service, 'selection_status', lambda *a: {'state':'following'})
    item = {'uid':'fixture', 'timestamp':timestamp, 'source':{'table':'Msg_' + 'a'*32},
            'type':{'kind':kind}, 'sender':{}, 'media':{'md5':digest}}
    core = SimpleNamespace(config={'index_path':str(tmp_path/'index.db')}, account='account',
        wechat_root=tmp_path/'source', project_root=tmp_path/'noteford', image_keys_path=tmp_path/'keys',
        media_cache_root=tmp_path/'cache', _avatar_fields=lambda _: {},
        repository=SimpleNamespace(red_envelope_ids=lambda _: {}, list_messages=lambda *a,**k:
            {'conversation':{'username':'chat'}, 'messages':[copy.deepcopy(item)]}),
        automatic_media=SimpleNamespace(expired_ids=lambda _:set()))
    core._media_resolver=lambda: service.LocalApi._media_resolver(core)
    first=service.LocalApi.messages(core, 'chat')['messages'][0]['media']
    assert first['status'] == ('ready_thumbnail' if kind == 'image' else 'thumbnail_only')
    assert first['download_error'] == 'local_original_unavailable'
    assert Path(first['path']).read_bytes() == preview
    # A later source file appears without a new message or a manual recheck.
    original = folder/(digest + ('.dat' if kind == 'image' else '.mp4'))
    contents = b'\x89PNG\r\n\x1a\noriginal bytes' if kind == 'image' else b'fixture video'
    original.write_bytes(contents)
    # Even a previously established expiry must not hide a readable local copy.
    core.automatic_media.expired_ids=lambda _: {'fixture'}
    second=service.LocalApi.messages(core, 'chat')['messages'][0]['media']
    assert second['status'] == 'ready' and 'download_error' not in second
    assert Path(second['path']).read_bytes() == contents
    assert thumbnail.read_bytes() == preview and original.read_bytes() == contents


@pytest.mark.parametrize(('status','original','error'), [
    ('ready', None, None), ('missing', None, 'local_original_unavailable'),
    ('thumbnail_only', None, 'local_original_unavailable'),
    ('ready_thumbnail', 'encrypted_original', 'local_decryption_unavailable'),
    ('ready_thumbnail', 'wxgf_container', 'unsupported_download_format'),
    ('ready_thumbnail', 'unreadable', 'local_media_unreadable'),
    ('insufficient_space', None, 'insufficient_space'),
])
def test_mac_local_media_errors_never_infer_server_expiry(status, original, error):
    from noteford.media import MediaArtifact
    artifact=MediaArtifact(status, 'image', None, None, None, False, original_status=original)
    assert mac_media.media_error(artifact) == error
