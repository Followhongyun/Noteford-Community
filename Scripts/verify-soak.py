"""Read-only row/content and retained-media audit after benchmark-soak.py."""
import argparse
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from noteford.downloaded_media import retained_download

parser = argparse.ArgumentParser()
parser.add_argument('fixture_root',type=Path)
parser.add_argument('result_directory',type=Path)
args=parser.parse_args()
root=args.fixture_root.resolve()
output=args.result_directory.resolve()
account='wxid_fixture_1234'
producer=json.loads((root/'producer-status.json').read_text())
assert producer.get('done'), 'Producer must finish before the audit'
info=json.loads((root/'media-info.json').read_text())
project=root/'project'
index=project/'index.db'
with closing(sqlite3.connect(index.resolve().as_uri()+'?mode=ro',uri=True)) as con:
    con.execute('ATTACH DATABASE ? AS source',((root/'producer/message_0.db').resolve().as_uri()+'?mode=ro',))
    tables=[row[0] for row in con.execute("SELECT name FROM source.sqlite_master WHERE name LIKE 'Msg_%'")]
    mismatched=source_rows=0
    for table in tables:
        assert table.startswith('Msg_') and all(c in '0123456789abcdef' for c in table[4:])
        source_rows+=con.execute(f'SELECT count(*) FROM source."{table}"').fetchone()[0]
        mismatched+=con.execute(f'''SELECT count(*) FROM source."{table}" s
            LEFT JOIN main.messages m ON m.account=? AND m.source_db='message_0.db'
            AND m.chat_table=? AND m.local_id=s.local_id
            WHERE m.uid IS NULL OR m.server_id IS NOT CAST(s.server_id AS TEXT)
              OR m.local_type IS NOT s.local_type OR m.create_time IS NOT s.create_time
              OR m.message_content IS NOT s.message_content''',(account,table)).fetchone()[0]
    indexed=con.execute('SELECT count(*) FROM messages WHERE account=?',(account,)).fetchone()[0]
    index_check=[row[0] for row in con.execute('PRAGMA main.quick_check')]
    source_check=[row[0] for row in con.execute('PRAGMA source.quick_check')]
valid=invalid=0
with closing(sqlite3.connect((project/'data/mac_automatic_media.sqlite').resolve().as_uri()+'?mode=ro',uri=True)) as con:
    jobs=con.execute("SELECT uid FROM jobs WHERE account=? AND status='ready'",(account,)).fetchall()
    for (uid,) in jobs:
        artifact=retained_download(project/'data/media-cache',account,uid,'image')
        if artifact is None:
            invalid+=1
            continue
        with artifact[0].open('rb') as handle:
            good=hashlib.file_digest(handle,'md5').hexdigest()==info['md5']
        valid+=int(good)
        invalid+=int(not good)
final=json.loads((output/'final.json').read_text())
result=dict(source_rows=source_rows,indexed_rows=indexed,mismatched_rows=mismatched,
    source_quick_check=source_check,index_quick_check=index_check,media_ready_jobs=len(jobs),
    media_cache_valid=valid,media_cache_invalid=invalid,
    network_bytes=final['network_bytes'],expected_network_bytes=producer['media_sent']*info['encrypted_bytes'],
    media_source_count=producer['media_sent'],
    counts_match=source_rows==indexed==producer['sent'],
    no_redundant_transfer=final['network_bytes']==producer['media_sent']*info['encrypted_bytes'])
(output/'integrity.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result))
assert result['counts_match'] and mismatched==0 and invalid==0
assert index_check==['ok'] and source_check==['ok']
assert valid==producer['media_sent'] and result['no_redundant_transfer']
