import threading
from types import SimpleNamespace

import pytest
from noteford import media_worker as worker


def test_continuous_work_only_yields_after_time_budget(monkeypatch):
    clock = [0.0]
    stop = threading.Event()
    calls, waits = [], []
    def run(event):
        calls.append(clock[0])
        clock[0] += .1
        if len(calls) == 7:
            stop.set()
        return True
    class Wake:
        def clear(self): pass
        def wait(self, delay): waits.append(delay)
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock[0])
    core = SimpleNamespace(refresh_lock=threading.Lock(), automatic_media=SimpleNamespace(run_one=run))
    worker.run_media_worker(lambda: [SimpleNamespace(id='one')], lambda _: core, stop, Wake())
    assert len(calls) == 7
    assert waits == [.01, .01]


def test_refresh_has_priority_before_selecting_another_file():
    stop = threading.Event()
    lock = threading.Lock()
    lock.acquire()
    calls, waits = [], []
    def run(event):
        assert not lock.locked()
        calls.append(True)
        stop.set()
        return True
    class Wake:
        def clear(self): pass
        def wait(self, delay):
            waits.append(delay)
            assert not calls
            lock.release()
    core = SimpleNamespace(refresh_lock=lock, automatic_media=SimpleNamespace(run_one=run))
    worker.run_media_worker(lambda: [SimpleNamespace(id='one')], lambda _: core, stop, Wake())
    assert calls == [True]
    assert waits == [.05]


def test_registry_outage_sleeps_and_recovers():
    stop = threading.Event()
    failures, waits = [], []
    def records():
        failures.append(True)
        if len(failures) == 1:
            raise OSError('fixture')
        stop.set()
        return []
    class Wake:
        def clear(self): pass
        def wait(self, delay): waits.append(delay)
    worker.run_media_worker(records, None, stop, Wake())
    assert len(failures) == 2
    assert waits == [15, 15]


@pytest.mark.parametrize('cancel', [False, True])
def test_in_flight_transfer_waits_for_refresh_without_restarting(tmp_path, cancel):
    from test_automatic_media import setup
    from noteford.automatic_media import AutomaticMedia
    core, calls, _ = setup(tmp_path)
    core.refresh_lock = threading.Lock()
    stop = threading.Event()
    entered, finished = threading.Event(), threading.Event()
    original = core.recheck_media
    def complete(uid, *, should_continue):
        assert should_continue()
        core.refresh_lock.acquire()
        entered.set()
        allowed = should_continue()
        finished.set()
        if not allowed:
            return {'messages': [{'media': {'status': 'missing', 'download_error': 'download_cancelled'}}]}
        return original(uid, should_continue=should_continue)
    core.recheck_media = complete
    media = AutomaticMedia(core)
    thread = threading.Thread(target=lambda: media.run_one(stop, now=100))
    thread.start()
    try:
        assert entered.wait(2)
        assert not finished.wait(.1)
        if cancel:
            stop.set()
        else:
            core.refresh_lock.release()
        assert finished.wait(2)
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert calls == ([] if cancel else ['image'])
    finally:
        stop.set()
        if core.refresh_lock.locked():
            core.refresh_lock.release()
        thread.join(timeout=2)
