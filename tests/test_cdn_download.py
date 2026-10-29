import hashlib
import http.client
import io
import time
import urllib.error
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from noteford import cdn_download as cdn
from noteford.downloaded_media import retain_download


def fixture(plain=None):
    plain = plain or b'\x89PNG\r\n\x1a\n' + b'a'*90000
    padding = 16-len(plain)%16
    enc = Cipher(algorithms.AES(b'k'*16), modes.ECB()).encryptor()
    encrypted = enc.update(plain+bytes([padding])*padding)+enc.finalize()
    digest = hashlib.md5(plain).hexdigest()
    attrs = {'aeskey': (b'k'*16).hex(), 'md5': digest, 'hdlength': str(len(plain)), 'length': str(len(plain))}
    def reply(req, deadline):
        start, end = int(req['rangestart']), int(req['rangeend'])
        return {'retcode': b'0', 'totalsize': str(len(encrypted)).encode(),
                'rangestart': str(start).encode(), 'filedata': encrypted[start:end+1],
                'rawfilemd5': digest.encode(), 'substituteftype': b'1'}
    return plain, attrs, reply


def test_complete_chunked_decryption(tmp_path, monkeypatch):
    plain, attrs, reply = fixture()
    calls = []
    def response(req, deadline):
        calls.append(int(req['rangestart']))
        return reply(req, deadline)
    monkeypatch.setattr(cdn, '_request', response)
    path, verification = cdn._transfer(attrs, 'a'*64, 'image', {}, tmp_path, time.monotonic()+10)
    assert path.read_bytes() == plain
    assert verification == 'message_original'
    assert calls == [0, 65536]


@pytest.mark.parametrize('failure', ['offset', 'total', 'hash', 'padding', 'oversize', 'empty'])
def test_reject_corrupt_or_inconsistent_chunks(tmp_path, monkeypatch, failure):
    _, attrs, reply = fixture()
    def response(req, deadline):
        result = reply(req, deadline)
        if int(req['rangestart']) > 0:
            if failure == 'offset': result['rangestart'] = b'0'
            if failure == 'total': result['totalsize'] = str(int(result['totalsize'])+16).encode()
            if failure == 'hash': result['rawfilemd5'] = b'0'*32
            if failure == 'padding': result['filedata'] = result['filedata'][:-16]+b'0'*16
            if failure == 'oversize': result['totalsize'] = str(128*1024*1024+16).encode()
            if failure == 'empty': result['filedata'] = b''
        return result
    monkeypatch.setattr(cdn, '_request', response)
    with pytest.raises(cdn.DownloadError):
        cdn._transfer(attrs, 'a'*64, 'image', {}, tmp_path, time.monotonic()+10)


def test_video_variant_requires_cdn_hash_and_known_substitution(tmp_path, monkeypatch):
    plain, attrs, reply = fixture(b'\0\0\0\x18ftypisom'+b'a'*90000)
    attrs['md5'] = '0'*32
    def response(req, deadline):
        result = reply(req, deadline)
        result['substituteftype'] = b'9'
        return result
    monkeypatch.setattr(cdn, '_request', response)
    path, verification = cdn._transfer(attrs, 'a'*64, 'video', {}, tmp_path, time.monotonic()+10)
    assert path.read_bytes() == plain
    assert verification == 'cdn_variant'
    monkeypatch.setattr(cdn, '_request', reply)
    with pytest.raises(cdn.DownloadError, match='integrity_failed'):
        cdn._transfer(attrs, 'a'*64, 'video', {}, tmp_path, time.monotonic()+10)


@pytest.mark.parametrize('payload', [b'\x00', cdn.pack_fields({'a':b'1'})*2,
                                     b'\0\0\0\xffbad', cdn.pack_fields({'a':b'1'})+b'junk'])
def test_protocol_parser_rejects_malformed_payload(payload):
    with pytest.raises(cdn.DownloadError, match='invalid_response'):
        cdn.unpack_fields(payload)


@pytest.mark.parametrize('failure', ['404', 'network'])
def test_cdn_route_falls_back_and_reuses_verified_endpoint(monkeypatch, failure):
    primary = 'https://c2c.cdn.weixin.qq.com/download'
    alternate = 'https://novac2c.cdn.weixin.qq.com/download'
    monkeypatch.setattr(cdn, '_preferred_endpoint', primary, raising=False)
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, request.data))
            if request.full_url == primary:
                if failure == 'network':
                    raise urllib.error.URLError('route unavailable')
                raise urllib.error.HTTPError(primary, 404, 'missing route', {}, io.BytesIO())
            return io.BytesIO(cdn.pack_fields({'retcode': b'0'}))
    monkeypatch.setattr(cdn.urllib.request, 'build_opener', lambda *args: Opener())
    request = {'rangestart': b'0'}
    assert cdn._request(request, time.monotonic()+10)['retcode'] == b'0'
    assert cdn._request(request, time.monotonic()+10)['retcode'] == b'0'
    assert [url for url, _ in calls] == [primary, alternate, alternate]
    assert len({body for _, body in calls}) == 1


@pytest.mark.parametrize('code', [403, 404])
def test_cdn_route_failure_is_bounded_and_redacted(monkeypatch, code):
    primary = 'https://c2c.cdn.weixin.qq.com/download'
    monkeypatch.setattr(cdn, '_preferred_endpoint', primary, raising=False)
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, code, 'SECRET', {}, io.BytesIO())
    monkeypatch.setattr(cdn.urllib.request, 'build_opener', lambda *args: Opener())
    with pytest.raises(cdn.DownloadError, match='^cdn_rejected$'):
        cdn._request({}, time.monotonic()+10)
    assert len(calls) == (2 if code == 404 else 1)


@pytest.mark.parametrize('network_error', [urllib.error.URLError('SECRET_TOKEN'), http.client.IncompleteRead(b'SECRET_TOKEN')])
def test_transient_network_retries_same_range_and_redacts_failure(monkeypatch, network_error):
    calls = []
    class Response(io.BytesIO):
        pass
    class Opener:
        def open(self, request, timeout):
            calls.append(request.data)
            if len(calls) < 3:
                raise network_error
            return Response(cdn.pack_fields({'retcode': b'0'}))
    monkeypatch.setattr(cdn.urllib.request, 'build_opener', lambda *a: Opener())
    monkeypatch.setattr(cdn.time, 'sleep', lambda _: None)
    assert cdn._request({'rangestart':b'65536'}, time.monotonic()+10)['retcode'] == b'0'
    assert len(calls) == 3 and len(set(calls)) == 1
    class Failed:
        def open(self, *args, **kwargs): raise network_error
    monkeypatch.setattr(cdn.urllib.request, 'build_opener', lambda *a: Failed())
    with pytest.raises(cdn.DownloadError, match='^network_unavailable$'):
        cdn._request({}, time.monotonic()+10)


def test_redirect_rejected_without_following(monkeypatch):
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 302, 'redirect', {}, None)
    monkeypatch.setattr(cdn.urllib.request, 'build_opener', lambda *a: Opener())
    with pytest.raises(cdn.DownloadError, match='cdn_rejected'):
        cdn._request({}, time.monotonic()+10)
    assert calls == ['https://c2c.cdn.weixin.qq.com/download']
    assert cdn._NoRedirect().redirect_request(None) is None


@pytest.mark.parametrize('http_status', [200, 400, 404])
@pytest.mark.parametrize('second_code', [b'0', b'-5103059', b'-7001'])
def test_explicit_retry_checks_alternate_once_without_guessing_expiry(monkeypatch, http_status, second_code):
    calls = []
    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, request.data))
            fields = ({'retcode': b'-7001', 'isretry': b'1', 'existflag': b'0'}
                      if len(calls) == 1 else {'retcode': second_code})
            if second_code == b'-7001':
                fields['isretry'] = b'1'
            payload = io.BytesIO(cdn.pack_fields(fields))
            if http_status != 200 and len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, http_status, 'error', {}, payload)
            return payload
    monkeypatch.setattr(cdn.urllib.request, 'build_opener', lambda *a: Opener())
    monkeypatch.setattr(cdn, '_preferred_endpoint', cdn._ENDPOINTS[0])
    if second_code == b'0':
        assert cdn._request({'rangestart':b'123'}, time.monotonic()+10)['retcode'] == b'0'
    else:
        expected = 'cdn_expired' if second_code == b'-5103059' else 'cdn_rejected'
        with pytest.raises(cdn.DownloadError, match='^'+expected+'$'):
            cdn._request({'rangestart':b'123'}, time.monotonic()+10)
    assert [url for url, _ in calls] == list(cdn._ENDPOINTS)
    assert calls[0][1] == calls[1][1]


def test_verified_cache_needs_no_session_and_busy_rejects(tmp_path, monkeypatch):
    cfg = {'project_root':str(tmp_path), 'account':'a'}
    data = b'cached';digest = hashlib.sha256(data).hexdigest()
    p = retain_download(tmp_path/'data/media-cache', 'a', 'uid', 'image', 'png', data,
                        verified_sha256=digest, verification='message_original')
    assert cdn.download_media(cfg, 'uid', 'image') == p
    with cdn._slots:
        with pytest.raises(cdn.DownloadError, match='download_busy'):
            cdn.download_media(cfg, 'other', 'image')


def test_failure_keeps_old_thumbnail_and_removes_temporary_files(tmp_path, monkeypatch):
    from noteford import cdn_session
    cfg = {'project_root':str(tmp_path), 'account':'a', 'index_path':str(tmp_path/'index'),
           'wechat_root':str(tmp_path/'wechat')}
    cache = tmp_path/'data/media-cache';cache.mkdir(parents=True)
    thumb = cache/'thumb.png';thumb.write_bytes(b'old thumbnail')
    monkeypatch.setattr(cdn, '_target', lambda *a: ({}, 'a'*64))
    monkeypatch.setattr(cdn_session, 'acquire_session', lambda *a: {})
    def fail(*args):
        (args[4]/'plain').write_bytes(b'partial')
        raise cdn.DownloadError('integrity_failed')
    monkeypatch.setattr(cdn, '_transfer', fail)
    with pytest.raises(cdn.DownloadError, match='integrity_failed'):
        cdn.download_media(cfg, 'uid', 'image')
    assert thumb.read_bytes() == b'old thumbnail'
    assert list(cache.iterdir()) == [thumb]
    assert cdn._slots.acquire(blocking=False)
    cdn._slots.release()


def test_session_account_mismatch_prevents_memory_packet_scan(tmp_path, monkeypatch):
    from noteford import cdn_session as session
    path = tmp_path/'all_users/config/global_config.crc';path.parent.mkdir(parents=True)
    path.write_bytes(b's'*32)
    name = b'wrong'
    monkeypatch.setattr(session, 'read_target', lambda _: {'mmkv_key_latest_login_username':bytes([len(name)])+name})
    monkeypatch.setattr(session, 'list_weixin_processes', lambda: pytest.fail('must not scan another account'))
    with pytest.raises(ValueError, match='account_mismatch'):
        session.acquire_session(tmp_path, 'wxid_target_1234')


def test_service_preserves_thumbnail_on_download_failure_and_skips_ready(monkeypatch):
    from types import SimpleNamespace
    from noteford import service
    monkeypatch.setattr(service.sys, 'platform', 'win32')
    from noteford.media import MediaArtifact
    api = service.LocalApi.__new__(service.LocalApi)
    recorded = []
    api.automatic_media = SimpleNamespace(record_manual_result=lambda uid, media: recorded.append((uid, dict(media))))
    monkeypatch.setattr(service, 'mark_manual', lambda *args: None)
    api.config = {'index_path':'index'};api.account = 'a';api.media_cache_root = Path('cache')
    api.repository = SimpleNamespace(message_by_uid=lambda _: {
        'message': {'uid':'uid', 'type':{'kind':'image'}, 'timestamp':0,
                    'media':{'md5':'id'}, 'source':{'table':'Msg_x'}},
        'conversation':{'username':'chat'}})
    monkeypatch.setattr(service, 'selection_status', lambda *a: {'state':'following'})
    artifact = MediaArtifact('ready_thumbnail', 'image', 'thumb.png', 'id', 1, False)
    resolver = SimpleNamespace(export_image=lambda *a: artifact)
    api._media_resolver = lambda: resolver
    calls = []
    def fail(*args):
        calls.append(args)
        raise cdn.DownloadError('network_unavailable')
    monkeypatch.setattr(service, 'download_media', fail)
    result = api.recheck_media('uid')['messages'][0]['media']
    assert result['path'] == 'thumb.png'
    assert result['status'] == 'ready_thumbnail'
    assert result['download_error'] == 'network_unavailable'
    artifact.status = 'ready'
    assert 'download_error' not in api.recheck_media('uid')['messages'][0]['media']
    assert len(calls) == 1
    assert [media['status'] for _, media in recorded] == ['ready_thumbnail', 'ready']
    monkeypatch.setattr(service, 'selection_status', lambda *a: {'state':'not_following'})
    with pytest.raises(ValueError, match='conversation_not_followed'):
        api.recheck_media('uid')
    assert len(calls) == 1


def test_session_missing_or_changed_identity_never_persists_credentials(tmp_path, monkeypatch):
    from noteford import cdn_session
    cfg = {'project_root':str(tmp_path), 'account':'a', 'index_path':str(tmp_path/'index'),
           'wechat_root':str(tmp_path/'wechat')}
    monkeypatch.setattr(cdn, '_target', lambda *a: ({}, 'a'*64))
    def fail(*args): raise ValueError('SECRET_TOKEN')
    monkeypatch.setattr(cdn_session, 'acquire_session', fail)
    with pytest.raises(cdn.DownloadError, match='^session_unavailable$'):
        cdn.download_media(cfg, 'uid', 'image')
    assert list(tmp_path.rglob('*')) == [tmp_path/'data', tmp_path/'data/media-cache']

@pytest.mark.parametrize('key_length', [57, 64, 73])
@pytest.mark.parametrize('has_sourceflag', [True, False])
def test_cdn_session_accepts_current_variable_length_credentials(tmp_path, monkeypatch, key_length, has_sourceflag):
    from noteford import cdn_session as session
    from noteford.cdn_download import pack_fields
    path = tmp_path/'all_users/config/global_config.crc'; path.parent.mkdir(parents=True); path.write_bytes(b's'*32)
    name=b'wxid_target'
    monkeypatch.setattr(session,'read_target',lambda _: {'mmkv_key_latest_login_username':bytes([len(name)])+name,'mmkv_key_latest_login_uin':b'\x01'})
    fields={'ver':b'1','weixinnum':b'1','authkey':b'a'*key_length,'rsavalue':b'r'*128,'seq':b'1','sourceflag':b'1','fileid':b'f'*194}
    if not has_sourceflag:
        fields.pop('sourceflag')
    packet=pack_fields(fields)
    class Reader:
        def __init__(self,*args): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def private_regions(self,**kwargs): return [(4096,len(packet))]
        def read(self,*args): return packet
    monkeypatch.setattr(session,'list_weixin_processes',lambda:[{'pid':1,'cmdline':[]}])
    monkeypatch.setattr(session,'ProcessReader',Reader)
    assert session.acquire_session(tmp_path,'wxid_target_1234')['authkey']==fields['authkey']


def test_missing_hd_reference_uses_verified_standard_image(tmp_path):
    import sqlite3
    db=tmp_path/'index.db'
    with sqlite3.connect(db) as con:
        con.execute('create table messages(account,uid,local_type,message_content)')
        con.execute('insert into messages values(?,?,?,?)',('a','u',3,'<msg><img aeskey="'+ 'ab'*16 +'" cdnmidimgurl="'+ 'cd'*32 +'" length="123" md5="'+ 'ef'*16 +'" /></msg>'))
    attrs,ref=cdn._target(db,'a','u','image')
    assert ref=='cd'*32 and attrs['hdlength']=='123'
    assert attrs['md5']=='ef'*16


def test_server_expired_file_is_terminal_without_route_retry(monkeypatch):
    calls=[]
    class Opener:
        def open(self,req,**kwargs):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url,400,'error',{},io.BytesIO(cdn.pack_fields({'retcode':b'-5103059','existflag':b'0','isretry':b'0'})))
    monkeypatch.setattr(cdn.urllib.request,'build_opener',lambda *a:Opener())
    with pytest.raises(cdn.DownloadError,match='cdn_expired'):
        cdn._request({},time.monotonic()+10)
    assert len(calls)==1


@pytest.mark.parametrize('http_status', [200, 400, 404])
def test_expiry_code_is_consistent_across_http_envelopes(monkeypatch, http_status):
    payload = cdn.pack_fields({'retcode': b'-5103059', 'retmsg': b'PRIVATE_ROUTING_DATA'})
    class Reply(io.BytesIO):
        pass
    class Opener:
        def open(self, req, **kwargs):
            if http_status == 200:
                return Reply(payload)
            raise urllib.error.HTTPError(req.full_url, http_status, 'error', {}, io.BytesIO(payload))
    monkeypatch.setattr(cdn.urllib.request, 'build_opener', lambda *a: Opener())
    with pytest.raises(cdn.DownloadError, match='^cdn_expired$'):
        cdn._request({}, time.monotonic()+10)


@pytest.mark.parametrize('fields', [
    {'retcode': b'-1', 'existflag': b'0', 'isretry': b'0'},
    {'retmsg': b'fetch origin data not found!'},
    {'retcode': b'-5103059', 'existflag': b'1'},
    {'retcode': b'-5103059', 'isretry': b'1'},
    {'retcode': b'-102'},  # Certificate expiry is not media expiry.
])
def test_unproven_or_contradictory_errors_are_not_media_expiry(fields):
    assert cdn._response_error(fields) == 'cdn_rejected'

@pytest.mark.parametrize('hevc,has_hd', [(False,False),(True,False),(True,True)])
def test_standard_image_requests_original_container_and_verifies_hash(tmp_path, monkeypatch, hevc, has_hd):
    plain,attrs,reply=fixture((b'wxgf' if hevc else b'\xff\xd8\xff')+b'x'*90000)
    attrs['cdnmidimgurl']='a'*64
    if hevc: attrs['hevc_mid_size']=str(len(plain))
    if has_hd: attrs['cdnbigimgurl']='a'*64
    def response(req,deadline):
        assert req['filetype']==(b'1' if has_hd else b'2')
        assert req['downpicformat']==req['support_format']==(b'2' if hevc and not has_hd else b'0')
        return reply(req,deadline)
    monkeypatch.setattr(cdn,'_request',response)
    path,verification=cdn._transfer(attrs,'a'*64,'image',{},tmp_path,time.monotonic()+10)
    assert path.read_bytes()==plain and verification=='message_original'
    attrs['md5']='0'*32
    with pytest.raises(cdn.DownloadError,match='integrity_failed'):
        cdn._transfer(attrs,'a'*64,'image',{},tmp_path,time.monotonic()+10)

@pytest.mark.parametrize('size_delta', [0,1])
@pytest.mark.parametrize('format_hint', [True, False])
def test_hevc_standard_image_uses_hevc_size_not_jpeg_size(tmp_path, monkeypatch, size_delta, format_hint):
    plain,attrs,reply=fixture(b'wxgf'+b'x'*81652)
    attrs.update(cdnmidimgurl='a'*64,length='589987',hdlength='589987',hevc_mid_size=str(len(plain)+size_delta))
    def response(req,deadline):
        result=reply(req,deadline)
        if format_hint:
            result.update(rsppicformat=b'2',substituteftype=b'12')
        return result
    monkeypatch.setattr(cdn,'_request',response)
    if size_delta:
        with pytest.raises(cdn.DownloadError,match='integrity_failed'):
            cdn._transfer(attrs,'a'*64,'image',{},tmp_path,time.monotonic()+10)
    else:
        path,verification=cdn._transfer(attrs,'a'*64,'image',{},tmp_path,time.monotonic()+10)
        assert path.read_bytes()==plain and verification=='message_original'


def test_download_progress_tracks_actual_chunks_and_scope(tmp_path, monkeypatch):
    from noteford.download_progress import track, snapshot
    _, attrs, reply = fixture()
    request_id = 'download-test-123456'
    seen = []
    def response(req, deadline):
        seen.append(snapshot(tmp_path, 'account', request_id))
        return reply(req, deadline)
    monkeypatch.setattr(cdn, '_request', response)
    with track(tmp_path, 'account', request_id):
        cdn._transfer(attrs, 'a'*64, 'image', {}, tmp_path, time.monotonic()+10)
        value = snapshot(tmp_path, 'account', request_id)
        assert value['received'] == value['total'] == 90016
        assert snapshot(tmp_path, 'other-account', request_id)['received'] == 0
        assert snapshot(tmp_path / 'other', 'account', request_id)['received'] == 0
    assert seen[0]['received'] == 0
    assert seen[1]['received'] == 65536
    assert seen[1]['total'] == 90016
    assert snapshot(tmp_path, 'account', request_id)['state'] == 'finished'
