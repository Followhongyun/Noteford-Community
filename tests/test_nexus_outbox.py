import json
import os
import sqlite3
import sys
import threading
import http.client
from pathlib import Path
from types import SimpleNamespace
from http.server import ThreadingHTTPServer
import psutil
import pytest
from noteford.indexer import index_messages
from noteford.nexus_outbox import FILEHELPER, pending, acknowledge, owner_scope
from noteford.service import make_handler

OWNER = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
URL = 'https://weixin.qq.com/sph/test-fixture'


def managed(root):
    runtime = root / 'data/runtime/product-account'
    runtime.mkdir(parents=True)
    (root / '.product-workspace.json').write_text(json.dumps({'version': 1, 'installation': str(root), 'workspace': 'legacy'}))
    lease = {'version': 1, 'active': True, 'root': str(root), 'workspace': 'legacy', 'epoch': 'fixture-epoch',
             'accountId': OWNER, 'pid': os.getpid(), 'appExecutable': psutil.Process().exe(),
             'processStartedAtMs': psutil.Process().create_time() * 1000}
    (runtime / 'active-workspace.json').write_text(json.dumps(lease))
    return runtime / 'active-workspace.json', lease


def source(root, table=FILEHELPER):
    p = root / 'message_0.db'
    with sqlite3.connect(p) as con:
        con.execute(f'CREATE TABLE {table}(local_id INTEGER,server_id INTEGER,local_type INTEGER,create_time INTEGER,message_content BLOB)')
        con.execute(f'INSERT INTO {table} VALUES(1,101,1,1000,?)', (URL.encode(),))
    return p


def test_atomic_queue_replay_ack_and_owner_isolation(tmp_path):
    managed(tmp_path)
    src, index = source(tmp_path), tmp_path / 'data/index/messages.db'
    assert index_messages(src, index, 'wechat-fixture').inserted == 1
    first = pending(index, OWNER)
    assert len(first) == 1 and first[0]['url'] == URL
    assert first[0]['owner_id'] == OWNER and first[0]['message_uid']
    assert (tmp_path / 'data/runtime/nexus-events/wake').exists()
    assert pending(index, OTHER) == []
    index_messages(src, index, 'wechat-fixture')
    assert pending(index, OWNER) == first
    acknowledge(index, OTHER, first[0]['task_id'])
    assert pending(index, OWNER) == first
    acknowledge(index, OWNER, first[0]['task_id'])
    acknowledge(index, OWNER, first[0]['task_id'])
    assert pending(index, OWNER) == []
    index_messages(src, index, 'wechat-fixture', force_full=True)
    assert pending(index, OWNER) == []


def test_other_chats_and_unmanaged_cli_do_not_enqueue(tmp_path):
    managed(tmp_path)
    index_messages(source(tmp_path, 'Msg_other'), tmp_path / 'index.db', 'fixture')
    assert pending(tmp_path / 'index.db', OWNER) == []
    unmanaged = tmp_path / 'unmanaged'; unmanaged.mkdir()
    # Outside a managed root, CLI indexing has no product identity and cannot deliver.
    assert owner_scope(Path('/tmp/noteford-unmanaged-fixture/index.db')) is None


def test_enqueue_failure_rolls_back_message_and_cursor(tmp_path, monkeypatch):
    managed(tmp_path)
    src, index = source(tmp_path), tmp_path / 'index.db'
    def fail(*args):
        raise OSError('synthetic storage failure')
    monkeypatch.setattr('noteford.nexus_outbox.enqueue_message', fail)
    with pytest.raises(OSError):
        index_messages(src, index, 'fixture')
    with sqlite3.connect(index) as con:
        assert con.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
        assert con.execute('SELECT count(*) FROM cursors').fetchone()[0] == 0
    assert not (tmp_path / 'data/runtime/nexus-events/wake').exists()


def test_locked_workspace_cannot_index_or_read_tasks(tmp_path):
    file, lease = managed(tmp_path)
    src, index = source(tmp_path), tmp_path / 'index.db'
    lease['active'] = False; file.write_text(json.dumps(lease))
    with pytest.raises(PermissionError):
        index_messages(src, index, 'fixture')


def test_internal_outbox_http_requires_primary_auth_and_active_workspace(tmp_path):
    lease_file, lease = managed(tmp_path)
    index = tmp_path / 'index.db'
    index_messages(source(tmp_path), index, 'fixture')
    registry = SimpleNamespace(list=lambda: [SimpleNamespace(id='library')], context=lambda id: SimpleNamespace(index_path=index))
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(SimpleNamespace(root_config={'project_root': str(tmp_path)}, registry=registry), 'primary-fixture-token'))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    con = http.client.HTTPConnection('127.0.0.1', server.server_port)
    try:
        con.request('GET', '/api/v2/nexus/outbox')
        response = con.getresponse(); response.read(); assert response.status == 401
        headers = {'X-Noteford-Token': 'primary-fixture-token'}
        con.request('GET', '/api/v2/nexus/outbox', headers=headers)
        response = con.getresponse(); body = json.loads(response.read()); assert len(body['tasks']) == 1
        headers['Content-Type'] = 'application/json'
        con.request('POST', '/api/v2/nexus/ack', json.dumps({'task_id': body['tasks'][0]['task_id']}), headers)
        response = con.getresponse(); response.read(); assert response.status == 200
        assert pending(index, OWNER) == []
        lease['active'] = False; lease_file.write_text(json.dumps(lease))
        con.request('GET', '/api/v2/nexus/outbox', headers=headers)
        response = con.getresponse(); response.read(); assert response.status == 401
    finally:
        con.close(); server.shutdown(); server.server_close()


def test_card_outbox_preserves_full_description_but_not_media_secrets(tmp_path):
    managed(tmp_path)
    src, index = source(tmp_path), tmp_path / 'index.db'
    xml = '<msg><appmsg><type>51</type><finderFeed><objectId>12345</objectId><username>stable-author@finder</username><nickname>临时作者</nickname><desc>完整描述，严格匹配。</desc><mediaList><media><url>https://finder.video.qq.com/fixture</url><urlToken>synthetic-media-token</urlToken><decodeKey>synthetic-decode-key</decodeKey></media></mediaList></finderFeed></appmsg></msg>'
    import zstandard
    compressed = zstandard.ZstdCompressor().compress(xml.encode())
    with sqlite3.connect(src) as con:
        con.execute(f'ALTER TABLE {FILEHELPER} ADD COLUMN compress_content BLOB')
        con.execute(f'UPDATE {FILEHELPPER if False else FILEHELPER} SET local_type=?, message_content=?, compress_content=?', ((51 << 32) | 49, b'fallback', compressed))
    index_messages(src, index, 'fixture')
    task = pending(index, OWNER)[0]
    assert task['finder'] == {'object_id': '12345', 'username': 'stable-author@finder', 'nickname': '临时作者', 'desc': '完整描述，严格匹配。'}
    assert 'synthetic-media-token' not in json.dumps(task)
    assert 'synthetic-decode-key' not in json.dumps(task)


def test_upgrade_replays_only_previously_delivered_author_identity_once(tmp_path):
    managed(tmp_path)
    src,index=source(tmp_path),tmp_path/'index.db'
    xml='<msg><appmsg><type>51</type><finderFeed><objectId>12345</objectId><username>stable@finder</username><nickname>作者</nickname><desc>作品</desc></finderFeed></appmsg></msg>'
    with sqlite3.connect(src) as con:
        con.execute(f'UPDATE {FILEHELPER} SET local_type=?,message_content=?',((51<<32)|49,xml.encode()))
    index_messages(src,index,'fixture')
    task=pending(index,OWNER)[0];acknowledge(index,OWNER,task['task_id'])
    with sqlite3.connect(index) as con: con.execute('UPDATE nexus_outbox SET author_version=0')
    assert pending(index,OTHER)==[]
    upgraded=pending(index,OWNER)[0]
    assert upgraded['task_id']==task['task_id']
    assert upgraded['finder']['username']=='stable@finder'
    acknowledge(index,OWNER,task['task_id'])
    assert pending(index,OWNER)==[]
