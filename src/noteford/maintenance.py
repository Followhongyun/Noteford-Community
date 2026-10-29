from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


GENERATION_RE = re.compile(r'^(?P<name>.+)-(?P<stamp>\d{8}-\d{6})$')


@dataclass
class CleanupResult:
    apply: bool
    generations_seen: int
    generations_removed: int
    bytes_removed: int
    paths: list[str]


@dataclass
class RetentionPolicyResult:
    apply: bool
    encrypted: CleanupResult
    decrypted: CleanupResult
    batches: CleanupResult
    media_cache: CleanupResult
    runtime: CleanupResult

    @property
    def bytes_removed(self) -> int:
        return sum(
            item.bytes_removed
            for item in (
                self.encrypted,
                self.decrypted,
                self.batches,
                self.media_cache,
                self.runtime,
            )
        )

    def to_dict(self, *, include_paths: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            'apply': self.apply,
            'bytes_removed': self.bytes_removed,
            'megabytes_removed': round(self.bytes_removed / 1024 / 1024, 2),
        }
        for name in ('encrypted', 'decrypted', 'batches', 'media_cache', 'runtime'):
            item = getattr(self, name)
            value = asdict(item)
            if not include_paths:
                value.pop('paths', None)
            payload[name] = value
        return payload


def _tree_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return int(path.stat().st_size)
        except OSError:
            return 0
    total = 0
    for item in path.rglob('*'):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _safe_remove(path: Path, *, apply: bool) -> int:
    size = _tree_bytes(path)
    if not apply:
        return size
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
    return size


def _state_protected_paths(project_root: Path, account: str) -> set[Path]:
    protected: set[Path] = set()
    state_root = project_root / 'data' / 'state' / account
    if not state_root.is_dir():
        return protected
    for state_file in state_root.rglob('*.json'):
        try:
            payload = json.loads(state_file.read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            continue
        for key in ('snapshot', 'decrypted'):
            raw = payload.get(key)
            if not raw:
                continue
            try:
                protected.add(Path(str(raw)).resolve())
            except OSError:
                continue
    return protected


def _prune_snapshot_layer(
    project_root: Path,
    account: str,
    layer: str,
    *,
    keep: int,
    apply: bool,
    protected: set[Path],
) -> CleanupResult:
    if keep < 1:
        raise ValueError('keep must be >= 1')
    account_root = project_root / 'data' / layer / account
    paths: list[str] = []
    seen = 0
    removed = 0
    bytes_removed = 0
    if not account_root.is_dir():
        return CleanupResult(apply, 0, 0, 0, [])

    account_resolved = account_root.resolve()
    for category in account_root.iterdir():
        if not category.is_dir() or category.is_symlink():
            continue
        groups: dict[str, list[Path]] = {}
        for child in category.iterdir():
            if not child.is_dir() or child.is_symlink():
                continue
            match = GENERATION_RE.match(child.name)
            if not match:
                continue
            groups.setdefault(match.group('name'), []).append(child)

        for entries in groups.values():
            entries.sort(key=lambda path: path.name, reverse=True)
            seen += len(entries)
            entry_resolved = {entry.resolve() for entry in entries}
            keep_set = {entry.resolve() for entry in entries[:keep]}
            keep_set.update(path for path in protected if path in entry_resolved)
            for stale in entries:
                resolved = stale.resolve()
                if resolved in keep_set:
                    continue
                try:
                    resolved.relative_to(account_resolved)
                except ValueError as exc:
                    raise RuntimeError('cleanup path escaped snapshot account root') from exc
                paths.append(str(resolved))
                bytes_removed += _safe_remove(stale, apply=apply)
                removed += 1

    return CleanupResult(
        apply=apply,
        generations_seen=seen,
        generations_removed=removed,
        bytes_removed=bytes_removed,
        paths=paths,
    )


def prune_snapshot_generations(
    project_root: Path,
    account: str,
    *,
    keep: int | None = None,
    encrypted_keep: int = 2,
    decrypted_keep: int = 1,
    apply: bool = False,
) -> CleanupResult:
    """Aggregate snapshot cleanup.

    keep is retained for older callers and applies to both layers. New callers
    should use encrypted_keep=2 and decrypted_keep=1.
    """
    if keep is not None:
        encrypted_keep = keep
        decrypted_keep = keep
    project_root = project_root.resolve()
    protected = _state_protected_paths(project_root, account)
    encrypted = _prune_snapshot_layer(
        project_root,
        account,
        'encrypted',
        keep=encrypted_keep,
        apply=apply,
        protected=protected,
    )
    decrypted = _prune_snapshot_layer(
        project_root,
        account,
        'decrypted',
        keep=decrypted_keep,
        apply=apply,
        protected=protected,
    )
    return CleanupResult(
        apply=apply,
        generations_seen=encrypted.generations_seen + decrypted.generations_seen,
        generations_removed=encrypted.generations_removed + decrypted.generations_removed,
        bytes_removed=encrypted.bytes_removed + decrypted.bytes_removed,
        paths=[*encrypted.paths, *decrypted.paths],
    )


def _job_referenced_batches(project_root: Path) -> set[Path]:
    referenced: set[Path] = set()
    jobs_root = project_root / 'data' / 'jobs'
    if not jobs_root.is_dir():
        return referenced
    for job_file in jobs_root.rglob('job.json'):
        try:
            payload = json.loads(job_file.read_text(encoding='utf-8'))
            directory = payload.get('batch', {}).get('directory')
            if directory:
                referenced.add(Path(str(directory)).resolve())
        except (OSError, ValueError, TypeError):
            continue
    return referenced


def prune_batches(
    project_root: Path,
    *,
    retention_days: int = 30,
    apply: bool = False,
) -> CleanupResult:
    if retention_days < 1:
        raise ValueError('retention_days must be >= 1')
    batches_root = project_root / 'data' / 'batches'
    if not batches_root.is_dir():
        return CleanupResult(apply, 0, 0, 0, [])
    protected = _job_referenced_batches(project_root)
    cutoff = time.time() - retention_days * 86400
    candidates: list[Path] = []
    seen = 0
    for child in batches_root.iterdir():
        if not child.is_dir() or child.is_symlink():
            continue
        seen += 1
        resolved = child.resolve()
        if resolved in protected:
            continue
        try:
            age_key = child.stat().st_mtime
        except OSError:
            continue
        if age_key < cutoff:
            candidates.append(child)

    paths: list[str] = []
    bytes_removed = 0
    for stale in candidates:
        paths.append(str(stale.resolve()))
        bytes_removed += _safe_remove(stale, apply=apply)

    return CleanupResult(
        apply=apply,
        generations_seen=seen,
        generations_removed=len(candidates),
        bytes_removed=bytes_removed,
        paths=paths,
    )


def prune_media_cache(
    project_root: Path,
    *,
    max_bytes: int = 2 * 1024 * 1024 * 1024,
    apply: bool = False,
) -> CleanupResult:
    if max_bytes < 0:
        raise ValueError('max_bytes must be >= 0')
    cache_root = project_root / 'data' / 'media-cache'
    if not cache_root.is_dir():
        return CleanupResult(apply, 0, 0, 0, [])

    files: list[tuple[float, Path, int]] = []
    total = 0
    for item in cache_root.rglob('*'):
        try:
            if not item.is_file() or item.is_symlink():
                continue
            stat = item.stat()
            size = int(stat.st_size)
            files.append((float(stat.st_mtime), item, size))
            total += size
        except OSError:
            continue

    if total <= max_bytes:
        return CleanupResult(apply, len(files), 0, 0, [])

    files.sort(key=lambda row: row[0])
    target_remove = total - max_bytes
    selected: list[tuple[Path, int]] = []
    selected_bytes = 0
    for _mtime, path, size in files:
        selected.append((path, size))
        selected_bytes += size
        if selected_bytes >= target_remove:
            break

    paths: list[str] = []
    bytes_removed = 0
    for stale, _size in selected:
        paths.append(str(stale.resolve()))
        bytes_removed += _safe_remove(stale, apply=apply)

    if apply:
        directories = [path for path in cache_root.rglob('*') if path.is_dir()]
        for directory in sorted(
            directories,
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass

    return CleanupResult(
        apply=apply,
        generations_seen=len(files),
        generations_removed=len(selected),
        bytes_removed=bytes_removed,
        paths=paths,
    )


def prune_runtime(
    project_root: Path,
    *,
    retention_days: int = 7,
    apply: bool = False,
) -> CleanupResult:
    if retention_days < 1:
        raise ValueError('retention_days must be >= 1')
    runtime_root = project_root / 'data' / 'runtime'
    if not runtime_root.is_dir():
        return CleanupResult(apply, 0, 0, 0, [])

    cutoff = time.time() - retention_days * 86400
    candidates: list[Path] = []
    seen = 0
    for profile in runtime_root.iterdir():
        if not profile.is_dir() or profile.is_symlink():
            continue
        seen += 1
        latest = 0.0
        try:
            latest = profile.stat().st_mtime
        except OSError:
            continue
        for item in profile.rglob('*'):
            try:
                latest = max(latest, item.stat().st_mtime)
            except OSError:
                continue
        if latest < cutoff:
            candidates.append(profile)

    paths: list[str] = []
    bytes_removed = 0
    for stale in candidates:
        paths.append(str(stale.resolve()))
        bytes_removed += _safe_remove(stale, apply=apply)

    return CleanupResult(
        apply=apply,
        generations_seen=seen,
        generations_removed=len(candidates),
        bytes_removed=bytes_removed,
        paths=paths,
    )


def run_retention_policy(
    project_root: Path,
    account: str,
    *,
    encrypted_keep: int = 2,
    decrypted_keep: int = 1,
    batch_days: int = 30,
    media_max_bytes: int = 2 * 1024 * 1024 * 1024,
    runtime_days: int = 7,
    apply: bool = False,
) -> RetentionPolicyResult:
    project_root = project_root.resolve()
    protected = _state_protected_paths(project_root, account)
    encrypted = _prune_snapshot_layer(
        project_root,
        account,
        'encrypted',
        keep=encrypted_keep,
        apply=apply,
        protected=protected,
    )
    decrypted = _prune_snapshot_layer(
        project_root,
        account,
        'decrypted',
        keep=decrypted_keep,
        apply=apply,
        protected=protected,
    )
    batches = prune_batches(
        project_root,
        retention_days=batch_days,
        apply=apply,
    )
    media = prune_media_cache(
        project_root,
        max_bytes=media_max_bytes,
        apply=apply,
    )
    runtime = prune_runtime(
        project_root,
        retention_days=runtime_days,
        apply=apply,
    )
    return RetentionPolicyResult(
        apply=apply,
        encrypted=encrypted,
        decrypted=decrypted,
        batches=batches,
        media_cache=media,
        runtime=runtime,
    )
