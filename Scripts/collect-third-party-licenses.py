"""Collect the actual frozen-service and production npm notices, without network access.

This is an evidence collector, not a legal clearance engine. A separate review
records unresolved native/source-distribution obligations. Never infer clearance
from an SPDX label or from the presence of a license file alone.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata as metadata
import json
import re
import sys
from pathlib import Path


def sha(path: Path) -> str:
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def is_notice(path: Path) -> bool:
    return path.name.lower().startswith(('license', 'licence', 'copying', 'notice', 'copyright'))


def collect(root: Path, output: Path, analysis: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    entries, files, issues = [], {}, []

    def copy(source: Path, target: str) -> str:
        destination = output / target
        if not destination.resolve().is_relative_to(output.resolve()):
            raise ValueError('Notice path escapes output directory')
        if not source.is_file() or source.stat().st_size == 0:
            raise ValueError(f'Missing or empty notice: {source}')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        files[target] = sha(destination)
        return target

    lock = json.loads((root / 'desktop/package-lock.json').read_text(encoding='utf-8'))
    for rel, record in sorted(lock['packages'].items()):
        if not rel or record.get('dev'):
            continue
        folder = root / 'desktop' / rel
        package = json.loads((folder / 'package.json').read_text(encoding='utf-8'))
        name, version = package['name'], package['version']
        if version != record['version']:
            raise ValueError(f'Installed npm version differs from lock: {name}')
        target = 'npm/' + rel.removeprefix('node_modules/').replace('node_modules/', '_nested_')
        notices = [copy(p, f'{target}/{p.name}') for p in sorted(folder.iterdir()) if p.is_file() and is_notice(p)]
        copy(folder / 'package.json', f'{target}/package.json')
        if not notices:
            issues.append(f'npm:{name}@{version}: upstream package has no license/notice text')
        entries.append(dict(ecosystem='npm', name=name, version=version,
                            license=package.get('license', record.get('license', 'UNKNOWN')), notices=notices))

    toc = ast.literal_eval(analysis.read_text(encoding='utf-8'))
    sources = {str(Path(row[1]).resolve()).casefold()
               for section in toc if isinstance(section, list)
               for row in section if isinstance(row, tuple) and len(row) == 3 and isinstance(row[1], str)}
    owned = set()
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
        paths = {str(Path(dist.locate_file(f)).resolve()).casefold() for f in dist.files or []}
        included = sources & paths
        if not included:
            continue
        owned.update(included)
        name, version = dist.metadata['Name'], dist.version
        safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', name)
        notices = []
        for f in dist.files or []:
            source = Path(dist.locate_file(f))
            if not is_notice(source) or not source.is_file():
                continue
            # pywin32 contains unrelated LGPL adodbapi/Pythonwin components;
            # the frozen service only includes its win32 modules and loader.
            if name.lower() == 'pywin32' and str(f).replace('\\', '/') not in ('win32/License.txt', 'pywin32-312.dist-info/licenses/License.txt'):
                continue
            rel = str(f).replace('\\', '/').replace('../', '_parent_/')
            notices.append(copy(source, f'python/{safe_name}/{rel}'))
        if not notices:
            issues.append(f'python:{name}@{version}: no license/notice text')
        label = dist.metadata.get('License-Expression') or dist.metadata.get('License') or 'See included license text'
        entries.append(dict(ecosystem='python', name=name, version=version,
                            license=label, notices=notices, collected_file_count=len(included)))
    unknown = sorted(p for p in sources if 'site-packages' in p and p not in owned)
    if unknown:
        issues.extend('unmapped frozen module: ' + Path(p).name for p in unknown)

    electron = root / 'desktop/node_modules/electron'
    electron_version = json.loads((electron / 'package.json').read_text())['version']
    copy(electron / 'dist/LICENSE', 'runtime/LICENSE.electron.txt')
    copy(electron / 'dist/LICENSES.chromium.html', 'runtime/LICENSES.chromium.html')
    copy(Path(sys.base_prefix) / 'LICENSE.txt', 'runtime/LICENSE.python.txt')
    copy(Path(sys.base_prefix) / 'Doc/html/license.html', 'runtime/LICENSE.python-stdlib.html')
    copy(root / 'NOTICE.md', 'NOTICE.md')
    copy(root / 'packaging/DISTRIBUTION-TERMS.txt', 'DISTRIBUTION-TERMS.txt')
    copy(root / 'packaging/FFMPEG-SOURCE-AND-RELINKING.md', 'FFMPEG-SOURCE-AND-RELINKING.md')
    copy(root / 'packaging/INSTALLER-SOURCES.md', 'INSTALLER-SOURCES.md')
    source_root = root / 'packaging/license-sources'
    source_records = json.loads((source_root / 'sources.json').read_text())
    for record in source_records:
        p = source_root / record['file']
        if sha(p) != record['sha256']:
            raise ValueError(f'Upstream notice hash mismatch: {p.name}')
        copy(p, 'upstream/' + record['file'])
    copy(source_root / 'sources.json', 'upstream/sources.json')
    source_policy = json.loads((root / 'packaging/redistributable-sources.json').read_text())
    if source_policy['electron_version'] != electron_version:
        issues.append('Electron version changed: corresponding-source mapping requires review')
    provenance = source_root / 'electron-build/binary-provenance.json'
    if provenance.is_file() and json.loads(provenance.read_text())['version'] != electron_version:
        issues.append('Electron Windows binary changed: renew official ZIP/DLL provenance before distribution')
    for record in source_policy['files']:
        source = root / 'build/redistributable-source' / record['file']
        if not source.is_file() or sha(source) != record['sha256']:
            raise ValueError('Missing version-matched source archive: run Scripts/prepare-license-sources.py')
        copy(source, 'sources/' + record['file'])
    copy(root / 'packaging/redistributable-sources.json', 'sources/provenance.json')

    review = json.loads((root / 'packaging/license-review.json').read_text(encoding='utf-8'))
    issues.extend(review['unresolved'])
    policy = json.loads((root / 'packaging/license-policy.json').read_text(encoding='utf-8'))
    for entry in entries:
        key = f"{entry['ecosystem']}:{entry['name']}@{entry['version']}"
        expected = policy.get(key)
        actual = {name: files[name] for name in entry['notices']}
        if not expected or expected['license'] != entry['license'] or expected['notices'] != actual:
            issues.append(f'{key}: new or changed package/notice requires review')
    copy(root / 'packaging/license-review.json', 'REVIEW.json')
    service = root / 'build/service/noteford-service'
    binaries = {str(p.relative_to(service)).replace('\\', '/'): sha(p)
                for p in sorted(service.rglob('*')) if p.is_file()}
    if not binaries or 'noteford-service.exe' not in binaries:
        raise ValueError('Frozen service output is missing')
    index = dict(schema_version=1, app_version=json.loads((root / 'desktop/package.json').read_text())['version'],
                 electron_version=electron_version, python_version=sys.version.split()[0],
                 analysis_sha256=sha(analysis), npm_lock_sha256=sha(root / 'desktop/package-lock.json'),
                 entries=entries, files=files, service_files=binaries,
                 unresolved=issues, commercial_clearance=not issues)
    (output / 'manifest.json').write_text(json.dumps(index, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    lines = ['# Noteford third-party notices', '',
             'License documents retain their original text. Reference projects are not runtime dependencies.',
             'This collection is not a declaration that all redistribution obligations are complete.', '',
             '| Ecosystem | Package | Version | Declared license |', '| --- | --- | --- | --- |']
    for item in entries:
        label = item['license'].split('\n')[0].replace('|', '/')
        lines.append(f"| {item['ecosystem']} | {item['name']} | {item['version']} | {label} |")
    lines.extend(['', 'Electron/Chromium and Python notices: runtime/.',
                  'Reference and native-library supplemental notices: upstream/.', '', '## Unresolved before commercial distribution', ''])
    lines.extend('- ' + item for item in issues)
    (output / 'INDEX.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return index


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path)
    parser.add_argument('--analysis', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    result = collect(root, args.output or root / 'build/third-party-licenses',
                     args.analysis or root / 'build/pyinstaller-work/noteford-service/Analysis-00.toc')
    print(json.dumps({'packages': len(result['entries']), 'notice_files': len(result['files']),
                      'unresolved_count': len(result['unresolved']),
                      'commercial_clearance': result['commercial_clearance']}))
