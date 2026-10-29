"""Continuous, bounded media work that yields to message refreshes."""
from __future__ import annotations

import time


def refresh_active(core):
    lock = getattr(core, 'refresh_lock', None)
    return lock is not None and lock.locked()


def run_media_worker(records, core_for, stop, wake):
    # One file at a time bounds memory and retains existing job/credential locks.
    # Successful work continues immediately. Yield by elapsed work budget, rather
    # than charging every small file a fixed delay. Empty/error queues still sleep.
    burst_started = time.monotonic()
    while not stop.is_set():
        wake.clear()
        worked = syncing = False
        try:
            for record in records():
                if stop.is_set():
                    break
                try:
                    core = core_for(record.id)
                    if refresh_active(core):
                        syncing = True
                        continue
                    worked = core.automatic_media.run_one(stop) or worked
                except Exception:
                    # A broken account must neither kill the worker nor hot-spin.
                    continue
        except Exception:
            pass  # Registry/storage outages use the same idle retry below.
        if syncing:
            delay = .05
        elif not worked:
            delay = 15
        elif time.monotonic() - burst_started >= .25:
            delay = .01
        else:
            continue
        wake.wait(delay)
        burst_started = time.monotonic()
