"""Local-only WXGF still-image conversion, including separate alpha masks."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading

_slots = threading.BoundedSemaphore(2)


def decode_wxgf(data: bytes, destination: Path) -> tuple[Path | None, str]:
    if not data.startswith(b'wxgf') or len(data) > 64 * 1024 * 1024:
        return None, 'unsupported_image_container'
    digest = hashlib.sha256(data).hexdigest()
    stamp = destination.with_suffix('.png.sha256')
    try:
        if destination.is_file() and stamp.read_text() == digest:
            with destination.open('rb') as handle:
                if handle.read(8) == b'\x89PNG\r\n\x1a\n':
                    return destination, 'ready'
    except OSError:
        pass
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        return None, 'image_decoder_unavailable'
    # Locate the Annex B VPS, preserving all subsequent NAL units. Never guess
    # a fixed header length; known containers have different header sizes.
    offsets = [data.find(marker) for marker in (b'\x00\x00\x00\x01\x40', b'\x00\x00\x01\x40')]
    offsets = [offset for offset in offsets if 4 <= offset <= 4096]
    if not offsets:
        return None, 'unsupported_image_container'
    stream = data[min(offsets):]
    options = {'input': stream, 'capture_output': True, 'timeout': 20,
               'creationflags': subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0}
    if not _slots.acquire(timeout=2):
        return None, 'image_decoder_busy'
    try:
        probe = subprocess.run([ffprobe, '-v', 'error', '-f', 'hevc', '-show_frames',
            '-show_entries', 'frame=width,height,pix_fmt', '-of', 'json', 'pipe:0'], **options)
        info = json.loads(probe.stdout).get('frames', [])
        if probe.returncode or not info:
            return None, 'image_decode_failed'
        width, height = int(info[0]['width']), int(info[0]['height'])
        partitions = [match.start() for match in re.finditer(b'\x00\x00\x00\x01\x40|(?<!\x00)\x00\x00\x01\x40', stream)]
        masked = (len(info) == 2 and len(partitions) == 2
                  and info[0].get('pix_fmt') == 'gray'
                  and info[1].get('pix_fmt') in {'yuv420p', 'yuvj420p'}
                  and (info[1]['width'], info[1]['height']) == (width, height))
        if len(info) != 1 and not masked:
            return None, 'unsupported_multiframe_image'
        if min(width, height) <= 0 or max(width, height) > 32768 or width * height > 24_000_000:
            return None, 'image_dimensions_exceeded'
        destination.parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(destination.parent).free < 2 * 1024**3 + width * height * 4:
            return None, 'insufficient_space'
        if masked:
            # A grayscale alpha partition plus one color partition is one image,
            # not an animation. Decode separately so pixel-format changes cannot
            # reset filter state and accidentally select the mask as the picture.
            with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.wxgf-') as temp:
                mask_path, color_path = Path(temp) / 'mask.hevc', Path(temp) / 'color.hevc'
                mask_path.write_bytes(stream[:partitions[1]])
                color_path.write_bytes(stream[partitions[1]:])
                file_options = {key: value for key, value in options.items() if key != 'input'}
                result = subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-threads', '2',
                    '-f', 'hevc', '-i', str(mask_path), '-f', 'hevc', '-i', str(color_path),
                    '-filter_complex', '[1:v]format=rgb24[c];[c][0:v]alphamerge[out]',
                    '-map', '[out]', '-frames:v', '1', '-f', 'image2pipe', '-c:v', 'png', 'pipe:1'], **file_options)
        else:
            result = subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-threads', '2',
                '-f', 'hevc', '-i', 'pipe:0', '-frames:v', '1', '-f', 'image2pipe',
                '-c:v', 'png', 'pipe:1'], **options)
        png = result.stdout
        if result.returncode or not png.startswith(b'\x89PNG\r\n\x1a\n') or len(png) < 24 or struct.unpack('>II', png[16:24]) != (width, height):
            return None, 'image_decode_failed'
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=destination.parent, suffix='.png.tmp', delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(png)
            temporary.replace(destination)
            stamp.write_text(digest)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        return destination, 'ready'
    except subprocess.TimeoutExpired:
        return None, 'image_decode_timeout'
    except (OSError, ValueError, KeyError, TypeError):
        return None, 'image_decode_failed'
    finally:
        _slots.release()


def decode_wxgf_file(source, destination):
    from .mac_image_decode import decode_wxgf_file as decode_file
    return decode_file(source, destination, decoder_binaries=(shutil.which('ffmpeg'), shutil.which('ffprobe')))
