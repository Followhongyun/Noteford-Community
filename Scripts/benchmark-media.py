"""Synthetic queue selection benchmark; no media or network operations."""
import json
import resource
import tempfile
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

from noteford.automatic_media import AutomaticMedia


class Queue(AutomaticMedia):
    def _rows(self, uid=None):
        yield {'uid': 'new', 'row_hash': 'v1', 'create_time': 1, 'username': 'fixture'}


with tempfile.TemporaryDirectory(prefix='noteford-queue-bench-') as directory:
    core = SimpleNamespace(project_root=Path(directory), account='fixture')
    worker = Queue(core)
    con = worker._state()
    with con:
        con.executemany('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (('fixture', str(i), 'chat', 1, 'v1', 'missing', 1, 1000, None, None, None)
             for i in range(20000)))
    con.close()
    tracemalloc.start()
    before = resource.getrusage(resource.RUSAGE_SELF)
    start = time.perf_counter()
    result = worker._candidate(1)
    elapsed = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    after = resource.getrusage(resource.RUSAGE_SELF)
    assert result[0]['uid'] == 'new'
    print(json.dumps(dict(history_jobs=20000, wall_seconds=elapsed,
        cpu_seconds=after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,
        python_peak_bytes=peak, process_peak_rss_bytes=after.ru_maxrss,
        physical_read_bytes=None, physical_write_bytes=None)))
