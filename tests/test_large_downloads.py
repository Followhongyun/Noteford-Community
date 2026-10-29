"""Large synthetic objects: real decryption, disk retention, no live WeChat data."""
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from noteford import cdn_download, mac_cdn, mac_attachments
from noteford.download_policy import automatic
from noteford.downloaded_media import file_digest, retained_download

SIZE = 129 * 1024 * 1024
KEY = b'k'*16


class Object:
    def __init__(self, prefix):
        self.prefix = prefix.ljust(16,b'x')
        digest=hashlib.md5(self.prefix)
        remaining=SIZE-16
        while remaining:
            count=min(remaining,512*1024);digest.update(b'x'*count);remaining-=count
        self.md5=digest.hexdigest()
        self.clock=1000
        self.requests=0
    def encrypted(self,start,count):
        end=min(start+count,SIZE+16)
        data=b'x'*(min(end,SIZE)-start) if start<SIZE else b''
        if start==0:data=self.prefix+data[16:]
        if end>SIZE:data+=bytes([16])*(end-max(start,SIZE))
        cipher=Cipher(algorithms.AES(KEY),modes.ECB()).encryptor()
        return cipher.update(data)+cipher.finalize()
    def request(self,req,deadline):
        assert deadline>self.clock
        self.clock+=5;self.requests+=1
        start,end=int(req['rangestart']),int(req['rangeend'])
        return {'totalsize':str(SIZE+16).encode(),'rangestart':str(start).encode(),
                'filedata':self.encrypted(start,end-start+1),'rawfilemd5':self.md5.encode()}


def disallow_whole_file_reads(monkeypatch):
    original=Path.read_bytes
    def bounded(path):
        assert path.stat().st_size<1024*1024,'whole-file allocation during large download'
        return original(path)
    monkeypatch.setattr(Path,'read_bytes',bounded)


@pytest.mark.parametrize('kind',['image','video','file'])
def test_large_cdn_object_reaches_verified_cache_without_whole_file_allocation(tmp_path,monkeypatch,kind):
    obj=Object(b'\x89PNG\r\n\x1a\n' if kind=='image' else b'\0\0\0\x18ftypisom' if kind=='video' else b'%PDF-1.7')
    config={'project_root':str(tmp_path),'account':'fixture','index_path':str(tmp_path/'index')}
    monkeypatch.setattr(mac_cdn,'live_root',lambda _:tmp_path)
    monkeypatch.setattr(mac_attachments,'live_root',lambda _:tmp_path)
    monkeypatch.setattr(mac_cdn,'session',lambda *a:{})
    monkeypatch.setattr(mac_attachments,'session',lambda *a:{})
    for module in (cdn_download,mac_cdn,mac_attachments):
        monkeypatch.setattr(module,'_request',obj.request)
    monkeypatch.setattr(cdn_download.time,'monotonic',lambda:obj.clock)
    attrs={'aeskey':KEY.hex(),'md5':obj.md5,'length':str(SIZE),'hdlength':str(SIZE)}
    monkeypatch.setattr(mac_cdn,'_target',lambda *a:(attrs,'a'*64))
    disallow_whole_file_reads(monkeypatch)
    with automatic({'auto_media_max_mb':0}):
        if kind=='file':
            path=mac_attachments.complete(config,{'kind':kind,'uid':'large','fields':{
                'cdndataurl':'a'*64,'cdndatakey':KEY.hex(),'datasize':str(SIZE),
                'fullmd5':obj.md5,'filetype':'5','datafmt':'pdf'}})
        else:path=mac_cdn.download_media(config,'large',kind)
    assert path.stat().st_size==SIZE and file_digest(path,'md5')==obj.md5
    assert obj.requests>250 and obj.clock>1000+240 # virtual download exceeds old overall deadlines
    if kind=='file':assert mac_attachments.cached_file(config,'large')==path
    else:assert retained_download(tmp_path/'data/media-cache','fixture','large',kind)[0]==path
    # Preferences still stop automatic transfers, while manual/all has no byte cap.
    with automatic({'auto_media_max_mb':50}),pytest.raises(cdn_download.DownloadError,match='auto_size_limit'):
        cdn_download._transfer(attrs,'a'*64,'video',{},tmp_path,None)


def test_large_https_original_streams_and_decrypts_without_old_128mb_or_total_timeout(tmp_path,monkeypatch):
    obj=Object(b'\xff\xd8\xff')
    class Response:
        status=200
        headers={'Content-Length':str(SIZE+16),'ETag':'fixture'}
        def __init__(self):self.offset=0
        def __enter__(self):return self
        def __exit__(self,*a):return False
        def read1(self,count):
            obj.clock+=5
            data=obj.encrypted(self.offset,count) if self.offset<SIZE+16 else b''
            self.offset+=len(data);return data
    monkeypatch.setattr(mac_cdn.urllib.request,'build_opener',lambda *a:SimpleNamespace(open=lambda *a,**k:Response()))
    monkeypatch.setattr(mac_cdn,'live_root',lambda _:tmp_path)
    monkeypatch.setattr(mac_cdn.time,'monotonic',lambda:obj.clock)
    disallow_whole_file_reads(monkeypatch)
    config={'project_root':str(tmp_path),'account':'fixture'}
    path,original=mac_cdn.download_tp(config,'large',{'tpurl':'https://wwfile.work.weixin.qq.com/file',
        'tplength':str(SIZE),'tpauthkey':'12','aeskey':KEY.hex(),'md5':obj.md5})
    assert original and path.stat().st_size==SIZE and file_digest(path,'md5')==obj.md5
    assert obj.clock>1000+3600


def test_not_enough_space_fails_before_download_and_does_not_publish(tmp_path,monkeypatch):
    monkeypatch.setattr('shutil.disk_usage',lambda _:SimpleNamespace(free=1024))
    monkeypatch.setattr(mac_cdn,'live_root',lambda _:tmp_path)
    monkeypatch.setattr(mac_cdn,'_target',lambda *a:pytest.fail('download started without space'))
    with pytest.raises(cdn_download.DownloadError,match='insufficient_space'):
        mac_cdn.download_media({'project_root':str(tmp_path),'account':'a'},'u','video')
    assert not list(tmp_path.rglob('manifest.json'))


def test_old_size_failures_reenter_queue_once_but_expiry_does_not(tmp_path):
    from test_automatic_media import setup
    from noteford.automatic_media import AutomaticMedia
    core,calls,index=setup(tmp_path)
    worker=AutomaticMedia(core)
    row=dict(next(worker._rows()))
    worker._record(row,'too_large',3,999999)
    with worker._state() as db:
        db.execute("DELETE FROM migrations WHERE name='unlimited-download-size'")
    restarted=AutomaticMedia(core)
    assert restarted._candidate(100)[0]['uid']==row['uid']
    # New format/dimension rejection must not be re-enqueued on every restart.
    restarted._record(row,'too_large',3,999999)
    assert AutomaticMedia(core)._candidate(100)[0]['uid']!=row['uid']


def test_https_interrupted_body_resumes_only_same_object(tmp_path,monkeypatch):
    import io
    calls=[]
    class Response:
        status=200
        def __init__(self,offset):
            self.headers={'Content-Range':f'bytes {offset}-10/11','ETag':'stable'}
            # First response ends prematurely after three bytes.
            self.stream=io.BytesIO(b'hel' if offset==0 else b'lo world')
        def read1(self,count):return self.stream.read(count)
        def __enter__(self):return self
        def __exit__(self,*a):return False
    def open_request(request,**kwargs):
        value=request.get_header('Range');calls.append(value)
        return Response(3 if value else 0)
    monkeypatch.setattr(mac_cdn.urllib.request,'build_opener',lambda *a:SimpleNamespace(open=open_request))
    monkeypatch.setattr(mac_cdn.time,'sleep',lambda _:None)
    result=mac_cdn.fetch_to_file('https://wwfile.work.weixin.qq.com/file',tmp_path/'download',authkey='12')
    assert result.read_bytes()==b'hello world' and calls==[None,'bytes=3-']
