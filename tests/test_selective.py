import shutil
import sqlite3
from pathlib import Path

from noteford.metadata import ensure_metadata_schema
from noteford import selective
from noteford.archive import merge_message_archive


def _make_index(path: Path):
    con = sqlite3.connect(path)
    ensure_metadata_schema(con)
    with con:
        con.executemany(
            """INSERT INTO conversations(
                account, chat_table, username, display_name, is_group,
                session_type, last_timestamp, sort_timestamp
            ) VALUES(?,?,?,?,?,?,?,?)""",
            [
                ("acct", "Msg_target", "wxid_target", "Target", 0, 0, 10, 10),
                ("acct", "Msg_other", "wxid_other", "Other", 0, 0, 10, 10),
            ],
        )
        con.executemany(
            """INSERT INTO contacts(
                account, username, alias, remark, nick_name, display_name, is_group
            ) VALUES(?,?,?,?,?,?,?)""",
            [
                ("acct", "wxid_target", "", "", "Target", "Target", 0),
                ("acct", "wxid_other", "", "", "Other", "Other", 0),
                ("acct", "wxid_me", "", "", "Me", "Me", 0),
            ],
        )
    con.close()


def _make_source(path: Path):
    path.parent.mkdir(parents=True)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE Name2Id(user_name TEXT);
        INSERT INTO Name2Id(rowid,user_name) VALUES(1,'wxid_target');
        INSERT INTO Name2Id(rowid,user_name) VALUES(2,'wxid_me');

        CREATE TABLE Msg_target(
            local_id INTEGER, server_id TEXT, local_type INTEGER,
            sort_seq INTEGER, real_sender_id TEXT, create_time INTEGER,
            status INTEGER, message_content BLOB, compress_content BLOB,
            packed_info_data BLOB, WCDB_CT_message_content INTEGER, source BLOB
        );
        CREATE TABLE Msg_other(
            local_id INTEGER, server_id TEXT, local_type INTEGER,
            sort_seq INTEGER, real_sender_id TEXT, create_time INTEGER,
            status INTEGER, message_content BLOB, compress_content BLOB,
            packed_info_data BLOB, WCDB_CT_message_content INTEGER, source BLOB
        );
        """
    )
    con.executemany(
        "INSERT INTO Msg_target VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "t1", 1, 1, "1", 100, 0, b"target-one", b"", None, None, b""),
            (2, "t2", 1, 2, "2", 200, 0, b"target-two", b"", None, None, b""),
        ],
    )
    con.execute(
        "INSERT INTO Msg_other VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "o1", 1, 1, "1", 100, 0, b"other", b"", None, None, b""),
    )
    con.commit()
    con.close()


def _patch_plaintext_crypto(monkeypatch):
    def fake_snapshot(live_database, output_root, **_kwargs):
        folder = Path(output_root) / "snapshot"
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(live_database, folder / Path(live_database).name)
        return folder

    monkeypatch.setattr(selective, "copy_database_snapshot", fake_snapshot)
    monkeypatch.setattr(
        selective,
        "_match_or_scan",
        lambda *_args, **_kwargs: (b"x" * 32, "fingerprint"),
    )
    monkeypatch.setattr(
        selective,
        "decrypt_database",
        lambda source, destination, _key: shutil.copyfile(source, destination),
    )
    monkeypatch.setattr(selective, "merge_encrypted_wal", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(selective, "quick_check", lambda *_args, **_kwargs: True)


def test_selective_refresh_indexes_only_requested_conversation(tmp_path, monkeypatch):
    project = tmp_path / "project"
    index = project / "data" / "index" / "wechat_index.db"
    index.parent.mkdir(parents=True)
    _make_index(index)

    live = (
        tmp_path / "wechat" / "acct" / "db_storage"
        / "message" / "message_0.db"
    )
    _make_source(live)
    keys = project / "secrets" / "candidate_keys.json"
    keys.parent.mkdir(parents=True)
    keys.write_text('{"candidates":[]}', encoding="utf-8")

    _patch_plaintext_crypto(monkeypatch)

    result = selective.selective_refresh_conversation(
        wechat_root=tmp_path / "wechat",
        account="acct",
        project_root=project,
        candidates_path=keys,
        index_path=index,
        username="wxid_target",
        allow_memory_rescan=False,
    )

    assert result.source_dbs == ["message_0.db"]
    assert result.tables_found == 1
    assert result.inserted == 2

    con = sqlite3.connect(index)
    try:
        target = con.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_table='Msg_target'"
        ).fetchone()[0]
        other = con.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_table='Msg_other'"
        ).fetchone()[0]
        cursors = [
            row[0]
            for row in con.execute(
                "SELECT chat_table FROM cursors ORDER BY chat_table"
            )
        ]
    finally:
        con.close()

    assert target == 2
    assert other == 0
    assert cursors == ["Msg_target"]

    work = project / "data" / "runtime" / "selective"
    assert work.is_dir()
    assert list(work.iterdir()) == []


def test_follow_pause_state_is_persistent(tmp_path, monkeypatch):
    project = tmp_path / "project"
    index = project / "data" / "index" / "wechat_index.db"
    index.parent.mkdir(parents=True)
    _make_index(index)

    live = (
        tmp_path / "wechat" / "acct" / "db_storage"
        / "message" / "message_0.db"
    )
    _make_source(live)
    keys = project / "secrets" / "candidate_keys.json"
    keys.parent.mkdir(parents=True)
    keys.write_text('{"candidates":[]}', encoding="utf-8")
    _patch_plaintext_crypto(monkeypatch)

    followed = selective.follow_conversation(
        wechat_root=tmp_path / "wechat",
        account="acct",
        project_root=project,
        candidates_path=keys,
        index_path=index,
        username="wxid_target",
        allow_memory_rescan=False,
    )
    assert followed["selection"]["state"] == "following"
    assert followed["selection"]["local_message_count"] == 2

    paused = selective.pause_conversation(index, "acct", "wxid_target")
    assert paused["state"] == "paused"
    assert paused["local_message_count"] == 2

    rows = selective.list_selections(index, "acct")
    assert len(rows) == 1
    assert rows[0]["username"] == "wxid_target"
    assert rows[0]["state"] == "paused"


def test_later_follow_recovers_unselected_message_missing_from_live_db(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    index = project / 'data' / 'index' / 'wechat_index.db'
    index.parent.mkdir(parents=True)
    _make_index(index)
    live = tmp_path / 'wechat' / 'acct' / 'db_storage' / 'message' / 'message_0.db'
    _make_source(live)
    archive = project / 'data' / 'archive' / 'acct' / live.name
    merge_message_archive(live, archive)
    con = sqlite3.connect(live)
    con.execute('DELETE FROM Msg_other')
    con.commit()
    con.close()
    monkeypatch.setattr(
        selective, 'copy_database_snapshot',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('used live database')),
    )

    result = selective.follow_conversation(
        wechat_root=tmp_path / 'wechat',
        account='acct',
        project_root=project,
        candidates_path=project / 'secrets' / 'unused.json',
        index_path=index,
        username='wxid_other',
        allow_memory_rescan=False,
    )
    assert result['selection']['local_message_count'] == 1


def test_followed_archive_indexes_late_low_local_id(tmp_path):
    project = tmp_path / 'project'
    index = project / 'data' / 'index' / 'wechat_index.db'
    index.parent.mkdir(parents=True)
    _make_index(index)
    live = tmp_path / 'wechat' / 'acct' / 'db_storage' / 'message' / 'message_0.db'
    _make_source(live)
    con = sqlite3.connect(live)
    con.execute('DELETE FROM Msg_target')
    con.execute(
        'INSERT INTO Msg_target VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (1000, 'high', 1, 1000, '1', 1000, 0, b'high', b'', None, None, b''),
    )
    con.commit()
    con.close()
    archive = project / 'data' / 'archive' / 'acct' / live.name
    merge_message_archive(live, archive)
    selective.follow_conversation(
        wechat_root=tmp_path / 'wechat', account='acct', project_root=project,
        candidates_path=project / 'unused', index_path=index, username='wxid_target',
    )

    con = sqlite3.connect(live)
    con.execute(
        'INSERT INTO Msg_target VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (1, 'low', 1, 1, '1', 1, 0, b'late', b'', None, None, b''),
    )
    con.commit()
    con.close()
    next_source = tmp_path / 'next' / live.name
    next_source.parent.mkdir()
    shutil.copyfile(live, next_source)
    merge_message_archive(next_source, archive)

    selective.selective_refresh_conversation(
        wechat_root=tmp_path / 'wechat', account='acct', project_root=project,
        candidates_path=project / 'unused', index_path=index, username='wxid_target',
        overlap_local_ids=200,
    )
    assert selective.selection_status(index, 'acct', 'wxid_target')['local_message_count'] == 2
