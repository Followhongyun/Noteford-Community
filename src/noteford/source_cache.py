"""Per-index completed source revisions; failures never advance the cache."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path


def cached_source_stage(index_path, account, stage, sources, action, *, revision_token=None):
    revision = [revision_token]
    for source in sources:
        if source is None:
            revision.append(None)
        else:
            for path in (source, Path(str(source) + '-wal')):
                try:
                    stat = path.stat()
                    revision.append([str(path.resolve()), stat.st_ino, stat.st_size,
                                     stat.st_mtime_ns, stat.st_ctime_ns])
                except FileNotFoundError:
                    revision.append(None)
    revision = json.dumps(revision)
    with closing(sqlite3.connect(index_path)) as con, con:
        con.execute('''CREATE TABLE IF NOT EXISTS completed_source_stages (
            account TEXT, stage TEXT, revision TEXT, result TEXT,
            PRIMARY KEY(account,stage))''')
        old = con.execute('SELECT revision,result FROM completed_source_stages WHERE account=? AND stage=?',
                          (account, stage)).fetchone()
        if old and old[0] == revision:
            return json.loads(old[1]), True
    result = action()
    # Each action owns its transactions. Persist only after it returns, so a
    # crash or partial failure will rerun it against the last published source.
    with closing(sqlite3.connect(index_path)) as con, con:
        con.execute('INSERT OR REPLACE INTO completed_source_stages VALUES(?,?,?,?)',
                    (account, stage, revision, json.dumps(result)))
    return result, False
