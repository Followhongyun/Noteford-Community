from contextlib import closing
import hashlib
import json
import sqlite3
import struct
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from noteford import mac_cdn as cdn, mac_attachments as records, mac_completion as completion
from noteford.cdn_download import DownloadError
from noteford.mac_image_decode import decrypt_dat, partitions
from noteford.image_crypto import V2_MAGIC


def encrypt(data, key, mode):
    pad=16-len(data)%16
    cipher=Cipher(algorithms.AES(key),mode).encryptor()
    return cipher.update(data+bytes([pad])*pad)+cipher.finalize()


def wire(fields):
    def var(n):
        out=bytearray()
        while n>=128:out.append((n&127)|128);n>>=7
        return bytes(out)+bytes([n])
    out=b''
    for k,v in fields:
        out+=var(k*8+(2 if isinstance(v,bytes)else 0))
        out+=var(len(v))+v if isinstance(v,bytes)else var(v)
    return out


@pytest.mark.parametrize('uin,signed_wire', [(12345678, False), (0x81234567, False), (0x81234567, True)])
def test_account_bound_cdn_cache_and_malformed_protobuf(tmp_path,monkeypatch,uin,signed_wire):
    from noteford import mac_media
    account='wxid_fixture_1234';key=hashlib.md5((str(uin)+'wxid_fixture').encode()).hexdigest()[:16].encode()
    monkeypatch.setattr(cdn,'live_root',lambda c:tmp_path/account)
    monkeypatch.setattr(mac_media,'local_image_keys',lambda *a:(key,uin&255))
    record=wire([(1,1),(8,b'auth-fixture'*8)])
    branch=wire([(2,uin | 0xFFFFFFFF00000000 if signed_wire else uin),(7,record)])
    cache=tmp_path/'cache';cache.write_bytes(wire([(1,wire([(7,branch)]))]))
    config={'account':account,'project_root':str(tmp_path)}
    session=cdn.session(config,'1'*32,cache_path=cache)
    assert session['weixinnum']==str(uin).encode() and len(session['rsavalue'])==128
    # A malformed wider integer must not be accepted just because its low
    # 32 bits would match the account. Only int32 sign extension is valid.
    for invalid_uin in (uin | (1 << 32), uin | (1 << 64)):
        cache.write_bytes(wire([(1,wire([(7,wire([(2,invalid_uin),(7,record)]))]))]))
        with pytest.raises(DownloadError,match='account_mismatch'):cdn.session(config,'1'*32,cache_path=cache)
    cache.write_bytes(wire([(1,wire([(7,branch)]))]))
    monkeypatch.setattr(mac_media,'local_image_keys',lambda *a:(b'wrong-account!!!',0))
    with pytest.raises(DownloadError,match='account_mismatch'):cdn.session(config,'1'*32,cache_path=cache)
    for invalid in (b'\x0a\xff',b'\x00',b'\x0a\x05a',b'\x0f'):
        with pytest.raises(ValueError):cdn.protobuf(invalid)


def test_imported_archive_never_opens_live_credentials(tmp_path,monkeypatch):
    monkeypatch.setattr(cdn,'managed_archive',lambda:tmp_path/'managed')
    monkeypatch.setattr(cdn,'_storage',lambda _:pytest.fail('source opened'))
    with pytest.raises(DownloadError,match='offline_archive'):
        cdn.live_root({'wechat_root':str(tmp_path/'import'),'account':'fixture'})


def test_emoji_https_mirror_encryption_integrity_and_cancellation(tmp_path,monkeypatch):
    config={'project_root':str(tmp_path),'account':'fixture'}
    monkeypatch.setattr(cdn,'live_root',lambda _:tmp_path/'readonly')
    image=b'GIF89a'+b'synthetic image'*10; key=b'0123456789abcdef'
    encrypted=encrypt(image,key,modes.CBC(key));digest=hashlib.md5(image).hexdigest()
    attrs={'md5':digest,'cdnurl':'http://vweixinf.tc.qq.com/item','encrypturl':'https://wxapp.tc.qq.com/encrypted','aeskey':key.hex()}
    assert cdn.secure_url(attrs['cdnurl'])=='https://wxapp.tc.qq.com/item'
    calls=[]
    def fetch(url,**kw):
        calls.append(url)
        return b'corrupt'if url==attrs['cdnurl']else encrypted
    monkeypatch.setattr(cdn,'fetch',fetch)
    path=cdn.download_emoji(config,'message',attrs)
    assert path.read_bytes()==image and len(calls)==2
    cdn.download_emoji(config,'message',attrs)
    assert len(calls)==2
    with pytest.raises(DownloadError,match='download_cancelled'):
        cdn.download_emoji(config,'cancelled',attrs,should_continue=lambda:False)
    assert not list((tmp_path/'data/media-cache').glob('.mac-emoji-*'))
    for bad in ('http://127.0.0.1/x','https://wxapp.tc.qq.com.evil.test/x','https://secret@wxapp.tc.qq.com/x','https://wxapp.tc.qq.com:444/x','file:///tmp/x'):
        with pytest.raises(DownloadError):cdn.secure_url(bad)
    assert cdn._NoRedirect().redirect_request(None,None,None,None,None,None)is None


def test_tp_preview_is_not_mislabeled_as_original(tmp_path,monkeypatch):
    monkeypatch.setattr(cdn,'live_root',lambda _:tmp_path/'source')
    config={'project_root':str(tmp_path),'account':'fixture'}
    data=b'\xff\xd8\xff'+b'fixture jpeg';key=b'1'*16
    def fetch_file(url,path,**kwargs):
        path.write_bytes(encrypt(data,key,modes.ECB()));return path
    monkeypatch.setattr(cdn,'fetch_to_file',fetch_file)
    attrs={'tpurl':'https://wwfile.work.weixin.qq.com/cgi-bin/download?f=x','tplength':str(len(data)),
           'aeskey':key.hex(),'tpauthkey':'12','md5':'f'*32}
    path,original=cdn.download_tp(config,'image',attrs)
    assert path.read_bytes()==data and not original
    from noteford.downloaded_media import retained_download
    root=tmp_path/'data/media-cache'
    assert retained_download(root,'fixture','image','image')is None
    assert retained_download(root,'fixture','image:preview','image')is not None
    attrs['md5']=hashlib.md5(data).hexdigest()
    assert cdn.download_tp(config,'image',attrs)[1]


def test_legacy_xor_and_v1_decode_without_mutating_input():
    plain=b'\x89PNG\r\n\x1a\n'+b'synthetic picture'*4
    raw=bytes(v^0x32 for v in plain)
    assert decrypt_dat(raw)==plain and raw!=plain
    first=encrypt(plain[:16],b'cfcd208495d565ef',modes.ECB())
    v1=b'\x07\x08V1\x08\x07'+struct.pack('<LL',16,4)+b'\0'+first+plain[16:-4]+bytes(v^0x66 for v in plain[-4:])
    assert decrypt_dat(v1)==plain
    with pytest.raises(ValueError):decrypt_dat(b'not an image')


def test_record_payload_excludes_credentials_and_paths_are_account_bound(tmp_path,monkeypatch):
    from noteford.mac_decode import _xml
    xml=_xml('<msg><appmsg><recorditem><![CDATA[<recordinfo><datalist><dataitem datatype="8"><sourcename>Fixture</sourcename><datatitle>sample.pdf</datatitle><cdndatakey>secret-key</cdndatakey><cdndataurl>private-reference</cdndataurl></dataitem></datalist></recordinfo>]]></recorditem></appmsg></msg>')
    config={'project_root':str(tmp_path),'account':'one'}
    monkeypatch.setattr(records,'message_xml',lambda *a:xml)
    item={'uid':'parent','timestamp':1}
    entry=records.entries(xml,'parent')[0]
    path=records.cache_file(config,entry['uid'],b'%PDF-1.7 fixture','pdf')
    payload=records.present(config,item)
    assert payload[0]['media']['path']==str(path)
    assert 'secret-key'not in json.dumps(payload)and 'private-reference'not in json.dumps(payload)
    assert records.cached_file({**config,'account':'two'},entry['uid'])is None
    path.write_bytes(b'corrupt')
    assert records.present(config,item)[0]['media']['status']=='missing'


def test_record_cdn_checks_each_chunk_and_full_md5(tmp_path,monkeypatch):
    config={'project_root':str(tmp_path),'account':'one'}
    data=b'%PDF-fixture'+b'attachment'*9000;key=b'2'*16
    encrypted=encrypt(data,key,modes.ECB())
    fields={'cdndataurl':'ab'*40,'cdndatakey':key.hex(),'fullmd5':hashlib.md5(data).hexdigest(),
            'datasize':str(len(data)),'filetype':'5'}
    monkeypatch.setattr(records,'session',lambda *a:{})
    calls=[]
    def request(req,deadline):
        start,end=int(req['rangestart']),int(req['rangeend']);calls.append(start)
        return {'totalsize':str(len(encrypted)).encode(),'rangestart':str(start).encode(),'filedata':encrypted[start:end+1]}
    monkeypatch.setattr(records,'_request',request)
    path,verification=records.transfer(config,fields,tmp_path)
    assert path.read_bytes()==data and verification=='message_original'and len(calls)==2
    with pytest.raises(DownloadError,match='integrity_failed'):
        records.transfer(config,{**fields,'fullmd5':'0'*32},tmp_path)
    with pytest.raises(DownloadError,match='download_cancelled'):
        records.transfer(config,fields,tmp_path,lambda:False)


def test_login_queue_all_chats_restart_new_message_and_opt_out(tmp_path,monkeypatch):
    from test_automatic_media import setup
    core,calls,index=setup(tmp_path)
    core.config.update(account='acct',project_root=str(tmp_path))
    monkeypatch.setattr(completion,'live_root',lambda _:tmp_path/'source')
    worker=completion.MacAutomaticMedia(core);stop=threading.Event()
    while worker.run_one(stop,now=100):pass
    assert set(calls)=={'image','video','early','late','paused','other'}
    restarted=completion.MacAutomaticMedia(core)
    assert not restarted.run_one(stop,now=1000)
    with closing(sqlite3.connect(index))as db,db:
        db.execute("UPDATE messages SET row_hash='v2'WHERE uid='other'")
    assert restarted.run_one(stop,now=1000)and calls[-1]=='other'
    core.config['auto_media_loading']=False
    assert not restarted.run_one(stop,now=2000)
    core.config['auto_media_loading']=True
    stop.set();assert not restarted.run_one(stop,now=2000)


def test_transient_session_error_recovers_without_manual_click(tmp_path,monkeypatch):
    from test_automatic_media import setup
    core,calls,index=setup(tmp_path)
    monkeypatch.setattr(completion,'live_root',lambda _:tmp_path/'source')
    worker=completion.MacAutomaticMedia(core);stop=threading.Event()
    original=core.recheck_media
    core.recheck_media=lambda *a,**k:{'messages':[{'media':{'status':'missing','download_error':'session_unavailable'}}]}
    for stamp in (100,401,702,1003):assert worker.run_one(stop,now=stamp)
    core.recheck_media=original
    assert worker.run_one(stop,now=1304)
    assert calls


def test_bundled_wxgf_animation_preserves_color_alpha_and_frames(tmp_path):
    import shutil,subprocess
    from noteford.mac_image_decode import decode_wxgf,binaries
    if not all(p.is_file()for p in binaries())or not shutil.which('ffmpeg'):
        pytest.skip('native Mac decoder build and synthetic fixture encoder required')
    chunks=[]
    for color in ('red','blue'):
        for shade,pixel in (('white','gray'),(color,'yuv420p')):
            result=subprocess.run([shutil.which('ffmpeg'),'-v','error','-f','lavfi','-i',f'color=c={shade}:s=96x64:r=10',
                '-frames:v','1','-pix_fmt',pixel,'-c:v','libx265','-x265-params','pools=1:frame-threads=1:log-level=error',
                '-f','hevc','pipe:1'],capture_output=True,timeout=20)
            assert result.returncode==0
            chunks.append(result.stdout)
    data=b'wxgf'+bytes([16])+b'\0'*11+b''.join(struct.pack('>I',len(c))+c for c in chunks)
    assert len(partitions(data))==4
    destination=tmp_path/'animated.png'
    path,status=decode_wxgf(data,destination)
    assert status=='ready'
    png=path.read_bytes();pos=png.index(b'acTL')
    assert struct.unpack('>I',png[pos+4:pos+8])[0]==2
    assert png[25]==6 and struct.unpack('>II',png[16:24])==(96,64)
    assert decode_wxgf(data,destination)==(destination,'ready')
    assert decode_wxgf(b'wxgf-invalid',destination)[0]is None
    assert destination.read_bytes()==png
    # The file-backed acquisition path also accepts a container beyond the old
    # size ceiling; sparse trailing padding avoids allocating a huge test input.
    from noteford.mac_image_decode import decode_wxgf_file
    source=tmp_path/'large.wxgf';source.write_bytes(data)
    with source.open('ab') as handle:handle.truncate(129*1024*1024)
    streamed,state=decode_wxgf_file(source,tmp_path/'streamed.png')
    assert state=='ready'
    result=streamed.read_bytes();position=result.index(b'acTL')
    assert struct.unpack('>I',result[position+4:position+8])[0]==2
    assert result[25]==6 and struct.unpack('>II',result[16:24])==(96,64)


def test_mac_cdn_jpeg_rendition_is_preview_and_hash_failure_stays_rejected(tmp_path,monkeypatch):
    key=b'2'*16;data=b'\xff\xd8\xff'+b'reencoded jpeg fixture'+b'\xff\xd9'
    encrypted=encrypt(data,key,modes.ECB())
    attrs={'aeskey':key.hex(),'md5':'f'*32,'cdnmidimgurl':'ab'*40,'hdlength':'999'}
    response={'totalsize':str(len(encrypted)).encode(),'rangestart':b'0','filedata':encrypted,
              'substituteftype':b'2','rsppicformat':b'1'}
    monkeypatch.setattr(cdn,'_request',lambda *a:response)
    checked=[]
    monkeypatch.setattr(cdn,'_validate_jpeg',lambda path:checked.append(path.read_bytes()))
    p,verification=cdn._transfer_image(attrs,attrs['cdnmidimgurl'],'image',{},tmp_path,1)
    assert p.read_bytes()==data and checked==[data] and verification=='cdn_preview'
    response['rawfilemd5']=b'0'*32
    with pytest.raises(DownloadError,match='integrity_failed'):
        cdn._transfer_image(attrs,attrs['cdnmidimgurl'],'image',{},tmp_path,1)
    response.pop('rawfilemd5');response['rsppicformat']=b'0'
    with pytest.raises(DownloadError,match='integrity_failed'):
        cdn._transfer_image(attrs,attrs['cdnmidimgurl'],'image',{},tmp_path,1)


def test_mac_rendition_cache_never_replaces_original(tmp_path,monkeypatch):
    from noteford.downloaded_media import retained_download
    data=b'\xff\xd8\xfffixture';config={'project_root':str(tmp_path),'account':'one','index_path':str(tmp_path/'db')}
    monkeypatch.setattr(cdn,'live_root',lambda _:tmp_path/'source')
    monkeypatch.setattr(cdn,'_target',lambda *a:({'aeskey':'2'*32},'ab'*40))
    monkeypatch.setattr(cdn,'session',lambda *a:{})
    def transfer(*args):
        path=args[4]/'plain';path.write_bytes(data);return path,'cdn_preview'
    monkeypatch.setattr(cdn,'_transfer_image',transfer)
    path=cdn.download_media(config,'message','image')
    assert path.read_bytes()==data
    root=tmp_path/'data/media-cache'
    assert retained_download(root,'one','message','image')is None
    assert retained_download(root,'one','message:preview','image')is not None


def test_mac_jpeg_validation_rejects_truncation_and_dimensions_before_decoder(tmp_path,monkeypatch):
    path=tmp_path/'image.jpg'
    monkeypatch.setattr(cdn.subprocess,'run',lambda *a,**k:pytest.fail('decoder must not run'))
    for data in (b'\xff\xd8\xffbroken',b'\xff\xd8\xff\xc0\x00\x08\x08\xff\xff\xff\xff\x01\xff\xd9'):
        path.write_bytes(data)
        with pytest.raises(DownloadError):cdn._validate_jpeg(path)


def test_tp_fetch_joins_server_limited_ranges_even_when_status_is_200(monkeypatch):
    import io
    chunks=[b'first',b'second'];calls=[]
    class Response:
        status=200
        def __init__(self,index):
            start=0 if index==0 else 5;end=4 if index==0 else 10
            self.headers={'Content-Range':f'bytes {start}-{end}/11','ETag':'fixture'}
            self.stream=io.BytesIO(chunks[index])
        def read1(self,n):return self.stream.read(n)
        def __enter__(self):return self
        def __exit__(self,*a):return False
    def open_request(request,**kwargs):
        index=len(calls);calls.append(request.get_header('Range'));return Response(index)
    monkeypatch.setattr(cdn.urllib.request,'build_opener',lambda *a:SimpleNamespace(open=open_request))
    assert cdn.fetch('https://wwfile.work.weixin.qq.com/file',authkey='12')==b'firstsecond'
    assert calls==[None,'bytes=5-']
    calls.clear();chunks[0]=b'shortened'
    with pytest.raises(DownloadError,match='invalid_response'):
        cdn.fetch('https://wwfile.work.weixin.qq.com/file',authkey='12')
    calls.clear();chunks[0]=b'first'
    with pytest.raises(DownloadError,match='too_large'):
        cdn.fetch('https://wwfile.work.weixin.qq.com/file',authkey='12',limit=10)


@pytest.mark.parametrize('failure',['cdn_rejected','cdn_not_found','integrity_failed','invalid_response'])
def test_recoverable_cdn_failure_is_retried_after_backoff(tmp_path,monkeypatch,failure):
    from test_automatic_media import setup
    core,calls,index=setup(tmp_path)
    monkeypatch.setattr(completion,'live_root',lambda _:tmp_path/'source')
    worker=completion.MacAutomaticMedia(core)
    row=dict(next(worker._rows()))
    worker._record(row,failure,3,500)
    # Other messages may be processed, but this message respects backoff.
    assert worker._candidate(499)[0]['uid']!=row['uid']
    assert worker._candidate(501)[0]['uid']==row['uid']


def test_tp_failure_falls_back_and_local_arrival_wins(tmp_path,monkeypatch):
    import xml.etree.ElementTree as ET
    from noteford.media import MediaArtifact
    item={'uid':'one','timestamp':1,'source':{'table':'Msg_one'},'type':{'kind':'image'}}
    root=ET.fromstring('<msg><img tpurl="https://wxapp.tc.qq.com/test" cdnbigimgurl="abc"/></msg>')
    monkeypatch.setattr(completion,'message_xml',lambda *a:root)
    monkeypatch.setattr(completion,'enrich',lambda *a:None)
    attempts=[]
    monkeypatch.setattr(completion,'download_tp',lambda *a,**k:(_ for _ in ()).throw(DownloadError('cdn_expired')))
    monkeypatch.setattr(completion,'download_media',lambda *a,**k:attempts.append('cdn'))
    def local(*args):
        return MediaArtifact('ready' if attempts else 'missing','image',str(tmp_path/'original') if attempts else None,None,10,False)
    core=SimpleNamespace(config={},repository=SimpleNamespace(message_by_uid=lambda uid:{'message':item,'conversation':{}}),
        _media_resolver=lambda:SimpleNamespace(export_image=local),media_cache_root=tmp_path)
    result=completion.recheck(core,'one',should_continue=lambda:True)
    assert attempts==['cdn'] and result['messages'][0]['media']['status']=='ready'
    # If all remote paths fail, a fresh local check still recovers WeChat's file.
    attempts.clear()
    def remote(*a,**k):
        attempts.append('cdn');raise DownloadError('cdn_expired')
    monkeypatch.setattr(completion,'download_media',remote)
    result=completion.recheck(core,'one',should_continue=lambda:True)
    assert result['messages'][0]['media']['status']=='ready'
    assert not result['messages'][0]['media'].get('download_error')
