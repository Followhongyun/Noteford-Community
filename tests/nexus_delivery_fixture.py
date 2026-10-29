"""Temporary protocol test process; never opens WeChat or production data."""
import sys
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
from test_nexus_outbox import managed, FILEHELPER
from noteford.indexer import index_messages
from noteford.nexus_outbox import pending
from noteford.service import make_handler

root = Path(sys.argv[1]).resolve()
root.mkdir(parents=True, exist_ok=True)
managed(root)
index = root / 'data/index/messages.db'
source = root / 'message_fixture.db'
with sqlite3.connect(source) as con:
    for table in (FILEHELPER, 'Msg_unrelated'):
        con.execute(f'CREATE TABLE {table}(local_id INTEGER,server_id INTEGER,local_type INTEGER,create_time INTEGER,message_content BLOB)')
registry = SimpleNamespace(list=lambda: [SimpleNamespace(id='fixture-library')], context=lambda id: SimpleNamespace(index_path=index))
server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(SimpleNamespace(root_config={'project_root': str(root)}, registry=registry), 'primary-fixture-token'))
threading.Thread(target=server.serve_forever, daemon=True).start()
print(json.dumps({'port': server.server_port}), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    try:
        if request['action'] == 'index':
            with sqlite3.connect(source) as con:
                table = FILEHELPER if request.get('filehelper', True) else 'Msg_unrelated'
                id = request['id']
                content = request['content']
                con.execute(f'INSERT OR REPLACE INTO {table} VALUES(?,?,?,?,?)', (id, 1000 + id, request.get('local_type', 1), request['time'], content.encode()))
            import noteford.nexus_outbox as outbox
            notify = outbox.notify_committed
            try:
                if request.get('notify') is False:
                    outbox.notify_committed = lambda scope: None
                result = index_messages(source, index, 'wechat-fixture')
            finally:
                outbox.notify_committed = notify
            value = {'inserted': result.inserted}
        elif request['action'] == 'pending':
            value = {'count': len(pending(index, request['owner']))}
        else:
            raise ValueError('unsupported fixture action')
        print(json.dumps({'ok': True, **value}), flush=True)
    except Exception:
        print('{"ok":false}', flush=True)
server.shutdown(); server.server_close()
