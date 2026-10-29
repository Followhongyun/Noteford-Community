import sqlite3

from noteford.archive import merge_message_archive


def test_archive_keeps_unfollowed_rows_after_source_deletion(tmp_path):
    source = tmp_path / 'snapshot' / 'message_0.db'
    source.parent.mkdir()
    con = sqlite3.connect(source)
    con.executescript('''
        CREATE TABLE Msg_target(local_id INTEGER, server_id TEXT, create_time INTEGER, message_content BLOB);
        CREATE TABLE Msg_other(local_id INTEGER, server_id TEXT, create_time INTEGER, message_content BLOB);
        CREATE TABLE Name2Id(user_name TEXT);
        INSERT INTO Name2Id(rowid,user_name) VALUES(2,'sender');
        INSERT INTO Msg_target VALUES(1,'t1',100,X'6F6C64');
        INSERT INTO Msg_other VALUES(1,'o1',100,X'6B656570');
    ''')
    con.close()
    archive = tmp_path / 'archive' / 'message_0.db'

    first = merge_message_archive(source, archive)
    assert first.tables == 2
    assert first.rows_seen == 2
    assert first.rows_changed == 2
    assert merge_message_archive(source, archive).skipped

    con = sqlite3.connect(source)
    con.executescript('''
        DELETE FROM Msg_other WHERE local_id=1;
        UPDATE Msg_target SET message_content=X'6E6577' WHERE local_id=1;
        INSERT INTO Msg_target VALUES(2,'t2',200,X'616464');
    ''')
    con.close()
    # A fresh decrypted generation represents the next hourly snapshot.
    next_source = tmp_path / 'next' / source.name
    next_source.parent.mkdir()
    source.replace(next_source)
    second = merge_message_archive(next_source, archive)
    assert second.rows_seen == 2
    assert second.rows_changed == 2

    con = sqlite3.connect(archive)
    try:
        assert con.execute('SELECT local_id,message_content FROM Msg_target ORDER BY local_id').fetchall() == [
            (1, b'new'), (2, b'add')
        ]
        assert con.execute('SELECT message_content FROM Msg_other').fetchone()[0] == b'keep'
        assert con.execute('SELECT rowid,user_name FROM Name2Id').fetchone() == (2, 'sender')
    finally:
        con.close()
