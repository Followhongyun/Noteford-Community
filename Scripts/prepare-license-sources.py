"""Fetch only reviewed, checksum-pinned corresponding-source archives."""
import hashlib
import json
import urllib.request
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'src'))
from noteford.build_tls import build_context
cache = root / 'build/redistributable-source'
cache.mkdir(parents=True, exist_ok=True)
for item in json.loads((root / 'packaging/redistributable-sources.json').read_text())['files']:
    destination = cache / item['file']
    if not destination.resolve().is_relative_to(cache.resolve()):
        raise ValueError('Invalid source archive path')
    if destination.is_file() and hashlib.sha256(destination.read_bytes()).hexdigest() == item['sha256']:
        continue
    with urllib.request.urlopen(item['url'], timeout=60, context=build_context()) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != item['sha256']:
        raise ValueError('Source archive hash mismatch: ' + item['file'])
    destination.write_bytes(data)
print('Version-matched source archives ready')
