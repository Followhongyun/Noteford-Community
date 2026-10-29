"""Build a small LGPL FFmpeg image decoder for the Mac bundle, with no network."""
from __future__ import annotations
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from .build_tls import build_context

VERSION='8.1.3'
URL=f'https://ffmpeg.org/releases/ffmpeg-{VERSION}.tar.xz'
SHA256='7138d28c96d9d3e3af4ee3d8cad72741f8ffb40da90c1112235dea3ecd3178a3'
FLAGS=['--disable-everything','--disable-autodetect','--disable-network','--disable-doc',
       '--disable-debug','--disable-shared','--enable-static','--disable-ffplay',
       '--disable-avdevice','--disable-audiotoolbox','--disable-videotoolbox',
       '--enable-zlib','--enable-decoder=hevc,png','--enable-parser=hevc,png',
       '--enable-demuxer=hevc,image2','--enable-protocol=file,pipe',
       '--enable-encoder=png,apng','--enable-muxer=image2,image2pipe,apng',
       '--enable-filter=format,scale,alphamerge,null','--arch=arm64','--cc=/usr/bin/clang']


def build_images(directory):
    directory=Path(directory).resolve();directory.mkdir(parents=True,exist_ok=True)
    cached=directory/f'ffmpeg-{VERSION}.tar.xz'
    if not cached.is_file() or hashlib.sha256(cached.read_bytes()).hexdigest()!=SHA256:
        with urllib.request.urlopen(URL,timeout=60,context=build_context())as response:
            data=response.read(32*1024*1024)
        if hashlib.sha256(data).hexdigest()!=SHA256:
            raise ValueError('FFmpeg source checksum mismatch')
        cached.write_bytes(data)
    fingerprint=hashlib.sha256(json.dumps([SHA256,FLAGS]).encode()).hexdigest()
    stamp=directory/'image-decoder-build.json'
    binaries=[directory/'noteford-ffmpeg',directory/'noteford-ffprobe']
    if not(stamp.is_file() and stamp.read_text()==fingerprint and all(p.is_file()for p in binaries)):
        source=directory/f'ffmpeg-{VERSION}'
        with tarfile.open(cached)as archive:
            archive.extractall(directory,filter='data')
        subprocess.run([str(source/'configure'),*FLAGS],cwd=source,check=True)
        subprocess.run(['make','-j',str(min(os.cpu_count()or 2,8)),'ffmpeg','ffprobe'],cwd=source,check=True)
        for binary,name in zip(binaries,('ffmpeg','ffprobe')):
            shutil.copyfile(source/name,binary);binary.chmod(0o755)
        stamp.write_text(fingerprint)
    licenses=directory.parent/'third-party-licenses/mac-image-decoder'
    licenses.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(cached,licenses/cached.name)
    source=directory/f'ffmpeg-{VERSION}'
    for name in ('COPYING.LGPLv2.1','LICENSE.md'):
        shutil.copyfile(source/name,licenses/name)
    (licenses/'BUILD.json').write_text(json.dumps({'source':URL,'sha256':SHA256,'configure':FLAGS,
        'build':'make -j8 ffmpeg ffprobe','license':'LGPL-2.1-or-later',
        'network':False,'gpl':False,'nonfree':False},indent=2)+'\n')
    shutil.copyfile(Path(__file__),licenses/'mac_image_build.py')
    return binaries
