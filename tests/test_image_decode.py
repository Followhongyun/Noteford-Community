import shutil
import struct
import subprocess

import pytest

from noteford.image_decode import decode_wxgf


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='local decoder unavailable')
@pytest.mark.parametrize('masked', [False, True])
def test_wxgf_real_decoder_preserves_dimensions_and_reuses_cache(tmp_path, monkeypatch, masked):
    result = subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-f', 'lavfi',
        '-i', 'color=c=green:s=96x64:r=1', '-frames:v', '1', '-c:v', 'libx265',
        '-x265-params', 'pools=1:frame-threads=1:log-level=error', '-f', 'hevc', 'pipe:1'],
        capture_output=True, timeout=20)
    assert result.returncode == 0
    stream = result.stdout
    if masked:
        mask = subprocess.run([shutil.which('ffmpeg'), '-v', 'error', '-f', 'lavfi',
            '-i', 'color=c=white:s=96x64:r=1', '-frames:v', '1', '-pix_fmt', 'gray', '-c:v', 'libx265',
            '-x265-params', 'pools=1:frame-threads=1:log-level=error', '-f', 'hevc', 'pipe:1'],
            capture_output=True, timeout=20)
        assert mask.returncode == 0
        stream = mask.stdout + stream
    data = b'wxgf\x00\x00synthetic-header' + stream
    destination = tmp_path / 'image.png'
    path, status = decode_wxgf(data, destination)
    assert status == 'ready'
    assert struct.unpack('>II', path.read_bytes()[16:24]) == (96, 64)
    if masked:
        assert path.read_bytes()[25] == 6  # RGBA, preserves the transparency channel.
    monkeypatch.setattr(shutil, 'which', lambda _: None)
    assert decode_wxgf(data, destination) == (destination, 'ready')
    assert decode_wxgf(data + b'changed', tmp_path / 'another.png')[1] == 'image_decoder_unavailable'


def test_bad_container_does_not_replace_existing_image(tmp_path):
    destination = tmp_path / 'image.png'
    destination.write_bytes(b'existing')
    assert decode_wxgf(b'wxgf-invalid', destination)[0] is None
    assert destination.read_bytes() == b'existing'


def test_timeout_is_reported_without_creating_image(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, 'which', lambda name: name)
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('decoder', 20)
    monkeypatch.setattr(subprocess, 'run', timeout)
    path, status = decode_wxgf(b'wxgf-header\x00\x00\x00\x01\x40data', tmp_path / 'image.png')
    assert path is None and status == 'image_decode_timeout'
    assert not (tmp_path / 'image.png').exists()
