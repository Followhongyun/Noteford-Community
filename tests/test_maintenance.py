import json
import os
import time
from pathlib import Path

from noteford.maintenance import (
    prune_batches,
    prune_media_cache,
    prune_runtime,
    prune_snapshot_generations,
    run_retention_policy,
)


def test_cleanup_keeps_recent_generations_and_is_dry_run_by_default(tmp_path):
    project = tmp_path / 'project'
    encrypted = project / 'data' / 'encrypted' / 'acct' / 'message'
    decrypted = project / 'data' / 'decrypted' / 'acct' / 'message'
    names = [
        'message_0-20260918-120000',
        'message_0-20260919-120000',
        'message_0-20260920-120000',
        'message_0-20260921-120000',
    ]
    for root in (encrypted, decrypted):
        for index, name in enumerate(names):
            folder = root / name
            folder.mkdir(parents=True)
            (folder / 'message_0.db').write_bytes(bytes([index]) * 10)

    dry = prune_snapshot_generations(
        project,
        'acct',
        encrypted_keep=2,
        decrypted_keep=1,
        apply=False,
    )
    assert dry.generations_seen == 8
    assert dry.generations_removed == 5
    assert all((encrypted / name).is_dir() for name in names)
    assert all((decrypted / name).is_dir() for name in names)

    applied = prune_snapshot_generations(
        project,
        'acct',
        encrypted_keep=2,
        decrypted_keep=1,
        apply=True,
    )
    assert applied.generations_removed == 5
    assert not (encrypted / names[0]).exists()
    assert not (encrypted / names[1]).exists()
    assert (encrypted / names[2]).is_dir()
    assert (encrypted / names[3]).is_dir()

    assert not (decrypted / names[0]).exists()
    assert not (decrypted / names[1]).exists()
    assert not (decrypted / names[2]).exists()
    assert (decrypted / names[3]).is_dir()


def test_snapshot_cleanup_protects_state_referenced_generation(tmp_path):
    project = tmp_path / 'project'
    root = project / 'data' / 'decrypted' / 'acct' / 'message'
    old = root / 'message_0-20260918-120000'
    newest = root / 'message_0-20260921-120000'
    for folder in (old, newest):
        folder.mkdir(parents=True)
        (folder / 'message_0.db').write_bytes(b'x' * 10)

    state = project / 'data' / 'state' / 'acct' / 'message' / 'message_0.json'
    state.parent.mkdir(parents=True)
    state.write_text(
        json.dumps({'decrypted': str(old.resolve())}),
        encoding='utf-8',
    )

    result = prune_snapshot_generations(
        project,
        'acct',
        encrypted_keep=2,
        decrypted_keep=1,
        apply=True,
    )
    assert result.generations_removed == 0
    assert old.is_dir()
    assert newest.is_dir()


def test_batches_expire_but_ready_job_reference_is_protected(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    batches = project / 'data' / 'batches'
    stale = batches / 'batch-old'
    protected = batches / 'batch-protected'
    fresh = batches / 'batch-fresh'
    for folder in (stale, protected, fresh):
        folder.mkdir(parents=True)
        (folder / 'manifest.json').write_text('{}', encoding='utf-8')

    old_time = time.time() - 60 * 86400
    os.utime(stale, (old_time, old_time))
    os.utime(protected, (old_time, old_time))

    job = project / 'data' / 'jobs' / 'Ready' / 'job-1' / 'job.json'
    job.parent.mkdir(parents=True)
    job.write_text(
        json.dumps({'batch': {'directory': str(protected.resolve())}}),
        encoding='utf-8',
    )

    result = prune_batches(project, retention_days=30, apply=True)
    assert result.generations_removed == 1
    assert not stale.exists()
    assert protected.is_dir()
    assert fresh.is_dir()


def test_media_cache_prunes_oldest_until_under_limit(tmp_path):
    project = tmp_path / 'project'
    cache = project / 'data' / 'media-cache'
    cache.mkdir(parents=True)
    files = []
    for index in range(4):
        path = cache / f'{index}.bin'
        path.write_bytes(bytes([index]) * 10)
        stamp = 1000 + index
        os.utime(path, (stamp, stamp))
        files.append(path)

    result = prune_media_cache(project, max_bytes=25, apply=True)
    assert result.generations_removed == 2
    assert not files[0].exists()
    assert not files[1].exists()
    assert files[2].exists()
    assert files[3].exists()


def test_runtime_prunes_only_stale_profiles(tmp_path):
    project = tmp_path / 'project'
    runtime = project / 'data' / 'runtime'
    old = runtime / 'old-profile'
    current = runtime / 'current-profile'
    for folder in (old, current):
        folder.mkdir(parents=True)
        (folder / 'cache.bin').write_bytes(b'x')

    old_time = time.time() - 20 * 86400
    os.utime(old / 'cache.bin', (old_time, old_time))
    os.utime(old, (old_time, old_time))

    result = prune_runtime(project, retention_days=7, apply=True)
    assert result.generations_removed == 1
    assert not old.exists()
    assert current.is_dir()


def test_retention_policy_combines_all_layers(tmp_path):
    project = tmp_path / 'project'
    encrypted = project / 'data' / 'encrypted' / 'acct' / 'message'
    decrypted = project / 'data' / 'decrypted' / 'acct' / 'message'
    for root in (encrypted, decrypted):
        for stamp in ('20260918-120000', '20260919-120000', '20260920-120000'):
            folder = root / f'message_0-{stamp}'
            folder.mkdir(parents=True)
            (folder / 'message_0.db').write_bytes(b'x' * 10)

    result = run_retention_policy(
        project,
        'acct',
        encrypted_keep=2,
        decrypted_keep=1,
        batch_days=30,
        media_max_bytes=100,
        runtime_days=7,
        apply=False,
    )
    assert result.encrypted.generations_removed == 1
    assert result.decrypted.generations_removed == 2
    assert result.bytes_removed == 30
