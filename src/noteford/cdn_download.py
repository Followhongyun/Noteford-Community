"""Bounded, explicit downloads from the fixed WeChat CDN; no UI automation."""
from __future__ import annotations

import hashlib
import http.client
import re
import sqlite3
import struct
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .decode import decode_text_blob, _safe_xml_root
from .downloaded_media import retained_download
from .image_crypto import image_extension
from .https_context import download_context

CHUNK = 512 * 1024
_slots = threading.BoundedSemaphore(1)
_ENDPOINTS = ('https://c2c.cdn.weixin.qq.com/download',
              'https://novac2c.cdn.weixin.qq.com/download')
_preferred_endpoint = _ENDPOINTS[0]


class DownloadError(ValueError):
    """Only a fixed, non-sensitive error code may reach callers."""


def unpack_fields(data: bytes, *, strict=True) -> dict[str, bytes]:
    fields = {}
    pos = 0
    try:
        while pos < len(data):
            if len(fields) >= 80 or pos + 4 > len(data):
                raise ValueError()
            length = struct.unpack_from('>I', data, pos)[0]
            pos += 4
            if not 1 <= length <= 40 or pos + length + 4 > len(data):
                raise ValueError()
            key = data[pos:pos+length]
            pos += length
            if not re.fullmatch(rb'[a-zA-Z0-9_-]+', key):
                raise ValueError()
            length = struct.unpack_from('>I', data, pos)[0]
            pos += 4
            name = key.decode('ascii')
            if length > CHUNK+4096 or pos + length > len(data) or name in fields:
                raise ValueError()
            fields[name] = data[pos:pos+length]
            pos += length
    except (ValueError, struct.error):
        if strict:
            raise DownloadError('invalid_response') from None
    return fields


def pack_fields(fields):
    return b''.join(struct.pack('>I', len(k))+k.encode()+struct.pack('>I', len(v))+v
                    for k, v in fields.items())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _response_error(fields):
    # ERR_SERVER_FILE_EXPIRED; checked against native image/video failures.
    # This describes the remote reference, never the availability of a local copy.
    # Protocol reference: wechat-tests/wechat-golang/mm_pb/base_common.proto.
    if fields.get('retcode') == b'-5103059':
        if fields.get('existflag', b'0') == b'0' and fields.get('isretry', b'0') == b'0':
            return 'cdn_expired'
    return 'cdn_rejected'


def _request(fields, deadline):
    global _preferred_endpoint
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                         urllib.request.HTTPSHandler(context=download_context()))
    endpoint = _preferred_endpoint
    tried_routes = {endpoint}
    for attempt in range(3):
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise DownloadError('download_timeout')
        request = urllib.request.Request(endpoint,
            data=pack_fields(fields), method='POST',
            headers={'Content-Type': 'application/octet-stream', 'User-Agent': 'MicroMessenger Client'})
        try:
            with opener.open(request, timeout=min(10, remaining)) as response:
                # Bound both bytes and elapsed time, including slow streaming peers.
                payload = bytearray()
                while True:
                    if time.monotonic() > deadline:
                        raise DownloadError('download_timeout')
                    part = response.read1(min(65536, CHUNK+8193-len(payload)))
                    if not part:
                        break
                    payload.extend(part)
                    if len(payload) > CHUNK+8192:
                        raise DownloadError('invalid_response')
                result = unpack_fields(bytes(payload))
                if result.get('retcode') != b'0':
                    if result.get('isretry') == b'1':
                        alternate = next((url for url in _ENDPOINTS if url not in tried_routes), None)
                        if alternate is not None:
                            endpoint = alternate
                            tried_routes.add(endpoint)
                            continue
                    raise DownloadError(_response_error(result))
                _preferred_endpoint = endpoint
                return result
        except urllib.error.HTTPError as exc:
            # CDN may return its binary error envelope with an HTTP error.
            # Inspect fixed flags only; response text can contain private routing data.
            error_fields = {}
            if exc.code in {400, 404}:
                try:
                    error_fields = unpack_fields(exc.read(8193))
                    unavailable = _response_error(error_fields) == 'cdn_expired'
                except (DownloadError, OSError):
                    unavailable = False
                if unavailable:
                    exc.close()
                    raise DownloadError('cdn_expired') from None
            retry = exc.code in {408, 429, 500, 502, 503, 504}
            exc.close()
            if exc.code == 404 or error_fields.get('isretry') == b'1':
                alternate = next((url for url in _ENDPOINTS if url not in tried_routes), None)
                if alternate is not None:
                    endpoint = alternate
                    tried_routes.add(endpoint)
                    continue
            if not retry:
                raise DownloadError('cdn_rejected') from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException):
            pass
        alternate = next((url for url in _ENDPOINTS if url not in tried_routes), None)
        if alternate is not None:
            endpoint = alternate
            tried_routes.add(endpoint)
        if attempt < 2:
            time.sleep(min(0.25 * (attempt+1), max(0, deadline-time.monotonic())))
    raise DownloadError('network_unavailable')


def _target(index: Path, account: str, uid: str, kind: str):
    with sqlite3.connect(index.as_uri()+'?mode=ro', uri=True) as con:
        row = con.execute('select local_type,message_content from messages where account=? and uid=?',
                          (account, uid)).fetchone()
    if row is None or row[0] != (3 if kind == 'image' else 43):
        raise DownloadError('missing_download_reference')
    text, error = decode_text_blob(row[1])
    root = _safe_xml_root(text[text.find('<'):]) if not error and '<' in text else None
    node = root.find('img' if kind == 'image' else 'videomsg') if root is not None else None
    attrs = dict(node.attrib) if node is not None else {}
    reference = attrs.get('cdnbigimgurl' if kind == 'image' else 'cdnvideourl', '')
    if kind == 'image' and not reference:
        # Some messages carry only the standard image, not a separate HD original.
        # Keep the same AES, message hash and size verification; never use a thumbnail.
        reference = attrs.get('cdnmidimgurl', '')
        attrs['hdlength'] = attrs.get('length', '0')
    if (not re.fullmatch(r'[0-9a-fA-F]{32,4096}', reference)
            or not re.fullmatch(r'[0-9a-fA-F]{32}', attrs.get('aeskey', ''))):
        raise DownloadError('missing_download_reference')
    return attrs, reference


def _transfer(attrs, reference, kind, session, work: Path, deadline, should_continue=None):
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
    if (cdn_hash and digest != cdn_hash) or (variant and
            (kind != 'video' or digest != cdn_hash or first.get('substituteftype') != b'9')):
        raise DownloadError('integrity_failed')
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


def download_media(config: dict, uid: str, kind: str, *, should_continue=None) -> Path:
    if kind not in {'image', 'video'}:
        raise DownloadError('unsupported_media_kind')
    if should_continue is not None and not should_continue():
        raise DownloadError('download_cancelled')
    root = Path(config['project_root']) / 'data/media-cache'
    account = config['account']
    existing = retained_download(root, account, uid, kind)
    if existing is not None:
        return existing[0]
    if not _slots.acquire(blocking=False):
        raise DownloadError('download_busy')
    try:
        root.mkdir(parents=True, exist_ok=True)
        from .download_policy import ensure_space
        ensure_space(root)
        attrs, reference = _target(Path(config['index_path']).resolve(), account, uid, kind)
        from .cdn_session import acquire_session
        from .winmem import UnsupportedPlatform
        deadline = None  # Each chunk has its own stall deadline; no whole-file timeout.
        try:
            session = acquire_session(Path(config['wechat_root']), account)
        except UnsupportedPlatform:
            raise DownloadError('unsupported_platform') from None
        except (OSError, ValueError, IndexError, KeyError, struct.error) as exc:
            raise DownloadError('account_mismatch' if str(exc) == 'account_mismatch'
                                else 'session_unavailable') from None
        with tempfile.TemporaryDirectory(prefix='.download-', dir=root) as folder:
            work = Path(folder)
            path, verification = _transfer(attrs, reference, kind, session, work, deadline,
                                          **({'should_continue':should_continue} if should_continue else {}))
            with path.open('rb') as handle:
                header = handle.read(16)
            extension = image_extension(header) if kind == 'image' else 'mp4'
            if kind == 'video' and (len(header) < 12 or header[4:8] != b'ftyp'):
                raise DownloadError('unsupported_download_format')
            if extension == 'wxgf':
                from .image_decode import decode_wxgf_file
                path, _ = decode_wxgf_file(path, work/'decoded.png')
                if path is None:
                    raise DownloadError('unsupported_download_format')
                extension = 'png'
            if extension not in ({'png', 'jpg', 'webp', 'gif'} if kind == 'image' else {'mp4'}):
                raise DownloadError('unsupported_download_format')
            from .downloaded_media import retain_download_file
            return retain_download_file(root, account, uid, kind, extension, path,
                                        verification=verification, should_continue=should_continue)
    except DownloadError:
        raise
    except OSError:
        raise DownloadError('storage_unavailable') from None
    finally:
        _slots.release()
