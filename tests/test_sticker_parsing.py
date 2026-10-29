import http.client
import json
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from noteford import mac_cdn, mac_completion
from noteford.cdn_download import DownloadError
from noteford.service import MultiLibraryApi, make_handler


def settings_api(root):
    root.mkdir(parents=True, exist_ok=True)
    config = root / 'config.local.json'
    if not config.exists():
        config.write_text(json.dumps({'project_root': str(root), 'wechat_roots': []}))
    return MultiLibraryApi(config)


def test_setting_persists_per_workspace_and_propagates_to_open_libraries(tmp_path):
    a, b = settings_api(tmp_path / 'a'), settings_api(tmp_path / 'b')
    assert a.settings()['parse_stickers'] and b.settings()['parse_stickers']
    core = SimpleNamespace(config={'auto_media_loading': False})
    a._cores['open-library'] = core
    cache = tmp_path / 'a/data/media-cache/saved.gif'
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b'GIF89a existing private cache')
    assert a.update_sticker_parsing(False)['parse_stickers'] is False
    assert core.config == {'auto_media_loading': False, 'parse_stickers': False}
    assert a._automatic_wake.is_set()
    assert settings_api(tmp_path / 'a').settings()['parse_stickers'] is False
    assert settings_api(tmp_path / 'b').settings()['parse_stickers'] is True
    a.registry = SimpleNamespace(context=lambda _: SimpleNamespace(config_dict=lambda: {'account': 'future'}))
    assert a._context_config('future-library')['parse_stickers'] is False
    a.update_sticker_parsing(True)
    assert core.config['parse_stickers'] and cache.read_bytes() == b'GIF89a existing private cache'


def test_invalid_or_failed_save_preserves_config_and_running_workers(tmp_path, monkeypatch):
    api = settings_api(tmp_path)
    api.update_sticker_parsing(False)
    core = SimpleNamespace(config={'parse_stickers': False})
    api._cores['open'] = core
    before = api.config_path.read_bytes()
    for value in (None, 0, 1, 'false', [], {}):
        with pytest.raises(ValueError):
            api.update_sticker_parsing(value)
    monkeypatch.setattr('noteford.service.os.replace', lambda *args: (_ for _ in ()).throw(OSError('fixture write failure')))
    with pytest.raises(OSError):
        api.update_sticker_parsing(True)
    assert api.config_path.read_bytes() == before
    assert not api.settings()['parse_stickers'] and not core.config['parse_stickers']
    assert not api.config_path.with_suffix('.stickers.tmp').exists()


def test_authenticated_setting_endpoint_and_boolean_validation(tmp_path):
    api = settings_api(tmp_path)
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(api, 'fixture-token'))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    try:
        for value, authorized, status in [(False, False, 401), ('false', True, 400), (False, True, 200), (True, True, 200)]:
            headers = {'Content-Type': 'application/json'}
            if authorized:
                headers['X-Noteford-Token'] = 'fixture-token'
            connection.request('POST', '/api/v1/settings/parse-stickers', json.dumps({'enabled': value}), headers)
            response = connection.getresponse()
            body = json.loads(response.read())
            assert response.status == status
            if status == 200:
                assert body['data']['parse_stickers'] is value
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_disabled_queue_skips_stickers_but_keeps_photos_and_resumes(tmp_path, monkeypatch):
    from test_automatic_media import setup
    core, calls, index = setup(tmp_path)
    with sqlite3.connect(index) as db:
        db.execute("""INSERT INTO messages(uid,account,source_db,chat_table,local_id,server_id,
            local_type,create_time,row_hash,indexed_at) VALUES('sticker','acct','message_0.db',
            'Msg_follow',100,'100',47,1,'v1','now')""")
    monkeypatch.setattr(mac_completion, 'live_root', lambda _: tmp_path / 'source')
    worker = mac_completion.MacAutomaticMedia(core)
    assert 'sticker' in {row['uid'] for row in worker._rows()}
    core.config['parse_stickers'] = False
    assert not worker.eligible('sticker', 'v1')
    assert {'image', 'video'} <= {row['uid'] for row in worker._rows()}
    core.config['parse_stickers'] = True
    assert worker.eligible('sticker', 'v1')


@pytest.mark.parametrize('kind', ['emoji', 'gif'])
def test_disabled_sticker_never_reads_cached_pixels_or_original_xml(tmp_path, monkeypatch, kind):
    item = {'uid': 'sticker', 'type': {'kind': kind}, 'text': '[表情]'}
    core = SimpleNamespace(config={'parse_stickers': False}, repository=SimpleNamespace(
        message_by_uid=lambda _: {'message': item, 'conversation': {'username': 'group'}}))
    monkeypatch.setattr(mac_completion, '_cached', lambda *args: pytest.fail('cached pixels read while disabled'))
    monkeypatch.setattr(mac_completion, 'message_xml', lambda *args: pytest.fail('original XML read while disabled'))
    assert mac_completion.recheck(core, 'sticker')['messages'][0]['media']['status'] == 'disabled'
    text = {'uid': 'text', 'type': {'kind': 'text'}, 'text': '收到 👍 [微笑]'}
    mac_completion.enrich(core, text)
    assert text == {'uid': 'text', 'type': {'kind': 'text'}, 'text': '收到 👍 [微笑]'}


def test_disabled_download_and_inflight_toggle_never_write_sticker(tmp_path, monkeypatch):
    config = {'parse_stickers': False, 'account': 'fixture', 'project_root': str(tmp_path)}
    monkeypatch.setattr(mac_cdn, 'live_root', lambda _: pytest.fail('source accessed while disabled'))
    with pytest.raises(DownloadError, match='download_cancelled'):
        mac_cdn.download_emoji(config, 'sticker', {})
    config['parse_stickers'] = True
    monkeypatch.setattr(mac_cdn, 'live_root', lambda _: tmp_path / 'source')
    monkeypatch.setattr(mac_cdn, 'retained_download', lambda *args: None)
    def read_local(*args, should_continue):
        config['parse_stickers'] = False
        assert not should_continue()
        return b'GIF89a fixture'
    monkeypatch.setattr('noteford.mac_emoticon.read_local', read_local)
    monkeypatch.setattr(mac_cdn, 'retain', lambda *args: pytest.fail('sticker decoding started after disabling'))
    with pytest.raises(DownloadError, match='download_cancelled'):
        mac_cdn.download_emoji(config, 'sticker', {'md5': 'a' * 32})
    assert not list((tmp_path / 'data/media-cache').rglob('*'))
