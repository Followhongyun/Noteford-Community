import http.client
import json
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

from noteford.repository import WeChatRepository
from noteford.service import LocalApi, MultiLibraryApi, load_or_create_token, make_handler
from noteford.indexer import _connect_index
from noteford.metadata import ensure_metadata_schema
from http.server import ThreadingHTTPServer


def _minimal_index(path: Path):
    con = _connect_index(path)
    ensure_metadata_schema(con)
    with con:
        con.execute(
            """INSERT INTO conversations(
                account, chat_table, username, display_name, is_group,
                session_type, last_timestamp, sort_timestamp
            ) VALUES(?,?,?,?,?,?,?,?)""",
            ("acct", "Msg_x", "wxid_x", "X", 0, 0, 0, 0),
        )
    con.close()


def test_media_recheck_requires_auth_and_explicit_library(tmp_path):
    calls = []
    def core(library):
        calls.append(library)
        if library != 'chosen':
            raise ValueError('unknown_library')
        return SimpleNamespace(recheck_media=lambda uid: {'messages': [{'uid': uid}]})
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(SimpleNamespace(_core=core, root_config={'project_root': str(tmp_path)}), 'test-token'))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    con = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=3)
    try:
        for payload, authorized, expected in [
            ({'library_id': 'chosen', 'uid': 'one'}, False, 401),
            ({'uid': 'one'}, True, 400),
            ({'library_id': 'unknown', 'uid': 'one'}, True, 400),
            ({'library_id': 'chosen', 'uid': 'one'}, True, 200),
        ]:
            headers = {'Content-Type': 'application/json'}
            if authorized:
                headers['X-Noteford-Token'] = 'test-token'
            con.request('POST', '/api/v1/media/recheck', json.dumps(payload), headers)
            response = con.getresponse()
            body = json.loads(response.read())
            assert response.status == expected
            if expected == 200:
                assert body['data']['library_id'] == 'chosen'
                assert body['data']['messages'][0]['uid'] == 'one'
        assert calls == ['unknown', 'chosen']
    finally:
        con.close()
        server.shutdown()
        server.server_close()


def test_service_requires_token_and_serves_status(tmp_path):
    index = tmp_path / "index.db"
    _minimal_index(index)
    token_file = tmp_path / "token.json"
    token = load_or_create_token(token_file)
    assert load_or_create_token(token_file) == token

    config = {
        "index_path": str(index),
        "account": "acct",
        "wechat_root": str(tmp_path / "wechat"),
        "project_root": str(tmp_path),
        "candidates_path": str(tmp_path / "candidate_keys.json"),
    }
    api = LocalApi(config)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api, token))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        con = http.client.HTTPConnection("127.0.0.1", port, timeout=3)

        con.request("GET", "/api/v1/status")
        denied = con.getresponse()
        assert denied.status == 401
        denied.read()

        con.request("GET", "/api/v1/status", headers={"X-Noteford-Token": token})
        response = con.getresponse()
        assert response.status == 200
        payload = json.loads(response.read())
        assert payload["ok"] is True
        assert payload["data"]["conversations"] == 1
        assert payload['data']['media_downloads'] == dict(total=0, downloaded=0, not_downloaded=0, expired=0)

        con.request(
            "GET",
            "/api/v1/signals?limit=20",
            headers={"X-Noteford-Token": token},
        )
        signals = con.getresponse()
        assert signals.status == 404
        signal_payload = json.loads(signals.read())
        assert signal_payload["ok"] is False

        con.request(
            "GET",
            "/api/v1/signals/detail",
            headers={"X-Noteford-Token": token},
        )
        missing_id = con.getresponse()
        assert missing_id.status == 404
        missing_payload = json.loads(missing_id.read())
        assert missing_payload["ok"] is False
        for method, endpoint in [("GET", "/api/v1/monitor"), ("POST", "/api/v1/monitor"), ("GET", "/api/v1/laya-model"), ("POST", "/api/v1/laya-model/download"), ("POST", "/api/v1/settings")]:
            con.request(method, endpoint, headers={"X-Noteford-Token": token})
            response = con.getresponse()
            assert response.status == 404
            response.read()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)






def test_service_resolves_message_media_to_local_artifact(tmp_path):
    index = tmp_path / "index.db"
    con = _connect_index(index)
    ensure_metadata_schema(con)
    chat_hash = "0123456789abcdef0123456789abcdef"
    chat_table = "Msg_" + chat_hash
    md5 = b"fedcba9876543210fedcba9876543210"
    nested = bytes([0x22, len(md5)]) + md5
    packed = bytes([0x08, 0x01, 0x10, 0x02, 0x1A, len(nested)]) + nested
    timestamp = 1789980000

    with con:
        con.execute(
            """INSERT INTO conversations(
                account, chat_table, username, display_name, is_group,
                session_type, last_timestamp, sort_timestamp
            ) VALUES(?,?,?,?,?,?,?,?)""",
            ("acct", chat_table, "wxid_x", "X", 0, 0, timestamp, timestamp),
        )
        con.execute(
            """INSERT INTO messages(
                uid,account,source_db,chat_table,local_id,server_id,local_type,
                sort_seq,real_sender_id,create_time,status,message_content,
                compress_content,packed_info_data,wcdb_ct,source,row_hash,indexed_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "u1", "acct", "message_0.db", chat_table, 1, "101", 3,
                1, "", timestamp, 0, b"", b"", packed, None, b"", "h1", "now",
            ),
        )
    con.close()

    source = (
        tmp_path / "wechat" / "acct" / "msg" / "attach"
        / chat_hash / "2026-09" / "Img"
        / "fedcba9876543210fedcba9876543210_t.dat"
    )
    source.parent.mkdir(parents=True)
    source.write_bytes(b"encrypted-image")

    api = LocalApi(
        {
            "index_path": str(index),
            "account": "acct",
            "wechat_root": str(tmp_path / "wechat"),
            "project_root": str(tmp_path),
            "candidates_path": str(tmp_path / "candidate_keys.json"),
        }
    )
    payload = api.messages("wxid_x", limit=20)
    assert payload["selection"]["state"] == "unselected"
    media = payload["messages"][0]["media"]
    assert media["state"] == "indexed_only"
    assert "status" not in media
    assert not (tmp_path / "data" / "media-cache").exists()

    con = sqlite3.connect(index)
    with con:
        con.execute(
            """INSERT INTO conversation_selection(
                account,username,chat_table,state,source_dbs,
                selected_at,updated_at,last_refresh_at,last_error
            ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                "acct", "wxid_x", chat_table, "following", "[\"message_0.db\"]",
                "now", "now", "now", None,
            ),
        )
    con.close()

    payload = api.messages("wxid_x", limit=20)
    assert payload["selection"]["state"] == "following"
    media = payload["messages"][0]["media"]
    assert media["status"] == "encrypted_thumbnail"
    assert media["encrypted"] is True
    assert Path(media["path"]).read_bytes() == b"encrypted-image"


def test_hourly_refresh_includes_library_with_no_followed_conversations():
    called = threading.Event()
    updates = []
    record = SimpleNamespace(id='lib', display_name='Example')
    registry = SimpleNamespace(
        list=lambda: [record],
        current=lambda: record,
        update=lambda *args, **kwargs: updates.append((args, kwargs)),
    )
    core = SimpleNamespace(refresh=lambda **kwargs: (called.set() or {
        'ok': True, 'status': {'self_display_name': 'Example',
                               'following_conversations': 0}
    }))
    api = object.__new__(MultiLibraryApi)
    api.root_config = {'auto_refresh_interval_seconds': 3600}
    api.registry = registry
    api._core = lambda library_id: core
    api._periodic_stop = threading.Event()
    api._periodic_thread = None

    api.start_periodic_refresh()
    try:
        assert called.wait(timeout=2)
    finally:
        api.stop_periodic_refresh()
    assert updates and updates[0][0] == ('lib',)

def test_removed_digest_routes_do_not_generate_reports(tmp_path):
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(SimpleNamespace(root_config={'project_root': str(tmp_path)}), 'test-token'))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    con = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=3)
    try:
        for method, route in [('GET', 'settings'), ('GET', 'notifications'), ('POST', 'generate'), ('POST', 'group')]:
            con.request(method, '/api/v1/digest/' + route, '{}' if method == 'POST' else None,
                        {'X-Noteford-Token': 'test-token', 'Content-Type': 'application/json'})
            response = con.getresponse()
            response.read()
            assert response.status == 404
    finally:
        con.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_login_return_does_not_advance_the_sync_deadline(monkeypatch):
    import noteford.service as service
    from noteford import login_status
    now = [0.0]
    calls = []
    record = SimpleNamespace(id='lib', display_name='Example', wechat_root='fixture', account='one')
    # A login detector that would continuously report recovery must not schedule
    # any work. A new login only changes the source read by the next timed sync.
    monkeypatch.setattr(login_status.LoginReconnect, 'poll', lambda *a: True)
    monkeypatch.setattr(service, 'time', SimpleNamespace(time=lambda: now[0], monotonic=lambda: now[0]))
    monkeypatch.setattr(service, 'sys', SimpleNamespace(platform='win32'))
    class Stop:
        stopped = False
        def clear(self): self.stopped = False
        def is_set(self): return self.stopped
        def wait(self, seconds):
            now[0] += seconds
            if now[0] > 3600: self.stopped = True
            return self.stopped
    def refresh(**kwargs):
        calls.append((kwargs['trigger'], now[0]))
        if len(calls) == 2: api._periodic_stop.stopped = True
        return {'ok': True, 'status': {}}
    api = object.__new__(MultiLibraryApi)
    api.registry = SimpleNamespace(list=lambda: [record], update=lambda *a, **k: None)
    api._core = lambda _: SimpleNamespace(refresh=refresh)
    api.settings = lambda: {'sync_interval_minutes': 60, 'sync_interval_seconds': 3600}
    api._periodic_stop = Stop()
    api._periodic_thread = None
    class Thread:
        def __init__(self, target, **kwargs): self.target = target
        def start(self): self.target()
        def is_alive(self): return False
    monkeypatch.setattr(service, 'threading', SimpleNamespace(Thread=Thread))
    api.start_periodic_refresh()
    assert calls == [('startup', 0), ('interval', 3600)]
