"""Build the Mac SILK helper from checksum-pinned, BSD-licensed SDK sources."""
from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from .build_tls import build_context

URL = 'https://files.pythonhosted.org/packages/66/bb/938dd697b6bc2d851ffec4ffe82ed20078e58bd0ef049e84f4d038d6f991/pilk-0.2.4.tar.gz'
SHA256 = 'd4a1bcf93dc6ef5e95e0cfd728ed4ef4d49f9c0476d70816fecbe456cc762e7f'


def build_silk(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    cached = directory/'pilk-sdk-source.tar.gz'
    data = cached.read_bytes() if cached.is_file() else b''
    if hashlib.sha256(data).hexdigest() != SHA256:
        with urllib.request.urlopen(URL, timeout=30, context=build_context()) as response:
            data = response.read(4 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != SHA256:
            raise ValueError('SILK source checksum mismatch')
        cached.write_bytes(data)
    source = directory/'silk-sdk'
    source.mkdir(exist_ok=True)
    extracted = []
    # Only original Skype SDK files are used. The pilk Python bindings and
    # its separately licensed wrappers are neither copied nor compiled.
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for entry in archive.getmembers():
            relative = Path(entry.name).relative_to('pilk-0.2.4')
            if (not entry.isfile() or relative.suffix not in {'.c', '.h'} or
                    relative.parent.as_posix() not in {'src/SKP_SILK_SRC', 'src/interface'}):
                continue
            content = archive.extractfile(entry).read()
            if b'Copyright (c) 2006-2012, Skype Limited' not in content[:3500]:
                raise ValueError('Unexpected SILK SDK source license')
            target = source/relative.name
            target.write_bytes(content)
            extracted.append(target)
    binary = directory/'noteford-silk'
    subprocess.run(['/usr/bin/clang', '-O2', '-w', '-I', str(source),
                    str(Path(__file__).with_name('mac_silk_decode.c')),
                    *map(str, sorted(p for p in extracted if p.suffix == '.c')), '-o', str(binary)],
                   check=True, timeout=180)
    licenses = directory.parent/'third-party-licenses/mac-silk'
    licenses.mkdir(parents=True, exist_ok=True)
    header = (source/'SKP_Silk_SDK_API.h').read_text()
    (licenses/'LICENSE.txt').write_text(header[:header.index('*/') + 2] + '\n')
    with tarfile.open(licenses/'skype-silk-sdk.tar.gz', 'w:gz') as archive:
        for file in sorted(extracted):
            archive.add(file, arcname='silk-sdk/'+file.name)
    (licenses/'SOURCE.txt').write_text(f'Skype SILK SDK files from {URL}\nSHA-256: {SHA256}\n'
                                      'Only the original BSD-licensed Skype SDK is linked.\n')
    return binary
