from datetime import date, datetime
import threading
from types import SimpleNamespace

import pytest

from noteford.media_ranges import MediaRanges, validate_range
from noteford.service import MultiLibraryApi


def test_default_is_fixed_at_added_day_and_today_rolls(tmp_path):
    first = MediaRanges(tmp_path, '2026-09-30T12:00:00+08:00', today=date(2026, 10, 1))
    later = MediaRanges(tmp_path, '2026-09-30T12:00:00+08:00', today=date(2026, 10, 11))
    assert first.get('a') == later.get('a') == {'start': '2026-09-16', 'end': None}
    assert datetime.fromtimestamp(later.bounds('a')[1]).date() == date(2026, 10, 12)
    assert first.bounds('a')[1] < later.bounds('a')[1]


def test_persistence_boundaries_and_account_conversation_isolation(tmp_path):
    ranges = MediaRanges(tmp_path / 'one', '2026-10-01', today=date(2026, 10, 11))
    ranges.save('a', '2026-09-01', '2026-09-02')
    reloaded = MediaRanges(tmp_path / 'one', '2026-10-01', today=date(2026, 10, 12))
    assert reloaded.get('a') == {'start': '2026-09-01', 'end': '2026-09-02'}
    start, end = reloaded.bounds('a')
    assert reloaded.contains('a', start) and reloaded.contains('a', end - 1)
    assert not reloaded.contains('a', start - 1) and not reloaded.contains('a', end)
    assert reloaded.get('b')['end'] is None
    other = MediaRanges(tmp_path / 'two', '2026-10-01')
    assert other.get('a')['end'] is None


@pytest.mark.parametrize('start,end', [('2026-02-30', None), ('2026-10-12', None), ('2026-10-02', '2026-10-01'), ('2026-10-01', '2026-10-12'), ('20261001', None), (None, None)])
def test_invalid_dates(start, end):
    with pytest.raises(ValueError):
        validate_range(start, end, today=date(2026, 10, 11))


def test_api_pins_library_and_prefetches_only_requested_conversation(tmp_path):
    calls = []
    done = threading.Event()
    def prefetch(**kwargs):
        calls.append(kwargs)
        done.set()
    core = SimpleNamespace(repository=SimpleNamespace(conversation=lambda name: {} if name == 'a' else None),
                           _media_ranges=lambda: MediaRanges(tmp_path, '2026-10-01'),
                           refresh_lock=threading.Lock(), _prefetch_followed_media=prefetch,
                           automatic_media=SimpleNamespace(summary=lambda _: {'running':False,'completed':0,'waiting':0}))
    api = object.__new__(MultiLibraryApi)
    api.root_config = {'project_root': str(tmp_path)}
    api._lock = threading.RLock()
    api._automatic_wake = threading.Event()
    requested = []
    def get_core(library):
        requested.append(library)
        return core
    api._core = get_core
    result = api.conversation_media_range('one', 'a', update={'start': '2026-09-01', 'end': '2026-09-02'})
    assert done.wait(2)
    assert api._automatic_wake.is_set()
    assert result['library_id'] == 'one' and requested == ['one']
    assert calls == [{'username': 'a'}]
    assert api.conversation_media_range('one', 'a')['start'] == '2026-09-01'
    with pytest.raises(ValueError):
        api.conversation_media_range('', 'a')
    with pytest.raises(ValueError):
        api.conversation_media_range('one', 'missing', update={'start': '2026-09-01'})

    # Exercise authenticated HTTP transport with synthetic library/conversation data.
    import http.client
    import json
    from http.server import ThreadingHTTPServer
    from noteford.service import make_handler
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(api, 'fixture-token'))
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    headers = {'X-Noteford-Token': 'fixture-token', 'Content-Type': 'application/json'}
    try:
        route = '/api/v1/conversation-media-range'
        client.request('GET', route + '?library_id=one&conversation=a')
        response = client.getresponse()
        assert response.status == 401
        response.read()
        client.request('POST', route, json.dumps({'library_id': 'one', 'conversation': 'a', 'start': '2026-09-03', 'end': None}), headers)
        response = client.getresponse()
        assert response.status == 200
        assert json.loads(response.read())['data']['end'] is None
        client.request('GET', route + '?library_id=one&conversation=a', headers=headers)
        response = client.getresponse()
        assert response.status == 200
        assert json.loads(response.read())['data']['start'] == '2026-09-03'
        client.request('POST', route, json.dumps({'library_id': 'one', 'conversation': 'a', 'start': 'invalid'}), headers)
        response = client.getresponse()
        assert response.status == 400
        response.read()
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        worker.join(2)
