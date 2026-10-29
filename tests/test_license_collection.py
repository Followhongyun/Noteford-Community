import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'Scripts/collect-third-party-licenses.py'
spec = importlib.util.spec_from_file_location('license_collection', SCRIPT)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


@pytest.fixture(autouse=True)
def isolated_python_notices(tmp_path, monkeypatch):
    # Notice-copy tests must not depend on the build host's Python distribution.
    prefix = tmp_path / 'python-runtime'
    (prefix / 'Doc/html').mkdir(parents=True)
    (prefix / 'LICENSE.txt').write_text('synthetic Python notice', encoding='utf-8')
    (prefix / 'Doc/html/license.html').write_text('synthetic stdlib notice', encoding='utf-8')
    monkeypatch.setattr(collector, 'sys', SimpleNamespace(base_prefix=str(prefix), version='3.11 fixture'))


def test_nested_upstream_notices_are_not_overwritten(tmp_path):
    root = tmp_path / 'project'
    def write(name, value):
        p=root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(value, encoding='utf-8')
        return p
    write('desktop/package-lock.json', json.dumps({'packages': {}}))
    write('desktop/package.json', '{"version":"test"}')
    write('desktop/node_modules/electron/package.json', '{"version":"test"}')
    write('desktop/node_modules/electron/dist/LICENSE', 'electron fixture')
    write('desktop/node_modules/electron/dist/LICENSES.chromium.html', 'chromium fixture')
    write('NOTICE.md', 'project fixture')
    write('packaging/DISTRIBUTION-TERMS.txt', 'terms fixture')
    write('packaging/FFMPEG-SOURCE-AND-RELINKING.md', 'relink fixture')
    write('packaging/INSTALLER-SOURCES.md', 'installer fixture')
    records=[]
    for package in ['first','second']:
        file=write('packaging/license-sources/'+package+'/LICENSE', package+' original text')
        records.append({'file':package+'/LICENSE','source':'fixture','sha256':hashlib.sha256(file.read_bytes()).hexdigest()})
    write('packaging/license-sources/sources.json',json.dumps(records))
    write('packaging/license-policy.json','{}')
    write('packaging/license-review.json','{"unresolved":["fixture review required"]}')
    write('packaging/redistributable-sources.json','{"electron_version":"test","files":[]}')
    write('build/service/noteford-service/noteford-service.exe','fixture binary')
    analysis=write('Analysis.toc','[[]]')
    result=collector.collect(root,tmp_path/'out',analysis)
    assert (tmp_path/'out/upstream/first/LICENSE').read_text()=='first original text'
    assert (tmp_path/'out/upstream/second/LICENSE').read_text()=='second original text'
    assert result['commercial_clearance'] is False
    assert result['files']['upstream/first/LICENSE'] != result['files']['upstream/second/LICENSE']


def test_upstream_notice_hash_mismatch_is_rejected(tmp_path):
    # The package collector must reject altered source notices, not silently stamp them as original.
    import pytest
    root=tmp_path/'root'
    def put(name,text):
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text);return p
    put('desktop/package-lock.json','{"packages":{}}')
    put('desktop/node_modules/electron/package.json','{"version":"test"}')
    put('desktop/node_modules/electron/dist/LICENSE','fixture')
    put('desktop/node_modules/electron/dist/LICENSES.chromium.html','fixture')
    put('NOTICE.md','fixture')
    put('packaging/DISTRIBUTION-TERMS.txt','fixture')
    put('packaging/FFMPEG-SOURCE-AND-RELINKING.md','fixture')
    put('packaging/INSTALLER-SOURCES.md','fixture')
    put('packaging/license-sources/LICENSE','changed')
    put('packaging/license-sources/sources.json',json.dumps([{'file':'LICENSE','sha256':'0'*64}]))
    analysis=put('Analysis.toc','[[]]')
    with pytest.raises(ValueError, match='Upstream notice hash mismatch'):
        collector.collect(root,tmp_path/'out',analysis)
