"""Bundled Mac image decoder; never invokes a system-installed decoder."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from .image_crypto import V2_MAGIC, decrypt_v2_bytes, image_extension

MAX_BYTES = 64 * 1024 * 1024
_slots = threading.BoundedSemaphore(1)


def decrypt_dat(data, keys=None):
    if image_extension(data) != 'bin':
        return data
    if data.startswith(V2_MAGIC):
        if not keys:
            raise ValueError('image_key_unavailable')
        return decrypt_v2_bytes(data, *keys)
    if data.startswith(b'\x07\x08V1\x08\x07'):
        # V1 shares the container layout but uses a fixed public legacy key.
        return decrypt_v2_bytes(V2_MAGIC+data[6:], b'cfcd208495d565ef', 0x66)
    for magic in (b'\xff\xd8\xff', b'\x89PNG\r\n\x1a\n', b'GIF8', b'RIFF'):
        if len(data) < len(magic):
            continue
        key = data[0] ^ magic[0]
        if bytes(byte ^ key for byte in data[:len(magic)]) == magic:
            plain = bytes(byte ^ key for byte in data)
            if plain.startswith(b'RIFF') and plain[8:12] != b'WEBP':
                continue
            return plain
    raise ValueError('unsupported_image_container')


def binaries():
    root = (Path(sys._MEIPASS)/'mac-native' if getattr(sys,'frozen',False)
            else Path(__file__).resolve().parents[2]/'build/mac-native')
    return root/'noteford-ffmpeg', root/'noteford-ffprobe'


def partitions(data):
    """Length-delimited Annex B partitions, with a single-stream legacy fallback."""
    matches = list(re.finditer(rb'\x00\x00\x00\x01\x40|(?<!\x00)\x00\x00\x01\x40', data))
    if not matches or not 4 <= matches[0].start() <= 4096:
        raise ValueError('unsupported_image_container')
    streams, end = [], 0
    for match in matches:
        pos = match.start()
        if pos < end:
            continue
        size = int.from_bytes(data[pos-4:pos], 'big')
        if 0 < size <= len(data)-pos:
            streams.append(data[pos:pos+size]); end = pos+size
        elif len(matches) == 1:
            streams.append(data[pos:])
        else:
            raise ValueError('invalid_image_partition')
        if len(streams) > 400:
            raise ValueError('image_dimensions_exceeded')
    return streams


def decode_wxgf(data, destination):
    if not data.startswith(b'wxgf') or len(data) > MAX_BYTES:
        return None, 'unsupported_image_container'
    digest = hashlib.sha256(data).hexdigest()
    stamp = destination.with_suffix('.png.sha256')
    try:
        if destination.is_file() and stamp.read_text() == digest and destination.read_bytes()[:8] == b'\x89PNG\r\n\x1a\n':
            return destination, 'ready'
    except OSError:
        pass
    ffmpeg, ffprobe = binaries()
    if not ffmpeg.is_file() or not ffprobe.is_file():
        return None, 'image_decoder_unavailable'
    if not _slots.acquire(timeout=2):
        return None, 'image_decoder_busy'
    try:
        chunks = partitions(data)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(destination.parent).free < 512*1024*1024:
            return None, 'insufficient_space'
        with tempfile.TemporaryDirectory(prefix='.wxgf-', dir=destination.parent) as folder:
            work = Path(folder)
            def probe(content):
                result = subprocess.run([str(ffprobe),'-v','error','-f','hevc','-show_frames',
                    '-show_entries','frame=width,height,pix_fmt','-of','json','pipe:0'],
                    input=content,capture_output=True,timeout=20)
                frames = json.loads(result.stdout).get('frames',[])
                if result.returncode or not frames:
                    raise ValueError('image_decode_failed')
                for frame in frames:
                    w,h = int(frame['width']),int(frame['height'])
                    if min(w,h)<=0 or max(w,h)>16384 or w*h>24_000_000:
                        raise ValueError('image_dimensions_exceeded')
                return frames
            first = probe(chunks[0])
            width,height = int(first[0]['width']),int(first[0]['height'])
            masked = first[0]['pix_fmt'].startswith('gray') and len(chunks)>1
            if masked and len(chunks)%2:
                raise ValueError('invalid_image_partition')
            color = b''.join(chunks[1::2] if masked else chunks)
            colors = probe(color)
            if any((int(f['width']),int(f['height']))!=(width,height) for f in colors):
                raise ValueError('image_dimensions_exceeded')
            if len(colors)>200 or width*height*len(colors)>120_000_000:
                raise ValueError('image_dimensions_exceeded')
            color_path=work/'color.hevc';color_path.write_bytes(color)
            args=[str(ffmpeg),'-v','error','-nostdin','-threads','2','-f','hevc','-r','10','-i',str(color_path)]
            if masked:
                mask=b''.join(chunks[::2]); masks=probe(mask)
                if len(masks)!=len(colors) or any(not f['pix_fmt'].startswith('gray') or
                    (int(f['width']),int(f['height']))!=(width,height) for f in masks):
                    raise ValueError('invalid_image_partition')
                mask_path=work/'mask.hevc';mask_path.write_bytes(mask)
                args+=['-f','hevc','-r','10','-i',str(mask_path),'-filter_complex',
                       '[0:v]format=rgb24[c];[c][1:v]alphamerge[out]','-map','[out]']
            output=work/'decoded.png'
            animated=len(colors)>1
            args+=['-frames:v',str(len(colors)),'-fs',str(MAX_BYTES),'-f',
                   'apng' if animated else 'image2','-c:v','apng' if animated else 'png']
            if animated: args+=['-plays','0']
            args+=[str(output)]
            result=subprocess.run(args,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL,timeout=45)
            if result.returncode or not output.is_file() or output.stat().st_size>MAX_BYTES:
                raise ValueError('image_decode_failed')
            png=output.read_bytes()
            if png[:8]!=b'\x89PNG\r\n\x1a\n' or len(png)<24 or struct.unpack('>II',png[16:24])!=(width,height):
                raise ValueError('image_decode_failed')
            output.chmod(0o600);output.replace(destination)
            stamp.write_text(digest)
            return destination,'ready'
    except subprocess.TimeoutExpired:
        return None,'image_decode_timeout'
    except (OSError,ValueError,KeyError,TypeError):
        return None,'image_decode_failed'
    finally:
        _slots.release()


def decode_wxgf_file(source, destination, *, decoder_binaries=None):
    """Decode a downloaded container through file-backed partitions, without a byte cap."""
    import mmap
    from .downloaded_media import file_digest
    from .download_policy import ensure_space
    with source.open('rb') as handle:
        if handle.read(4) != b'wxgf':return None,'unsupported_image_container'
    digest=file_digest(source)
    stamp=destination.with_suffix('.png.sha256')
    try:
        if destination.is_file() and stamp.read_text()==digest:
            with destination.open('rb')as handle:
                if handle.read(8)==b'\x89PNG\r\n\x1a\n':return destination,'ready'
    except OSError:pass
    ffmpeg,ffprobe=decoder_binaries or binaries()
    if not ffmpeg or not ffprobe or not Path(ffmpeg).is_file() or not Path(ffprobe).is_file():
        return None,'image_decoder_unavailable'
    if not _slots.acquire(timeout=2):return None,'image_decoder_busy'
    try:
        destination.parent.mkdir(parents=True,exist_ok=True)
        ensure_space(destination.parent,source.stat().st_size)
        with tempfile.TemporaryDirectory(prefix='.wxgf-file-',dir=destination.parent)as folder:
            work=Path(folder);parts=[]
            with source.open('rb')as raw,mmap.mmap(raw.fileno(),0,access=mmap.ACCESS_READ)as data:
                end=0
                for match in re.finditer(rb'\x00\x00\x00\x01\x40|(?<!\x00)\x00\x00\x01\x40',data):
                    pos=match.start()
                    if pos<end:continue
                    if not parts and not 4<=pos<=4096:raise ValueError('unsupported_image_container')
                    length=int.from_bytes(data[pos-4:pos],'big')
                    if not 0<length<=len(data)-pos:
                        if parts:raise ValueError('invalid_image_partition')
                        length=len(data)-pos
                    parts.append((pos,length));end=pos+length
                    if len(parts)>400:raise ValueError('image_dimensions_exceeded')
                if not parts:raise ValueError('unsupported_image_container')
                def write_parts(path, ranges):
                    with path.open('wb')as output:
                        for pos,length in ranges:
                            while length:
                                count=min(length,1024*1024)
                                output.write(data[pos:pos+count]);pos+=count;length-=count
                def probe(path):
                    result=subprocess.run([str(ffprobe),'-v','error','-f','hevc','-i',str(path),
                        '-show_frames','-show_entries','frame=width,height,pix_fmt','-of','json'],
                        capture_output=True,timeout=30)
                    frames=json.loads(result.stdout).get('frames',[])
                    if result.returncode or not frames:raise ValueError('image_decode_failed')
                    for frame in frames:
                        w,h=int(frame['width']),int(frame['height'])
                        if min(w,h)<=0 or max(w,h)>16384 or w*h>24_000_000:
                            raise ValueError('image_dimensions_exceeded')
                    return frames
                first_path=work/'first.hevc';write_parts(first_path,parts[:1]);first=probe(first_path)
                width,height=int(first[0]['width']),int(first[0]['height'])
                masked=first[0]['pix_fmt'].startswith('gray') and len(parts)>1
                if masked and len(parts)%2:raise ValueError('invalid_image_partition')
                color_path=work/'color.hevc';write_parts(color_path,parts[1::2] if masked else parts)
                colors=probe(color_path)
                if any((int(f['width']),int(f['height']))!=(width,height)for f in colors):
                    raise ValueError('image_dimensions_exceeded')
                if len(colors)>200 or width*height*len(colors)>120_000_000:raise ValueError('image_dimensions_exceeded')
                ensure_space(work,width*height*4*len(colors))
                args=[str(ffmpeg),'-v','error','-nostdin','-threads','2','-f','hevc','-r','10','-i',str(color_path)]
                if masked:
                    mask_path=work/'mask.hevc';write_parts(mask_path,parts[::2]);masks=probe(mask_path)
                    if len(masks)!=len(colors) or any(not f['pix_fmt'].startswith('gray') or
                        (int(f['width']),int(f['height']))!=(width,height)for f in masks):raise ValueError('invalid_image_partition')
                    args+=['-f','hevc','-r','10','-i',str(mask_path),'-filter_complex',
                           '[0:v]format=rgb24[c];[c][1:v]alphamerge[out]','-map','[out]']
                output=work/'decoded.png';animated=len(colors)>1
                args+=['-frames:v',str(len(colors)),'-f','apng'if animated else 'image2','-c:v','apng'if animated else 'png']
                if animated:args+=['-plays','0']
                args+=[str(output)]
                result=subprocess.run(args,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
                if result.returncode or not output.is_file():raise ValueError('image_decode_failed')
                with output.open('rb')as handle:header=handle.read(24)
                if len(header)<24 or header[:8]!=b'\x89PNG\r\n\x1a\n' or struct.unpack('>II',header[16:24])!=(width,height):
                    raise ValueError('image_decode_failed')
                output.chmod(0o600);output.replace(destination);stamp.write_text(digest)
                return destination,'ready'
    except subprocess.TimeoutExpired:return None,'image_decode_timeout'
    except (OSError,ValueError,KeyError,TypeError):return None,'image_decode_failed'
    finally:_slots.release()
