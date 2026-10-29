"""Keychain-only Mac secrets; legacy files are deleted only after readback.

The paths remain migration locators, never operational plaintext fallbacks.
Windows file storage is deliberately outside this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from .runtime_lock import RuntimeLock


@contextmanager
def _secret_lock(root):
    # RuntimeLock is deliberately nonblocking for exclusive archive jobs.
    # Secret reads/writes instead queue so ordinary status/sync overlap is safe.
    lease = RuntimeLock(root)
    while True:
        try:
            lease.__enter__()
            break
        except RuntimeError as exc:
            if str(exc) != 'data_root_in_use':
                raise
            time.sleep(0.01)
    try:
        yield
    finally:
        lease.__exit__(None, None, None)


def _error(code):
    from .mac_capture import CaptureError
    return CaptureError(code)


def _store(store):
    if store is None:
        from .mac_capture import SecurityKeychainStore
        return SecurityKeychainStore()
    return store


def _decode(raw):
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError, UnicodeError):
        raise _error('key_store_invalid') from None


def _guard_path(path):
    # Check the workspace before creating locks or accessing any legacy file.
    from .workspace_access import process_root, require_source
    root = process_root()
    if root is not None:
        require_source(root, path)
    if path.is_symlink():
        raise _error('key_file_symlink')


def _legacy(path):
    _guard_path(path)
    if not path.exists():
        return None
    return _decode(path.read_bytes())


def _checked_put(store, account, payload):
    raw = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
    store.put(account, raw)
    if store.get(account) != raw:
        raise _error('keychain_readback_failed')


def _db_payload(value):
    if value is None:
        return {'schema': 1, 'keys': [], 'observed_accounts': []}
    keys = value.get('keys')
    observed = value.get('observed_accounts', [])
    if not isinstance(keys, list) or not isinstance(observed, list) or not all(isinstance(x, str) for x in observed):
        raise _error('key_store_invalid')
    for item in keys:
        try:
            if not isinstance(item, dict) or not isinstance(item['relative_path'], str) or not item['relative_path']:
                raise ValueError()
            if len(bytes.fromhex(item['key_hex'])) != 32:
                raise ValueError()
            if item.get('salt_hex') and len(bytes.fromhex(item['salt_hex'])) != 16:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise _error('key_store_invalid') from None
    return {'schema': 1, 'keys': keys, 'observed_accounts': observed}


def database_payload(path: Path, store=None, *, captured=None, observed_accounts=None):
    """Read/migrate DB records, optionally committing newly verified capture.

    Denied/locked Keychain reads propagate. No disk deletion or operational
    fallback occurs on failure. New captures supersede only their own DB paths.
    """
    from .mac_capture import DB_KEY_ACCOUNT, merge_key_records
    path = Path(path)
    _guard_path(path)
    store = _store(store)
    from .mac_resign import default_state_dir
    with _secret_lock(default_state_dir() / '.keychain-migration'):
        raw = store.get(DB_KEY_ACCOUNT)
        current = _db_payload(_decode(raw) if raw is not None else None)
        old = _legacy(path)
        if old is not None:
            old = _db_payload(old)
            by_path = {x['relative_path']: x for x in current['keys']}
            replacements = {x['relative_path'] for x in (captured or [])}
            for item in old['keys']:
                other = by_path.get(item['relative_path'])
                if other and item['relative_path'] not in replacements and any(
                    other.get(k) != item.get(k) for k in ('key_hex', 'salt_hex')
                ):
                    raise _error('key_migration_conflict')
            current['keys'] = merge_key_records(old['keys'], current['keys'])
            current['observed_accounts'] = sorted(set(old['observed_accounts'] + current['observed_accounts']))
        if captured is not None:
            _db_payload({'keys': captured})
            current['keys'] = merge_key_records(current['keys'], captured)
        if observed_accounts is not None:
            current['observed_accounts'] = sorted(set(current['observed_accounts'] + observed_accounts))
        if old is not None or captured is not None or observed_accounts is not None:
            _checked_put(store, DB_KEY_ACCOUNT, current)
            path.unlink(missing_ok=True)
        return current


def _validate_cache(value, kind):
    try:
        if kind == 'image':
            if len(value['aes_key_ascii'].encode('ascii')) != 16 or not 0 <= int(value['xor_key']) <= 255:
                raise ValueError()
        elif kind == 'candidates':
            if not isinstance(value['candidates'], list):
                raise ValueError()
            for item in value['candidates']:
                if len(bytes.fromhex(item['key_hex'])) != 32:
                    raise ValueError()
                if item.get('salt_hex') and len(bytes.fromhex(item['salt_hex'])) != 16:
                    raise ValueError()
        else:
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError, UnicodeError):
        raise _error('key_store_invalid') from None


def migrate_cache_path(source, destination, kind):
    """Registry relocation keeps working even after the legacy file is gone."""
    original = cached_secret(source, kind)
    target = cached_secret(destination, kind)
    if original is None:
        return
    if target is not None and original != target:
        raise _error('key_migration_conflict')
    if target is None:
        cached_secret(destination, kind, payload=original)


def cached_secret(path: Path, kind: str, *, payload=None, store=None):
    """Per-library image/candidate cache, additionally scoped by workspace."""
    path = Path(path)
    _guard_path(path)
    store = _store(store)
    account = 'noteford-wechat-' + kind + ':' + hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    with _secret_lock(path.parent / ('.keychain-' + kind)):
        raw = store.get(account)
        existing = _decode(raw) if raw is not None else None
        old = _legacy(path)
        residue = path.with_suffix(path.suffix + '.tmp')
        if old is None and existing is None and payload is None:
            old = _legacy(residue)
        result = payload if payload is not None else (existing if existing is not None else old)
        if old is not None and existing is not None and payload is None and old != existing:
            raise _error('key_migration_conflict')
        for value in (existing, old, payload):
            if value is not None:
                _validate_cache(value, kind)
        if payload is not None or old is not None or (existing is not None and residue.exists()):
            _checked_put(store, account, result)
            path.unlink(missing_ok=True)
            # Previous atomic-write residue is never a required operational key.
            residue.unlink(missing_ok=True)
        return result


def cached_candidates(path: Path):
    from .mac_resign import default_state_dir, keys_path
    cached = cached_secret(path, 'candidates') or {}
    rows = cached.get('candidates', [])
    if not isinstance(rows, list):
        raise _error('key_store_invalid')
    return rows + database_payload(keys_path(default_state_dir()))['keys']


def cleanup_capture_files(state_dir: Path):
    """Only capture-owned artifacts; never the migration source keys.json."""
    for name in ('keys.pending.json', 'keys.pending.new', 'keys.partial.json',
                 'keys.partial.new', 'keys.new', 'capture-events.jsonl',
                 'targets.json', 'hook-candidates.jsonl'):
        (state_dir / name).unlink(missing_ok=True)


def cleanup_hook_files(db_root: Path):
    from .mac_interpose import sandbox_candidates_path
    legacy = sandbox_candidates_path(db_root)
    legacy.unlink(missing_ok=True)
    # New captures have unique PID-tagged files; do not unlink an active writer.
    paths = list(legacy.parent.glob('hook-candidates-*.jsonl'))
    paths += list(legacy.parent.glob('hook-capture-*'))
    for path in paths:
        try:
            pid = int(path.name.split('-')[2])
            os.kill(pid, 0)
        except ProcessLookupError:
            if path.is_dir() and not path.is_symlink():
                cleanup_hook_capture(path / 'candidates.jsonl')
            else:
                path.unlink(missing_ok=True)
        except (ValueError, PermissionError):
            continue


def capture_session(function):
    """Serialize capture writers and always remove their transient secrets."""
    @wraps(function)
    def wrapped(config, *args, **kwargs):
        with RuntimeLock(config.work_root / '.capture'):
            cleanup_capture_files(config.work_root)
            try:
                return function(config, *args, **kwargs)
            finally:
                cleanup_capture_files(config.work_root)
    return wrapped


def cleanup_stale_capture_files(state_dir):
    try:
        with RuntimeLock(state_dir / '.capture'):
            cleanup_capture_files(state_dir)
    except RuntimeError as exc:
        if str(exc) != 'data_root_in_use':
            raise


def cleanup_hook_capture(path):
    path.unlink(missing_ok=True)
    if path.parent.name.startswith('hook-capture-'):
        try:
            path.parent.rmdir()
        except FileNotFoundError:
            pass


def migrate_workspace_caches(root: Path, store=None):
    """Migrate known cache locations, including currently unused libraries."""
    _guard_path(root)
    directories = [root / 'secrets', *sorted((root / 'libraries').glob('*/secrets'))]
    migrated = 0
    for directory in directories:
        for filename, kind in (('image_keys.json', 'image'), ('candidate_keys.json', 'candidates')):
            path = directory / filename
            if path.exists() or path.with_suffix('.json.tmp').exists():
                cached_secret(path, kind, store=store)
                migrated += 1
    return migrated
