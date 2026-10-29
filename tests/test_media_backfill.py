import sqlite3
from datetime import date, datetime

from noteford.indexer import ensure_index_schema
from noteford.media import MediaArtifact
from noteford.media_backfill import local_day_window, prefetch_recent_media
from noteford.metadata import ensure_metadata_schema


class VideoResolver:
    def __init__(self):
        self.calls = []

    def export_video(self, timestamp, md5, uid, destination_root):
        self.calls.append((timestamp, uid))
        destination_root.mkdir(parents=True, exist_ok=True)
        path = destination_root / (uid + '.mp4')
        path.write_bytes(b'v')
        return MediaArtifact('ready', 'video', str(path), md5, 1, False)


def test_prefetch_uses_exact_local_days_and_only_following(tmp_path):
    index = tmp_path / 'index.db'
    ensure_index_schema(index)
    con = sqlite3.connect(index)
    ensure_metadata_schema(con)
    with con:
        con.executemany(
            '''INSERT INTO conversations(account,chat_table,username,display_name,is_group,
                   session_type,last_timestamp,sort_timestamp) VALUES(?,?,?,?,?,?,?,?)''',
            [('acct', 'Msg_follow', 'follow', 'Follow', 0, 0, 0, 0),
             ('acct', 'Msg_other', 'other', 'Other', 0, 0, 0, 0)],
        )
        con.execute(
            '''INSERT INTO conversation_selection(account,username,chat_table,state,
                   source_dbs,selected_at,updated_at) VALUES(?,?,?,?,?,?,?)''',
            ('acct', 'follow', 'Msg_follow', 'following', '[]', 'now', 'now'),
        )
        since, until = local_day_window(today=date(2026, 9, 27))
        con.executemany(
            '''INSERT INTO messages(uid,account,source_db,chat_table,local_id,server_id,
                   local_type,create_time,row_hash,indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            [('inside', 'acct', 'message_0.db', 'Msg_follow', 1, '1', 43, since, 'v1', 'now'),
             ('before', 'acct', 'message_0.db', 'Msg_follow', 2, '2', 43, since - 1, 'v1', 'now'),
             ('after', 'acct', 'message_0.db', 'Msg_follow', 3, '3', 43, until, 'v1', 'now'),
             ('unfollowed', 'acct', 'message_0.db', 'Msg_other', 1, '4', 43, since, 'v1', 'now')],
        )
    con.close()
    resolver = VideoResolver()

    first = prefetch_recent_media(
        index_path=index, account='acct', resolver=resolver,
        destination_root=tmp_path / 'media', today=date(2026, 9, 27),
    )
    assert datetime.fromtimestamp(first['since']).date() == date(2026, 9, 14)
    assert datetime.fromtimestamp(first['until_exclusive']).date() == date(2026, 9, 28)
    assert first['attempted'] == 1
    assert resolver.calls == [(since, 'inside')]

    second = prefetch_recent_media(
        index_path=index, account='acct', resolver=resolver,
        destination_root=tmp_path / 'media', today=date(2026, 9, 27),
    )
    assert second['attempted'] == 0

    (tmp_path / 'media' / 'inside.mp4').unlink()
    missing_cache = prefetch_recent_media(
        index_path=index, account='acct', resolver=resolver,
        destination_root=tmp_path / 'media', today=date(2026, 9, 27),
    )
    assert missing_cache['attempted'] == 1

    con = sqlite3.connect(index)
    con.execute("UPDATE messages SET row_hash='v2' WHERE uid='inside'")
    con.commit()
    con.close()
    updated = prefetch_recent_media(
        index_path=index, account='acct', resolver=resolver,
        destination_root=tmp_path / 'media', today=date(2026, 9, 27),
    )
    assert updated['attempted'] == 1

    # Per-conversation ranges change the actual exports, including both boundaries.
    from noteford.media_ranges import MediaRanges
    ranges = MediaRanges(tmp_path, '2026-09-27T12:00:00', today=date(2026, 9, 28))
    ranges.save('follow', '2026-09-13', '2026-09-13')
    custom = prefetch_recent_media(
        index_path=index, account='acct', resolver=resolver,
        destination_root=tmp_path / 'media', ranges=ranges,
    )
    assert custom['attempted'] == 1
    assert resolver.calls[-1] == (since - 1, 'before')
    ranges.save('follow', '2026-09-28', None)
    rolling = prefetch_recent_media(
        index_path=index, account='acct', resolver=resolver,
        destination_root=tmp_path / 'media', ranges=ranges,
    )
    assert rolling['attempted'] == 1
    assert resolver.calls[-1] == (until, 'after')
    assert (tmp_path / 'media' / 'inside.mp4').exists()
