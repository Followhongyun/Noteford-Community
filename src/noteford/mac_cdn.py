"""Mac-only, account-bound CDN downloads. Official WeChat files are read-only."""
from __future__ import annotations

import hashlib
import http.client
import re
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from .cdn_download import DownloadError, CHUNK, _NoRedirect, _target, _transfer, _request
from .downloaded_media import retain_download, retained_download
from .image_crypto import image_extension
from .libraries import account_username
from .mac_live_sync import _storage, managed_archive

# WeChat CDN's public protocol key; this is not a user credential.
_RSA = int('BFEDFFB5EA28509F9C89ED83FA7FDDA8881435D444E984D53A98AD8E9410F1145EDD537890E10456190B22E6E5006455EFC6C12E41FDA985F38FBBC7213ECB810E3053D4B8D74FFBC70B4600ABD728202322AFCE1406046631261BD5EE3D44721082FEAB74340D73645DC0D02A293B962B9D47E4A64100BD7524DE00D9D3B5C1', 16)
_HEX = re.compile(r'[a-fA-F0-9]{32}')


def protobuf(data: bytes):
    """Small bounded wire reader; unknown fields remain opaque."""
    if len(data) > 1024 * 1024:
        raise ValueError('invalid_cache')
    def varint(pos):
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(data):
                raise ValueError('invalid_cache')
            byte = data[pos]; pos += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value, pos
        raise ValueError('invalid_cache')
    pos, fields, count = 0, {}, 0
    while pos < len(data):
        tag, pos = varint(pos)
        number, wire = tag >> 3, tag & 7
        count += 1
        if not 0 < number < 65536 or count > 10000:
            raise ValueError('invalid_cache')
        if wire == 0:
            value, pos = varint(pos)
        else:
            if wire == 2:
                length, pos = varint(pos)
            elif wire in (1, 5):
                length = 8 if wire == 1 else 4
            else:
                raise ValueError('invalid_cache')
            if pos + length > len(data):
                raise ValueError('invalid_cache')
            value = data[pos:pos+length]; pos += length
        fields.setdefault(number, []).append(value)
    return fields


def live_root(config):
    if Path(config['wechat_root']).resolve() != managed_archive().resolve():
        raise DownloadError('offline_archive')
    try:
        return _storage(config['account']).parent
    except (OSError, ValueError):
        raise DownloadError('session_unavailable') from None


def session(config, aeskey, *, cache_path=None):
    root = live_root(config)
    from .mac_media import local_image_keys
    keys = local_image_keys(root, Path(config['project_root'])/'secrets/image_keys.json')
    # Verify UIN against real account ciphertext, not the four-character folder
    # suffix alone. Credentials from a second logged-in account must not be used.
    if keys is None:
        raise DownloadError('session_unavailable')
    cache = cache_path or (root.parent.parent/'app_data/net/cdncomm/cdninfo_new.cache')
    try:
        if cache.is_symlink() or cache.stat().st_size > 1024 * 1024:
            raise ValueError()
        outer = protobuf(protobuf(cache.read_bytes())[1][0])
        for raw in outer.get(7, []) + outer.get(8, []):
            branch = protobuf(raw)
            uin = branch[2][0]
            # Protobuf int32 values with the high bit set use a sign-extended
            # ten-byte varint. Restore the uint32 account ID before deriving
            # the key, but reject arbitrary oversized integers rather than
            # truncating them into another account's ID.
            if isinstance(uin, int) and 0xFFFFFFFF80000000 <= uin <= 0xFFFFFFFFFFFFFFFF:
                uin &= 0xFFFFFFFF
            if not isinstance(uin, int) or not 0 < uin < 2**32:
                continue
            expected = hashlib.md5((str(uin) + account_username(config['account'])).encode()).hexdigest()[:16].encode()
            if keys[0] != expected or keys[1] != uin & 255:
                continue
            for raw_entry in branch.get(7, []):
                entry = protobuf(raw_entry)
                key = entry.get(8, [b''])[0]
                if entry.get(1) != [1] or not isinstance(key, bytes) or not 32 <= len(key) <= 4096:
                    continue
                result = {k:v.encode() for k,v in {
                    'ver':'1','weixinnum':str(uin),'seq':'1','clientversion':'1',
                    'clientostype':'Mac','nettype':'1','acceptdupack':'1','rsaver':'1',
                    'wxchattype':'0','lastretcode':'0','cli-quic-flag':'0','wxmsgflag':'',
                    'wxautostart':'1','largesvideo':'0','sourceflag':'0'}.items()}
                result.update(authkey=key, rsavalue=rsa.RSAPublicNumbers(65537, _RSA).public_key().encrypt(
                    bytes.fromhex(aeskey), padding.PKCS1v15()))
                return result
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        raise DownloadError('session_unavailable') from None
    raise DownloadError('account_mismatch')


def _allowed(callback):
    if callback is not None and not callback():
        raise DownloadError('download_cancelled')


def retain(config, uid, kind, data, verification, work, should_continue=None):
    extension = image_extension(data) if kind == 'image' else 'mp4'
    if extension == 'wxgf':
        from .mac_image_decode import decode_wxgf
        converted, _ = decode_wxgf(data, work/'decoded.png')
        if converted is None:
            raise DownloadError('unsupported_download_format')
        data, extension = converted.read_bytes(), 'png'
    if extension not in {'png','jpg','gif','webp','mp4'} or (kind == 'video' and data[4:8] != b'ftyp'):
        raise DownloadError('unsupported_download_format')
    _allowed(should_continue)
    return retain_download(Path(config['project_root'])/'data/media-cache', config['account'], uid,
                           kind, extension, data, verified_sha256=hashlib.sha256(data).hexdigest(),
                           verification=verification)


def retain_file(config, uid, kind, path, verification, work, should_continue=None):
    from .downloaded_media import retain_download_file
    with path.open('rb') as handle:
        header = handle.read(16)
    extension = image_extension(header) if kind == 'image' else 'mp4'
    if extension == 'wxgf':
        from .mac_image_decode import decode_wxgf_file
        path, _ = decode_wxgf_file(path, work/'decoded.png')
        if path is None:
            raise DownloadError('unsupported_download_format')
        extension = 'png'
    if extension not in {'png','jpg','gif','webp','mp4'} or (kind == 'video' and header[4:8] != b'ftyp'):
        raise DownloadError('unsupported_download_format')
    _allowed(should_continue)
    return retain_download_file(Path(config['project_root'])/'data/media-cache', config['account'],
                                uid, kind, extension, path, verification=verification,
                                should_continue=should_continue)


def _validate_jpeg(path):
    """Bound dimensions before asking macOS ImageIO to decode the rendition."""
    import mmap
    with path.open('rb') as handle,mmap.mmap(handle.fileno(),0,access=mmap.ACCESS_READ) as data:
        pos=2;dimensions=None
        end=data.rfind(b'\xff\xd9')
        if data[:2]!=b'\xff\xd8' or end<2 or len(data)-end-2>4096:
            raise DownloadError('integrity_failed')
        while pos+4<=len(data):
            if data[pos]!=255:break
            while pos<len(data)and data[pos]==255:pos+=1
            marker=data[pos];pos+=1
            if marker in {0xda,0xd9}:break
            if marker in {0x01,*range(0xd0,0xd8)}:continue
            length=int.from_bytes(data[pos:pos+2],'big')
            if length<2 or pos+length>len(data):raise DownloadError('integrity_failed')
            if marker in {0xc0,0xc1,0xc2} and length>=8:
                height,width=struct.unpack('>HH',data[pos+3:pos+7]);dimensions=(width,height)
                if min(width,height)<=0 or max(width,height)>16384 or width*height>24_000_000:
                    raise DownloadError('too_large')
            pos+=length
        if dimensions is None:raise DownloadError('integrity_failed')
    output=path.with_suffix('.checked.png')
    try:
        result=subprocess.run(['/usr/bin/sips','-s','format','png',str(path),'--out',str(output)],
                              capture_output=True,timeout=30)
        if result.returncode or not output.is_file():raise DownloadError('integrity_failed')
        with output.open('rb')as f:header=f.read(24)
        if header[:8]!=b'\x89PNG\r\n\x1a\n' or struct.unpack('>II',header[16:24])!=dimensions:
            raise DownloadError('integrity_failed')
    except (OSError,subprocess.TimeoutExpired,struct.error):
        raise DownloadError('integrity_failed')from None
    finally:output.unlink(missing_ok=True)

def _transfer_image(attrs, reference, kind, session, work: Path, deadline, should_continue=None):
    # Only request-header fields from the account-bound template; never replay payloads.
    allowed = ('ver weixinnum province city isp rulekey expectrulekey seq clientversion '
               'clientostype authkey nettype acceptdupack rsaver rsavalue filetype wxchattype '
               'taskid totalsize rawtotalsize fileid lastretcode cli-quic-flag wxmsgflag '
               'wxautostart downloadbehavor downpicformat support_format offset largesvideo sourceflag').split()
    req = {k: v for k, v in session.items() if k in allowed}
    standard_image = (kind == 'image' and not attrs.get('cdnbigimgurl')
                      and reference == attrs.get('cdnmidimgurl'))
    # Standard images have a distinct CDN object type. Request the original
    # HEVC container when advertised, avoiding server JPEG transcoding that
    # changes both the message hash and size. All integrity checks still apply.
    hevc_size = attrs.get('hevc_mid_size', '')
    image_format = b'2' if (standard_image and hevc_size.isdigit()
                            and 0 < int(hevc_size)) else b'0'
    req.update(fileid=reference.encode(),
               filetype=(b'2' if standard_image else b'1') if kind == 'image' else b'4',
               downpicformat=image_format, support_format=image_format, taskid=b'noteford_download')
    cipher = Cipher(algorithms.AES(bytes.fromhex(attrs['aeskey'])), modes.ECB()).decryptor()
    start = 0
    total = None
    first = None
    tail = b''
    path = work / 'plain'
    with path.open('wb') as output:
        while total is None or start < total:
            if should_continue is not None and not should_continue():
                raise DownloadError('download_cancelled')
            end = 65535 if total is None else min(total-1, start+CHUNK-1)
            req.update(offset=str(start).encode(), rangestart=str(start).encode(), rangeend=str(end).encode(),
                       seq=str(int(req.get('seq', b'0'))+1).encode())
            fields = _request(req, deadline if deadline is not None else time.monotonic()+30)
            try:
                current_total = int(fields['totalsize'])
                begin = int(fields['rangestart'])
                data = fields['filedata']
            except (KeyError, ValueError):
                raise DownloadError('invalid_response') from None
            from .download_policy import check_size
            check_size(current_total, encrypted=True)
            if current_total <= 0 or current_total % 16:
                raise DownloadError('invalid_response')
            from .download_policy import ensure_space
            ensure_space(work, current_total - start)
            if (begin != start or not data or len(data) % 16 or len(data) > end-start+1
                    or start+len(data) > current_total):
                raise DownloadError('invalid_response')
            if first is None:
                first, total = fields, current_total
            elif (current_total != total or any(fields.get(k) != first.get(k)
                      for k in ('rawfilemd5', 'substituteftype', 'videoformat', 'rsppicformat'))):
                raise DownloadError('invalid_response')
            block = tail + cipher.update(data)
            output.write(block[:-16])
            tail = block[-16:]
            start += len(data)
            from .download_progress import report
            report(start, total)
        tail += cipher.finalize()
        padding = tail[-1]
        if not 1 <= padding <= 16 or tail[-padding:] != bytes([padding])*padding:
            raise DownloadError('integrity_failed')
        output.write(tail[:-padding])
    with path.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'md5').hexdigest()
        handle.seek(0)
        downloaded_format = image_extension(handle.read(16)) if kind == 'image' else None
    expected = {attrs.get(k, '').lower() for k in ('md5', 'newmd5', 'rawmd5', 'originsourcemd5')}
    cdn_hash = first.get('rawfilemd5', b'').decode('ascii', errors='replace').lower()
    variant = digest not in expected
    # Tencent can replace a standard image with a JPEG rendition and omit its
    # MD5. It is authenticated by TLS and the message AES key, but is not the
    # original bytes: retain it only under the preview identity.
    rendition = (variant and not cdn_hash and standard_image and
                 first.get('substituteftype') == b'2' and
                 first.get('rsppicformat') == b'1' and downloaded_format == 'jpg')
    if (cdn_hash and digest != cdn_hash) or (variant and not rendition and
            (digest != cdn_hash or first.get('substituteftype') not in {b'1', b'2'})):
        raise DownloadError('integrity_failed')
    if rendition:
        _validate_jpeg(path)
        return path, 'cdn_preview'
    try:
        size_field = 'hdlength' if kind == 'image' else 'length'
        # Some successful CDN responses omit rsppicformat. The hash-verified
        # bytes identify the actual container; a missing hint must not make us
        # compare HEVC bytes with the alternate JPEG representation's length.
        if standard_image and image_format == b'2' and downloaded_format == 'wxgf':
            size_field = 'hevc_mid_size'
        expected_bytes = int(attrs.get(size_field, '0'))
    except ValueError:
        expected_bytes = 0
    if not variant and expected_bytes and path.stat().st_size != expected_bytes:
        raise DownloadError('integrity_failed')
    return path, 'cdn_variant' if variant else 'message_original'



def download_media(config, uid, kind, *, should_continue=None):
    _allowed(should_continue)
    live_root(config)
    root = Path(config['project_root'])/'data/media-cache'
    old = retained_download(root, config['account'], uid, kind)
    if old:
        return old[0]
    root.mkdir(parents=True, exist_ok=True)
    from .download_policy import ensure_space
    ensure_space(root)
    attrs, reference = _target(Path(config['index_path']).resolve(), config['account'], uid, kind)
    from .download_policy import check_size
    size = attrs.get('hdlength') or attrs.get('length') or '0'
    if str(size).isdigit():check_size(int(size))
    credentials = session(config, attrs['aeskey'])
    with tempfile.TemporaryDirectory(prefix='.mac-download-', dir=root) as folder:
        work = Path(folder)
        deadline = None  # Each chunk has its own stall deadline; no whole-file timeout.
        transfer = _transfer_image if kind == 'image' else _transfer
        try:
            path, verification = transfer(attrs, reference, kind, credentials, work, deadline, should_continue)
        except DownloadError as exc:
            middle = attrs.get('cdnmidimgurl', '')
            if kind != 'image' or not re.fullmatch('[a-fA-F0-9]{32,4096}', middle) or middle == reference or str(exc) not in {'cdn_expired','cdn_rejected','cdn_not_found','integrity_failed','invalid_response'}:
                raise
            attrs = {**attrs, 'cdnbigimgurl':'', 'hdlength':attrs.get('length','0')}
            path, verification = transfer(attrs, middle, kind, credentials, work, deadline, should_continue)
        _allowed(should_continue)
        preview = verification == 'cdn_preview'
        return retain_file(config, uid+':preview' if preview else uid, kind, path,
                      'cdn_variant' if preview else verification, work, should_continue)


# Fixed Tencent hosts only, with TLS validation. Do not send credentials to
# redirect targets or resolve a message-provided hostname outside this list.
_HOSTS = {'wxapp.tc.qq.com', 'emoji.qpic.cn', 'mmbiz.qpic.cn', 'mmbiz.qlogo.cn',
          'wwfile.work.weixin.qq.com'}


def secure_url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        host = parsed.hostname
        if host == 'vweixinf.tc.qq.com':
            host = 'wxapp.tc.qq.com'  # Same Tencent object route, valid TLS host.
        if (parsed.scheme not in {'http','https'} or host not in _HOSTS or
                parsed.username or parsed.password or parsed.port not in (None,80,443) or
                len(value) > 16384 or any(ord(c) < 32 for c in value)):
            raise ValueError()
        return urllib.parse.urlunsplit(('https', host, parsed.path, parsed.query, ''))
    except ValueError:
        raise DownloadError('missing_download_reference') from None


def fetch_to_file(value, destination, *, should_continue=None, limit=None, authkey=None):
    """Receive trusted-host HTTPS data in bounded chunks, including ranged WeCom replies."""
    url = secure_url(value)
    from .https_context import download_context
    from .download_policy import check_size, ensure_space
    from .download_progress import report
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                         urllib.request.HTTPSHandler(context=download_context()))
    headers = {'User-Agent':'MicroMessenger Client'}
    if authkey is not None:
        if urllib.parse.urlsplit(url).hostname != 'wwfile.work.weixin.qq.com' or not re.fullmatch('[a-fA-F0-9]{2,8192}', authkey):
            raise DownloadError('missing_download_reference')
        headers['authkey'] = authkey
    received=0;total=None;etag=None;failures=0
    try:
        with destination.open('wb') as output:
            while True:
                _allowed(should_continue)
                if received:headers['Range']=f'bytes={received}-'
                request = urllib.request.Request(url, headers=headers)
                try:
                    with opener.open(request, timeout=10) as response:
                        value=response.headers.get('Content-Range');segment=None
                        length=response.headers.get('Content-Length','')
                        if value:
                            match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',value)
                            if not match:raise DownloadError('invalid_response')
                            begin,end,count=map(int,match.groups())
                            if begin!=received or not begin<=end<count or (total is not None and count!=total):
                                raise DownloadError('invalid_response')
                            total=count;segment=end+1
                        elif received or response.status==206:
                            raise DownloadError('invalid_response')
                        elif length.isdigit():
                            total=int(length);segment=total
                        tag=response.headers.get('ETag')
                        if etag is not None and tag!=etag:raise DownloadError('invalid_response')
                        etag=tag
                        if total is not None:
                            if limit is not None and total>limit:raise DownloadError('too_large')
                            check_size(total, encrypted=authkey is not None)
                        ensure_space(destination.parent, (total or received)-received)
                        deadline=time.monotonic()+30
                        while True:
                            _allowed(should_continue)
                            if time.monotonic()>deadline:raise DownloadError('download_timeout')
                            block=response.read1(65536)
                            if not block:break
                            if segment is not None and received+len(block)>segment:raise DownloadError('invalid_response')
                            if limit is not None and received+len(block)>limit:raise DownloadError('too_large')
                            check_size(total or received+len(block), encrypted=authkey is not None)
                            ensure_space(destination.parent, (total-received) if total else len(block))
                            try:output.write(block)
                            except OSError:raise DownloadError('storage_unavailable') from None
                            received+=len(block)
                            report(received,total)
                            deadline=time.monotonic()+30
                            failures=0
                        if segment is not None and received!=segment:raise http.client.IncompleteRead(b'')
                        if total is None or received==total:return destination
                except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as exc:
                    if isinstance(exc, urllib.error.HTTPError):
                        code=exc.code;exc.close()
                        if code not in {408,429,500,502,503,504}:
                            raise DownloadError('cdn_not_found' if code==404 else 'cdn_rejected') from None
                    # Resume a partial object only with a stable server validator.
                    failures+=1
                    if failures>=3 or (received and (etag is None or total is None)):
                        raise DownloadError('network_unavailable') from None
                    _allowed(should_continue)
                    time.sleep(.25*failures)
    except OSError:
        destination.unlink(missing_ok=True)
        raise DownloadError('storage_unavailable') from None
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def fetch(value, *, should_continue=None, limit=32*1024*1024, authkey=None):
    # Bounded bytes API for legacy sticker callers. Media originals use fetch_to_file.
    with tempfile.TemporaryDirectory(prefix='noteford-https-') as folder:
        path=fetch_to_file(value,Path(folder)/'payload',should_continue=should_continue,limit=limit,authkey=authkey)
        return path.read_bytes()


def decrypt_file(source, destination, key, should_continue=None):
    """AES-ECB with one-block tail buffering; validate padding before publication."""
    from .download_policy import ensure_space
    if not _HEX.fullmatch(key or '') or not source.stat().st_size or source.stat().st_size%16:
        raise DownloadError('integrity_failed')
    ensure_space(destination.parent,source.stat().st_size)
    cipher=Cipher(algorithms.AES(bytes.fromhex(key)),modes.ECB()).decryptor()
    tail=b''
    with source.open('rb') as encrypted, destination.open('wb') as plain:
        while block:=encrypted.read(512*1024):
            _allowed(should_continue)
            ensure_space(destination.parent,len(block))
            data=tail+cipher.update(block)
            plain.write(data[:-16]);tail=data[-16:]
        tail+=cipher.finalize();pad=tail[-1]
        if not 1<=pad<=16 or tail[-pad:]!=bytes([pad])*pad:raise DownloadError('integrity_failed')
        plain.write(tail[:-pad])
    return destination


def decrypt_emoji(data, key):
    if not _HEX.fullmatch(key) or not data or len(data) % 16:
        raise DownloadError('integrity_failed')
    secret = bytes.fromhex(key)
    decryptor = Cipher(algorithms.AES(secret), modes.CBC(secret)).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    pad = plain[-1]
    if not 1 <= pad <= 16 or plain[-pad:] != bytes([pad])*pad:
        raise DownloadError('integrity_failed')
    return plain[:-pad]


def download_emoji(config, uid, attrs, *, should_continue=None):
    previous_check = should_continue
    def permitted():
        return config.get('parse_stickers', True) is not False and (previous_check is None or previous_check())
    should_continue = permitted
    _allowed(should_continue)
    source = live_root(config)
    root = Path(config['project_root'])/'data/media-cache'
    old = retained_download(root, config['account'], uid, 'image')
    if old:
        return old[0]
    digest = attrs.get('md5', '').lower()
    if not _HEX.fullmatch(digest):
        raise DownloadError('missing_download_reference')
    root.mkdir(parents=True, exist_ok=True)
    error = 'missing_download_reference'
    with tempfile.TemporaryDirectory(prefix='.mac-emoji-', dir=root) as folder:
        work = Path(folder)
        from .mac_emoticon import read_local
        data = read_local(config, source, digest, should_continue=should_continue)
        if data is not None:
            _allowed(should_continue)
            return retain(config, uid, 'image', data, 'message_original', work, should_continue)
        for field, expected, encrypted in [('cdnurl',digest,False), ('encrypturl',digest,True),
                ('externurl',attrs.get('externmd5','').lower(),False)]:
            if not attrs.get(field) or not _HEX.fullmatch(expected):
                continue
            try:
                data = fetch(attrs[field], should_continue=should_continue)
                if encrypted:
                    data = decrypt_emoji(data, attrs.get('aeskey', ''))
                if hashlib.md5(data).hexdigest() != expected:
                    raise DownloadError('integrity_failed')
                _allowed(should_continue)
                return retain(config, uid, 'image', data, 'message_original', work, should_continue)
            except DownloadError as exc:
                if str(exc) in {'download_cancelled', 'auto_size_limit'}:
                    raise
                error = str(exc)
    raise DownloadError(error)


def download_tp(config, uid, attrs, *, should_continue=None):
    """WeCom HTTPS original; decrypted renditions never masquerade as originals."""
    from .download_policy import check_size
    from .downloaded_media import file_digest
    live_root(config)
    root=Path(config['project_root'])/'data/media-cache';root.mkdir(parents=True,exist_ok=True)
    error='missing_download_reference'
    with tempfile.TemporaryDirectory(prefix='.tp-',dir=root)as folder:
        work=Path(folder)
        for field,length in [('tphdurl','tphdlength'),('tpurl','tplength'),('tpthumburl','tpthumblength')]:
            if not attrs.get(field):continue
            try:
                if str(attrs.get(length,'')).isdigit():check_size(int(attrs[length]))
                encrypted=fetch_to_file(attrs[field],work/'encrypted',authkey=attrs.get('tpauthkey'),should_continue=should_continue)
                key=attrs.get('tpthumbaeskey')if field=='tpthumburl'else attrs.get('aeskey')
                path=decrypt_file(encrypted,work/'plain',key,should_continue)
                encrypted.unlink()
                with path.open('rb')as handle:extension=image_extension(handle.read(16))
                if str(path.stat().st_size)!=attrs.get(length)or extension not in {'jpg','png','gif','webp'}:
                    raise DownloadError('integrity_failed')
                original=file_digest(path,'md5',should_continue)==attrs.get('md5','').lower()
                if field=='tphdurl'and not original:raise DownloadError('integrity_failed')
                _allowed(should_continue)
                return retain_file(config,uid if original else uid+':preview','image',path,
                                   'message_original'if original else 'cdn_variant',work,should_continue),original
            except DownloadError as exc:
                if str(exc) in {'download_cancelled','auto_size_limit','insufficient_space'}:raise
                error=str(exc)
    raise DownloadError(error)
