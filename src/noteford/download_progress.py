"""Bounded, in-memory byte counters for explicitly requested downloads."""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock
import re
import time

_current = ContextVar('download_progress', default=None)
_lock = Lock()
_states = {}


def key(root, account, request_id):
    if not isinstance(request_id, str) or not re.fullmatch(r'[a-zA-Z0-9-]{16,80}', request_id):
        raise ValueError('invalid_download_request')
    return (str(root), account, request_id)


def snapshot(root, account, request_id):
    with _lock:
        value = _states.get(key(root, account, request_id))
        return dict(value) if value and time.monotonic() - value['updated'] < 600 else {'received': 0, 'total': None, 'state': 'waiting'}


def report(received, total):
    current = _current.get()
    if current is None:
        return
    with _lock:
        value = _states.get(current)
        if value is not None:
            value.update(received=max(0, int(received)), total=int(total) if total and total > 0 else None,
                         state='downloading', updated=time.monotonic())


@contextmanager
def track(root, account, request_id):
    current = key(root, account, request_id)
    with _lock:
        now = time.monotonic()
        for old in list(_states):
            if now - _states[old]['updated'] > 600:
                del _states[old]
        while len(_states) >= 256:
            del _states[next(iter(_states))]
        _states[current] = {'received': 0, 'total': None, 'state': 'waiting', 'updated': now}
    token = _current.set(current)
    try:
        yield
    finally:
        _current.reset(token)
        with _lock:
            if current in _states:
                _states[current].update(state='finished', updated=time.monotonic())
