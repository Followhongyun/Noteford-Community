"""Offline data backups. Restore stages into a new directory, never overwrites."""
import argparse
import hashlib
import json
import shutil
import stat
from pathlib import Path, PurePosixPath

from .runtime_lock import RuntimeLock


def inventory(root):
    result = {}
    def walk(folder):
        for path in sorted(folder.iterdir()):
            yield path
            info = path.lstat()
            if path.is_dir() and not path.is_symlink() and not (getattr(info, 'st_file_attributes', 0) & 1024):
                if path.relative_to(root).as_posix() != 'data/runtime':
                    yield from walk(path)
    for path in walk(root):
        relative = path.relative_to(root).as_posix()
        if relative == 'data/runtime' or relative.startswith('data/runtime/'):
            continue
        info = path.lstat()
        if path.is_symlink() or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024):
            raise ValueError('backup_reparse_point_not_supported')
        if path.is_file():
            with path.open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            result[relative] = {'bytes': info.st_size, 'sha256': digest}
    return result


def create(root: Path, destination: Path):
    root, destination = root.resolve(), destination.resolve()
    if not (root / 'config.local.json').is_file():
        raise ValueError('backup_config_missing')
    if destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError('backup_roots_overlap')
    if destination.exists():
        raise ValueError('backup_destination_exists')
    with RuntimeLock(root):
        before = inventory(root)
        destination.mkdir(parents=True)
        content = destination / 'content'
        content.mkdir()
        for name in before:
            target = content / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, target)
        if inventory(root) != before or inventory(content) != before:
            raise ValueError('backup_source_changed_or_copy_corrupt')
        # Write the completion marker last. Partial directories are not backups.
        (destination / 'manifest.json').write_text(json.dumps(
            {'version': 1, 'files': before}, indent=2), encoding='utf-8')
    return {'files': len(before), 'verified': True}


def verify(backup: Path):
    payload = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
    if (payload.get('version') != 1 or not isinstance(payload.get('files'), dict)
            or 'config.local.json' not in payload['files']
            or not (backup / 'content').is_dir()):
        raise ValueError('backup_manifest_invalid')
    for name in payload['files']:
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or ':' in name or '\\' in name:
            raise ValueError('backup_manifest_invalid')
    if inventory(backup / 'content') != payload['files']:
        raise ValueError('backup_integrity_mismatch')
    return payload['files']


def restore(backup: Path, destination: Path):
    backup, destination = backup.resolve(), destination.resolve()
    expected = verify(backup)
    if destination.exists():
        raise ValueError('restore_destination_exists')
    if destination.is_relative_to(backup) or backup.is_relative_to(destination):
        raise ValueError('backup_roots_overlap')
    destination.mkdir(parents=True)
    for name in expected:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup / 'content' / name, target)
    if inventory(destination) != expected:
        raise ValueError('restore_integrity_mismatch')
    return {'files': len(expected), 'verified': True, 'staged_only': True}


def main():
    parser = argparse.ArgumentParser(description='Offline backup; stop desktop and API/MCP processes first. Backups contain private data.')
    parser.add_argument('action', choices=['create', 'verify', 'restore'])
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path, nargs='?')
    args = parser.parse_args()
    try:
        if args.action == 'verify':
            result = {'files': len(verify(args.source)), 'verified': True}
        else:
            if args.destination is None:
                parser.error('destination required')
            result = globals()[args.action](args.source, args.destination)
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        print(json.dumps({'verified': False, 'error': 'backup_operation_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
