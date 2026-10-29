import json
import threading
from types import SimpleNamespace
import pytest
from noteford.service import MultiLibraryApi


def make_api(tmp_path):
    api = object.__new__(MultiLibraryApi)
    api.config_path = tmp_path / 'config.json'
    api.config_path.write_text('{"unrelated": "preserve"}', encoding='utf-8')
    api.root_config = {}
    api._lock = threading.RLock()
    # Timing tests supply their own record stream; platform selection is tested separately.
    api._active_records = lambda: api.registry.list()
    return api


def test_interval_roundtrip_and_validation(tmp_path):
    api = make_api(tmp_path)
    assert api.settings()['sync_interval_minutes'] == 5
    for minutes in (5, 10, 15, 30, 65, 1440):
        assert api.update_sync_interval(minutes)['sync_interval_minutes'] == minutes
        saved = json.loads(api.config_path.read_text(encoding='utf-8'))
        assert saved['unrelated'] == 'preserve'
        restarted = object.__new__(MultiLibraryApi)
        restarted.root_config = saved
        assert restarted.settings()['sync_interval_minutes'] == minutes
    original = api.config_path.read_bytes()
    for invalid in (0, 4, 7, -5, 1441, 1445, 2880, True, '10', 5.0, None):
        with pytest.raises(ValueError):
            api.update_sync_interval(invalid)
        assert api.config_path.read_bytes() == original


def test_running_worker_reloads_interval_without_duplicate_worker(tmp_path, monkeypatch):
    import noteford.service as module
    api = make_api(tmp_path)
    clock = [0.0]
    starts = []
    class Stop:
        stopped = False
        def clear(self): self.stopped = False
        def is_set(self): return self.stopped
        def wait(self, seconds):
            clock[0] += seconds
            if clock[0] == 120:
                api.update_sync_interval(5)
            return self.stopped
    api._periodic_stop = Stop()
    api._periodic_thread = None
    api.registry = SimpleNamespace(list=lambda: [SimpleNamespace(id='sample')])
    def refresh(**kwargs):
        starts.append(clock[0])
        if len(starts) == 2: api._periodic_stop.stopped = True
        return {'ok': False}
    api._core = lambda _: SimpleNamespace(refresh=refresh)
    class Thread:
        def __init__(self, target, **kwargs): self.target=target
        def is_alive(self): return True
        def start(self): self.target()
    monkeypatch.setattr(module.time, 'time', lambda: clock[0])
    monkeypatch.setattr(module.threading, 'Thread', Thread)
    api.start_periodic_refresh()
    assert starts == [0.0, 300.0]
    api.start_periodic_refresh()
    assert starts == [0.0, 300.0]


@pytest.mark.parametrize('jump,work,expected', [(0,600,[0,600]), (3600,0,[0,3601])])
def test_slow_round_and_resume_do_not_replay_missed_ticks(tmp_path,monkeypatch,jump,work,expected):
    import noteford.service as module
    api=make_api(tmp_path);api.root_config={'auto_refresh_interval_seconds':300}
    clock=[0];starts=[]
    class Stop:
        stopped=False
        def clear(self):self.stopped=False
        def is_set(self):return self.stopped
        def wait(self,seconds):
            clock[0]+=seconds
            if jump and clock[0]==1:clock[0]+=jump
            return self.stopped
    api._periodic_stop=Stop();api._periodic_thread=None
    api.registry=SimpleNamespace(list=lambda:[SimpleNamespace(id='a')])
    def refresh(**kwargs):
        starts.append(clock[0]);clock[0]+=work
        if len(starts)==2:api._periodic_stop.stopped=True
        return {'ok':False}
    api._core=lambda _:SimpleNamespace(refresh=refresh)
    class Thread:
        def __init__(self,target,**kwargs):self.target=target
        def is_alive(self):return True
        def start(self):self.target()
    monkeypatch.setattr(module.time,'time',lambda:clock[0])
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0] - jump if jump and clock[0] > jump else clock[0])
    monkeypatch.setattr(module.threading,'Thread',Thread)
    api.start_periodic_refresh()
    assert starts==expected


@pytest.mark.parametrize('interval,work', [(300,120), (10,3), (10,25)])
def test_round_start_cadence_does_not_add_work_duration(tmp_path, monkeypatch, interval, work):
    import noteford.service as module
    api=make_api(tmp_path);api.root_config={'auto_refresh_interval_seconds':interval}
    clock=[0.0];starts=[]
    class Stop:
        stopped=False
        def clear(self):self.stopped=False
        def is_set(self):return self.stopped
        def wait(self,seconds):clock[0]+=seconds;return self.stopped
    api._periodic_stop=Stop();api._periodic_thread=None
    api.registry=SimpleNamespace(list=lambda:[SimpleNamespace(id='a')],update=lambda *a,**k:None)
    def refresh(**kwargs):
        starts.append(clock[0]);clock[0]+=work
        if len(starts)==2:api._periodic_stop.stopped=True
        return {'ok':False}
    api._core=lambda _:SimpleNamespace(refresh=refresh)
    class Thread:
        def __init__(self,target,**kwargs):self.target=target
        def is_alive(self):return True
        def start(self):self.target()
    monkeypatch.setattr(module.time,'time',lambda:clock[0])
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(module.threading,'Thread',Thread)
    api.start_periodic_refresh()
    assert starts == [0.0, float(max(interval, work))]


def test_registry_and_settings_failures_retry_without_stopping(tmp_path, monkeypatch):
    import noteford.service as module
    api=make_api(tmp_path);clock=[0.0];starts=[];registry_calls=[0];settings_calls=[0]
    class Stop:
        stopped=False
        def clear(self):self.stopped=False
        def is_set(self):return self.stopped
        def wait(self,seconds):clock[0]+=seconds;return self.stopped
    def records():
        registry_calls[0]+=1
        if registry_calls[0]==2:raise OSError('private registry path')
        return [SimpleNamespace(id='a',display_name='A')]
    api.registry=SimpleNamespace(list=records,update=lambda *a,**k:None)
    def settings():
        settings_calls[0]+=1
        if settings_calls[0]==1:raise ValueError('private settings value')
        return {'sync_interval_minutes':5, 'sync_interval_seconds':300}
    api.settings=settings
    def refresh(**kwargs):
        starts.append(clock[0])
        if len(starts)==2:api._periodic_stop.stopped=True
        return {'ok':False}
    api._core=lambda _:SimpleNamespace(refresh=refresh)
    api._periodic_stop=Stop();api._periodic_thread=None
    class Thread:
        def __init__(self,target,**kwargs):self.target=target
        def is_alive(self):return True
        def start(self):self.target()
    monkeypatch.setattr(module.time,'time',lambda:clock[0])
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(module.threading,'Thread',Thread)
    api.start_periodic_refresh()
    assert len(starts)==2
    assert registry_calls[0] >= 3
    assert clock[0] >= 1


def test_interval_change_during_round_updates_the_active_deadline(tmp_path, monkeypatch):
    import noteford.service as module
    api=make_api(tmp_path);api.root_config={'auto_refresh_interval_seconds':3600}
    clock=[0.0];seen=[]
    class Stop:
        stopped=False
        def clear(self):self.stopped=False
        def is_set(self):return self.stopped
        def wait(self,seconds):clock[0]+=seconds;return self.stopped
    api._periodic_stop=Stop();api._periodic_thread=None
    api.registry=SimpleNamespace(list=lambda:[SimpleNamespace(id='a')],update=lambda *a,**k:None)
    def refresh(**kwargs):
        clock[0]=120
        api.update_sync_interval(5)
        deadline = kwargs['deadline_monotonic']
        seen.append(deadline() if callable(deadline) else deadline)
        api._periodic_stop.stopped=True
        return {'ok':False}
    api._core=lambda _:SimpleNamespace(refresh=refresh)
    class Thread:
        def __init__(self,target,**kwargs):self.target=target
        def is_alive(self):return True
        def start(self):self.target()
    monkeypatch.setattr(module.time,'time',lambda:clock[0])
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(module.threading,'Thread',Thread)
    api.start_periodic_refresh()
    assert seen == [300.0]
    assert api._periodic_next_at == 300.0


def test_legacy_interval_is_capped_without_rewriting_other_preferences(tmp_path):
    api = make_api(tmp_path)
    api.root_config['auto_refresh_interval_seconds'] = 172800
    original = api.config_path.read_bytes()
    assert api.settings()['sync_interval_minutes'] == 1440
    assert api.config_path.read_bytes() == original


def test_ten_seconds_is_opt_in_persistent_and_validated(tmp_path):
    api = make_api(tmp_path)
    original = api.config_path.read_bytes()
    assert api.settings()['sync_interval_seconds'] == 300
    assert api.config_path.read_bytes() == original
    api._periodic_round_started_wall = 100
    api._periodic_round_started_monotonic = 50
    value = api.update_sync_interval_seconds(10)
    assert value['sync_interval_seconds'] == 10
    assert api._periodic_next_at == 110
    assert api._periodic_deadline_monotonic == 60
    saved = json.loads(api.config_path.read_text())
    assert saved['unrelated'] == 'preserve'
    restarted = object.__new__(MultiLibraryApi)
    restarted.root_config = saved
    assert restarted.settings()['sync_interval_seconds'] == 10
    original = api.config_path.read_bytes()
    for invalid in (None, True, 10.0, '10', 0, 11, 60, 299, 301, 86700):
        with pytest.raises(ValueError):
            api.update_sync_interval_seconds(invalid)
        assert api.config_path.read_bytes() == original
    assert api.update_sync_interval(15)['sync_interval_seconds'] == 900


def test_seconds_http_route_requires_auth_and_preserves_invalid_input(tmp_path):
    import http.client
    from http.server import ThreadingHTTPServer
    from noteford.service import make_handler
    api = make_api(tmp_path)
    api.root_config['project_root'] = str(tmp_path)
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(api, 'test-token'))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    try:
        original = api.config_path.read_bytes()
        for body, authorized, expected in [({'seconds': 10}, False, 401), ({'seconds': 60}, True, 400), ({'seconds': 10}, True, 200), ({'minutes': 15}, True, 200)]:
            headers = {'Content-Type': 'application/json'}
            if authorized:
                headers['X-Noteford-Token'] = 'test-token'
            connection.request('POST', '/api/v1/settings/sync-interval', json.dumps(body), headers)
            response = connection.getresponse()
            payload = json.loads(response.read())
            assert response.status == expected
            if expected != 200:
                assert api.config_path.read_bytes() == original
            else:
                assert payload['data']['sync_interval_seconds'] == (10 if 'seconds' in body else 900)
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
