import json
import sqlite3
from pathlib import Path

from noteford.batch import export_daily_batch
from noteford.indexer import _connect_index
from noteford.metadata import ensure_metadata_schema


def test_daily_batch_exports_private_names_only_to_jsonl(tmp_path):
    index = tmp_path / 'index.db'
    con = _connect_index(index)
    ensure_metadata_schema(con)
    with con:
        con.execute(
            '''INSERT INTO conversations(account,chat_table,username,display_name,is_group)
               VALUES(?,?,?,?,?)''',
            ('acct','Msg_demo','wxid_friend','Secret Friend',0),
        )
        con.execute(
            '''INSERT INTO contacts(account,username,display_name,is_group)
               VALUES(?,?,?,?)''',
            ('acct','wxid_friend','Secret Friend',0),
        )
        con.execute(
            '''INSERT INTO sender_map(account,source_db,sender_rowid,username)
               VALUES(?,?,?,?)''',
            ('acct','message_0.db',7,'wxid_friend'),
        )
        con.execute(
            '''
            INSERT INTO messages(
                uid,account,source_db,chat_table,local_id,server_id,local_type,
                sort_seq,real_sender_id,create_time,status,message_content,
                compress_content,source,row_hash,indexed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''',
            ('u1','acct','message_0.db','Msg_demo',1,'101',1,1,'7',1000,0,
             b'hello',b'',b'','hash','now'),
        )
    con.close()

    result = export_daily_batch(index,'acct',900,1100,tmp_path/'batches')
    line = Path(result.messages_file).read_text(encoding='utf-8').strip()
    item = json.loads(line)
    assert item['chat']['name'] == 'Secret Friend'
    assert item['text'] == 'hello'

    manifest_text = Path(result.manifest_file).read_text(encoding='utf-8')
    assert 'Secret Friend' not in manifest_text
    assert json.loads(manifest_text)['message_count'] == 1

    packet = json.loads(Path(result.conversations_file).read_text(encoding='utf-8').strip())
    assert packet['chat']['name'] == 'Secret Friend'
    assert packet['message_count'] == 1
    assert packet['messages'][0]['text'] == 'hello'
