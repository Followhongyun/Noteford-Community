"""Explicit, local-user complete uninstall. No source-directory traversal."""
from __future__ import annotations
import argparse
import json
import plistlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time

BUNDLE = 'com.followhongyun.noteford'
SERVICES = (BUNDLE, 'noteford-desktop Safe Storage', 'Noteford Safe Storage')


def checked_path(path: Path, home: Path) -> Path:
    # Reject symlink ancestors too: a cache or data root must not redirect to WeChat.
    if not path.is_absolute() or path == home or path == Path('/'):
        raise ValueError('unsafe_path')
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError('symlink_path')
    return path


def targets(home: Path, app: Path) -> list[Path]:
    if app not in (Path('/Applications/Noteford.app'), home / 'Applications/Noteford.app'):
        raise ValueError('unsupported_app_location')
    paths = [home / 'Noteford/Data']
    for name in ('Noteford', 'noteford-desktop', BUNDLE, BUNDLE + '.uninstaller'):
        paths += [home / 'Library/Application Support' / name,
                  home / 'Library/Caches' / name, home / 'Library/Logs' / name]
    paths += [home / 'Library/Caches' / BUNDLE,
              home / 'Library/Caches' / (BUNDLE + '.ShipIt'),
              home / 'Library/Caches/noteford-desktop-updater',
              home / 'Library/Preferences' / (BUNDLE + '.plist'),
              home / 'Library/Saved Application State' / (BUNDLE + '.savedState')]
    paths += [home / 'Library/Preferences' / (BUNDLE + '.uninstaller.plist'),
              home / 'Library/Saved Application State' / (BUNDLE + '.uninstaller.savedState')]
    companion = app.parent / '卸载 Noteford.app'
    if companion.exists():
        checked_path(companion, home)
        with (companion / 'Contents/Info.plist').open('rb') as stream:
            if plistlib.load(stream).get('CFBundleIdentifier') != BUNDLE + '.uninstaller':
                raise ValueError('wrong_companion')
    return [checked_path(p, home) for p in [*paths, app, companion]]


def remove_paths(paths: list[Path], home: Path):
    for p in paths:
        checked_path(p, home)
        if p.is_dir():
            shutil.rmtree(p)  # Python's fd-based traversal does not follow child symlinks.
        elif p.exists():
            p.unlink()
        if p.exists() or p.is_symlink():
            raise OSError('removal_incomplete')
    # Remove empty layout parents only; never erase developer files or user exports.
    for p in (home / 'Noteford/应用', home / 'Noteford'):
        try: p.rmdir()
        except OSError: pass


def clear_credentials(run=subprocess.run):
    for service in SERVICES:
        for _ in range(1024):
            result = run(['/usr/bin/security', 'delete-generic-password', '-s', service],
                         capture_output=True, timeout=30)
            if result.returncode == 44: break  # errSecItemNotFound
            if result.returncode: raise OSError('keychain_cleanup_failed')
        else: raise OSError('keychain_cleanup_limit')


def process_ids(app: Path, run=subprocess.run) -> list[int]:
    result = run(['/bin/ps', '-axo', 'pid=,command='], capture_output=True, text=True, check=True)
    prefix = str(app / 'Contents') + '/'
    return [int(parts[0]) for row in result.stdout.splitlines()
            if len(parts := row.strip().split(None, 1)) == 2
            and parts[1].startswith(prefix) and int(parts[0]) != os.getpid()]


def alive(pid: int) -> bool:
    try: os.kill(pid, 0); return True
    except ProcessLookupError: return False


def notify(message: str):
    # Product-owned completion dialog; never interpolate paths or secrets as code.
    try:
        subprocess.run(['/usr/bin/osascript', '-e', 'on run argv', '-e',
                    'display alert "Noteford 卸载" message (item 1 of argv) buttons {"好"}',
                    '-e', 'end run', message], capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        pass  # Notification failure does not undo successful cleanup.


def run(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--parent', type=int, required=True)
    args = parser.parse_args(argv)
    home = Path.home()
    try:
        if sys.platform != 'darwin' or not getattr(sys, 'frozen', False):
            raise ValueError('installed_mac_only')
        paths = targets(home, args.app)
        companion = args.app.parent / '卸载 Noteford.app'
        if Path(sys.executable).resolve() != companion / 'Contents/Resources/service/noteford-service':
            raise ValueError('wrong_uninstaller')
        if args.parent <= 1 or args.parent != os.getppid():
            raise ValueError('invalid_parent')
        # Do not delete data from a different layout or externally redirected workspace.
        root = paths[0]
        if root.exists():
            marker = json.loads((root / '.product-workspace.json').read_text())
            if marker.get('installation') != str(root): raise ValueError('invalid_data_root')
        if not shutil.rmtree.avoids_symlink_attacks: raise ValueError('unsafe_platform')
        print('READY', flush=True)
        for _ in range(120):
            if not alive(args.parent): break
            time.sleep(.25)
        else: raise OSError('app_did_not_exit')
        for pid in process_ids(args.app):
            try: os.kill(pid, signal.SIGTERM)
            except ProcessLookupError: pass
        for _ in range(80):
            if not process_ids(args.app): break
            time.sleep(.25)
        else: raise OSError('process_still_running')
        clear_credentials()
        # Per-app consent only; never reset WeChat or other applications' permissions.
        result = subprocess.run(['/usr/bin/tccutil', 'reset', 'All', BUNDLE], capture_output=True, timeout=30)
        if result.returncode: raise OSError('permission_cleanup_failed')
        subprocess.run(['/usr/bin/defaults', 'delete', BUNDLE], capture_output=True, timeout=10)
        remove_paths(paths, home)  # App is last, so failures leave a retryable installer.
        (Path(tempfile.gettempdir()) / 'noteford-uninstall-error.txt').unlink(missing_ok=True)
        notify('Noteford 及其本机资料、数据库、缓存和登录凭据已删除。微信原始数据和手动导出的文件未改动。')
        return 0
    except Exception as error:
        # Keep a minimal, secret-free failure receipt; never claim partial cleanup succeeded.
        receipt = Path(tempfile.gettempdir()) / 'noteford-uninstall-error.txt'
        try:
            fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write('Noteford uninstall incomplete: ' + type(error).__name__ + '\n')
        except OSError:
            receipt = Path('无法写入诊断记录')
        try: print('FAILED', flush=True)
        except BrokenPipeError: pass
        notify('卸载未完成，部分文件或权限无法清理。请重新打开“卸载 Noteford”工具重试；诊断记录：' + str(receipt))
        return 1
