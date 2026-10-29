import json
from pathlib import Path

import noteford.jobs as jobs
from noteford.batch import BatchResult


def test_daily_job_is_idempotent_and_catches_up(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    index = project / 'data' / 'index' / 'wechat_index.db'
    index.parent.mkdir(parents=True)
    index.write_bytes(b'index')
    keys = project / 'secrets' / 'candidate_keys.json'
    keys.parent.mkdir(parents=True)
    keys.write_text('{}', encoding='utf-8')

    monkeypatch.setattr(jobs, 'refresh_account', lambda **kwargs: [])
    calls = []

    def fake_export(**kwargs):
        calls.append((kwargs['since'], kwargs['until']))
        folder = project / 'fake-batch' / str(len(calls))
        folder.mkdir(parents=True)
        messages = folder / 'messages.jsonl'
        conversations = folder / 'conversations.jsonl'
        manifest = folder / 'manifest.json'
        messages.write_text('{}\n', encoding='utf-8')
        conversations.write_text('{}\n', encoding='utf-8')
        manifest.write_text('{"message_count":1}', encoding='utf-8')
        return BatchResult(
            directory=str(folder),
            messages_file=str(messages),
            conversations_file=str(conversations),
            manifest_file=str(manifest),
            message_count=1,
            conversation_count=1,
            decode_errors=0,
            since=kwargs['since'],
            until=kwargs['until'],
        )

    monkeypatch.setattr(jobs, 'export_daily_batch', fake_export)

    first_now = 1_800_000_000
    first = jobs.run_daily_job(
        project_root=project,
        wechat_root=tmp_path / 'wechat',
        account='acct',
        candidates_path=keys,
        index_path=index,
        now=first_now,
    )
    assert not first.skipped
    assert first.job_id
    ready = Path(first.job_directory)
    assert ready.is_dir()
    assert not (project / 'data' / 'jobs' / 'Staging' / f'{first.job_id}.partial').exists()
    payload = json.loads((ready / 'job.json').read_text(encoding='utf-8'))
    assert payload['contract']['may_modify_wechat'] is False
    assert 'messages' not in payload

    repeated = jobs.run_daily_job(
        project_root=project,
        wechat_root=tmp_path / 'wechat',
        account='acct',
        candidates_path=keys,
        index_path=index,
        now=first_now + 60,
    )
    assert repeated.skipped
    assert repeated.reason == 'already_queued_today'
    assert len(calls) == 1

    next_now = first_now + 86400
    second = jobs.run_daily_job(
        project_root=project,
        wechat_root=tmp_path / 'wechat',
        account='acct',
        candidates_path=keys,
        index_path=index,
        now=next_now,
    )
    assert not second.skipped
    assert calls[1][0] == first_now + 1
    assert calls[1][1] == next_now

    forced = jobs.run_daily_job(
        project_root=project,
        wechat_root=tmp_path / 'wechat',
        account='acct',
        candidates_path=keys,
        index_path=index,
        now=next_now + 60,
        force=True,
    )
    assert not forced.skipped
    assert calls[2][0] == next_now + 1
    assert calls[2][1] == next_now + 60
    assert forced.job_id != second.job_id
    assert forced.job_id.startswith(second.job_id + '-')
    assert Path(forced.job_directory).is_dir()
