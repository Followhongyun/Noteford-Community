"""Active desktop product-account boundary shared by HTTP, MCP and Mac sources.

Unmanaged CLI/test roots retain their existing behavior. Desktop-managed roots
carry a marker and must match the current, live desktop lease on every use.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import psutil


def _marker(root: Path) -> dict | None:
    file = root.resolve() / '.product-workspace.json'
    if not file.exists():
        return None
    try:
        value = json.loads(file.read_text(encoding='utf-8'))
        if value['version'] != 1 or not Path(value['installation']).is_absolute():
            raise ValueError()
        return value
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PermissionError('product_workspace_locked') from exc


def require_active(root: Path, epoch: str | None = None) -> str | None:
    root = root.resolve()
    marker = _marker(root)
    if marker is None:
        return None
    try:
        installation = Path(marker['installation']).resolve()
        lease = json.loads((installation / 'data/runtime/product-account/active-workspace.json').read_text(encoding='utf-8'))
        if (lease['version'] != 1 or lease.get('active') is not True
                or Path(lease['root']).resolve() != root or lease['workspace'] != marker['workspace']
                or not isinstance(lease['epoch'], str) or not lease['epoch']
                or (epoch is not None and epoch != lease['epoch'])):
            raise ValueError()
        pid = lease['pid']
        if not isinstance(pid, int) or pid <= 0:
            raise ValueError()
        process = psutil.Process(pid)
        if (not process.is_running() or Path(process.exe()).resolve() != Path(lease['appExecutable']).resolve()
                or abs(process.create_time() * 1000 - lease['processStartedAtMs']) > 5000):
            raise ValueError()
        return lease['epoch']
    except (OSError, ValueError, KeyError, TypeError, psutil.Error) as exc:
        raise PermissionError('product_workspace_locked') from exc


class WorkspaceAccess:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.epoch = require_active(self.root)

    def check(self):
        current = require_active(self.root, self.epoch)
        # A previously unmanaged reader cannot adopt a newly protected workspace.
        if current != self.epoch:
            raise PermissionError('product_workspace_changed')


def process_root() -> Path | None:
    value = os.environ.get('NOTEFORD_WORKSPACE_ROOT')
    if not value:
        return None
    root = Path(value).resolve()
    require_active(root, os.environ.get('NOTEFORD_WORKSPACE_EPOCH'))
    return root


def source_allowed(root: Path, source: Path) -> bool:
    """Never preview/import another managed account's archive, including aliases."""
    marker = _marker(root)
    if marker is None:
        return True
    require_active(root)
    source = source.expanduser().resolve()
    own = root.resolve()
    installation = Path(marker['installation']).resolve()
    profiles = installation / 'product-accounts'
    if source.is_relative_to(profiles) and not source.is_relative_to(own if own != installation else installation / '__no_profile__'):
        return False
    for ancestor in (source, *source.parents):
        other = _marker(ancestor)
        if other is not None:
            return ancestor == own
    # Disallow broad ancestors that could scan several managed workspaces.
    return not installation.is_relative_to(source)


def require_source(root: Path, source: Path):
    if not source_allowed(root, source):
        raise PermissionError('product_workspace_source_denied')


def validate_capture_paths(payload: dict, root: Path | None = None, *, public=False):
    root = root or process_root()
    if root is None or _marker(root) is None:
        return
    require_active(root)
    fields = {'state_dir', 'project_root', 'db_root', 'app', 'hook'}
    if public and fields.intersection(payload):
        raise PermissionError('product_workspace_paths_managed')
    for field, expected in (('state_dir', root / 'keycap'), ('project_root', root), ('app', Path('/Applications/WeChat.app'))):
        if payload.get(field) and Path(str(payload[field])).expanduser().resolve() != expected.resolve():
            raise PermissionError('product_workspace_paths_managed')
    if payload.get('db_root'):
        require_source(root, Path(str(payload['db_root'])))
