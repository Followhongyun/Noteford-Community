import sqlite3
from pathlib import Path
import pytest

from noteford.indexer import index_messages, index_status


def _make_source(path: Path):
    con = sqlite3.connect(path)
    con.execute(
        '''
        CREATE TABLE Msg_demo(
            local_id INTEGER, server_id INTEGER, local_type INTEGER, sort_seq INTEGER,
            real_sender_id INTEGER, create_time INTEGER, status INTEGER,
            message_content BLOB, compress_content BLOB, source BLOB
        )
        '''
    )
    con.executemany(
        'INSERT INTO Msg_demo VALUES(?,?,?,?,?,?,?,?,?,?)',
        [
            (1, 101, 1, 1, 7, 1000, 0, b'hello', b'', b''),
            (2, 102, 1, 2, 8, 1001, 0, b'world', b'', b''),
        ],
    )
    con.commit()
    con.close()


def test_index_is_idempotent_and_incremental(tmp_path):
    source = tmp_path / 'message_0.db'
    index = tmp_path / 'index.db'
    _make_source(source)

    first = index_messages(source, index, 'acct', overlap_local_ids=10)
    assert first.inserted == 2
    assert first.updated == 0

    second = index_messages(source, index, 'acct', overlap_local_ids=10)
    assert second.inserted == 0
    assert second.updated == 0
    assert second.unchanged == 2

    con = sqlite3.connect(source)
    con.execute("UPDATE Msg_demo SET message_content=? WHERE server_id=102", (b'changed',))
    con.execute(
        'INSERT INTO Msg_demo VALUES(?,?,?,?,?,?,?,?,?,?)',
        (3, 103, 1, 3, 9, 1002, 0, b'new', b'', b''),
    )
    con.commit()
    con.close()

    third = index_messages(source, index, 'acct', overlap_local_ids=10)
    assert third.inserted == 1
    assert third.updated == 1
    assert third.unchanged == 1

    status = index_status(index, 'acct')
    assert status['messages'] == 3
    assert status['tables'] == 1
    assert status['latest_create_time'] == 1002


@pytest.mark.parametrize('old_server,new_server,new_type', [(0, 201, 1), (101, 301, 25769803825)])
def test_in_place_server_id_change_keeps_uid_and_allows_new_messages(tmp_path, old_server, new_server, new_type):
    source, index = tmp_path / 'message_0.db', tmp_path / 'index.db'
    _make_source(source)
    with sqlite3.connect(source) as con:
        con.execute('UPDATE Msg_demo SET server_id=? WHERE local_id=1', (old_server,))
    index_messages(source, index, 'acct')
    index_messages(source, index, 'another-account')
    with sqlite3.connect(index) as con:
        uid = con.execute("SELECT uid FROM messages WHERE account='acct' AND local_id=1").fetchone()[0]
        other = con.execute("SELECT uid,row_hash FROM messages WHERE account='another-account' ORDER BY local_id").fetchall()
        checkpoint = con.execute('SELECT MAX(sequence) FROM mcp_changes').fetchone()[0]
    with sqlite3.connect(source) as con:
        con.execute('UPDATE Msg_demo SET server_id=?,local_type=?,message_content=? WHERE local_id=1',
                    (new_server, new_type, b'updated message'))
        con.execute('INSERT INTO Msg_demo VALUES(?,?,?,?,?,?,?,?,?,?)', (3, 303, 1, 3, 9, 1002, 0, b'new message', b'', b''))
    stats = index_messages(source, index, 'acct')
    assert (stats.inserted, stats.updated, stats.unchanged) == (1, 1, 1)
    with sqlite3.connect(index) as con:
        row = con.execute('SELECT server_id,local_type,message_content FROM messages WHERE uid=?', (uid,)).fetchone()
        assert row == (str(new_server), new_type, b'updated message')
        assert con.execute("SELECT COUNT(*) FROM messages WHERE account='acct'").fetchone()[0] == 3
        assert con.execute("SELECT uid,row_hash FROM messages WHERE account='another-account' ORDER BY local_id").fetchall() == other
        assert con.execute('SELECT kind FROM mcp_changes WHERE sequence>? AND uid=?', (checkpoint, uid)).fetchall() == [('upsert',)]
    repeated = index_messages(source, index, 'acct')
    assert (repeated.inserted, repeated.updated, repeated.unchanged) == (0, 0, 3)


def test_local_id_reuse_with_different_time_preserves_existing_history(tmp_path):
    source, index = tmp_path / 'message_0.db', tmp_path / 'index.db'
    _make_source(source)
    index_messages(source, index, 'acct')
    with sqlite3.connect(index) as con:
        previous = con.execute('SELECT * FROM messages ORDER BY uid').fetchall()
    with sqlite3.connect(source) as con:
        con.execute('UPDATE Msg_demo SET server_id=9001,create_time=9000 WHERE local_id=1')
    with pytest.raises(sqlite3.IntegrityError):
        index_messages(source, index, 'acct')
    with sqlite3.connect(index) as con:
        assert con.execute('SELECT * FROM messages ORDER BY uid').fetchall() == previous
