"""Capture media loading consent on first insertion; toggles never rewrite history."""
import sqlite3
from contextlib import closing
from pathlib import Path


def ensure_policy(con):
    existing = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_media_policy'").fetchone()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS media_loading_setting (id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL);
        INSERT OR IGNORE INTO media_loading_setting VALUES (1,1);
        CREATE TABLE IF NOT EXISTS message_media_policy (uid TEXT PRIMARY KEY, automatic INTEGER NOT NULL, manual INTEGER NOT NULL DEFAULT 0);
    """)
    columns = {row[1] for row in con.execute('PRAGMA table_info(message_media_policy)')}
    if 'range_exempt' not in columns:
        # Existing choices remain legacy-scoped; only future insertions bypass the retired calendar.
        con.execute('ALTER TABLE message_media_policy ADD COLUMN range_exempt INTEGER NOT NULL DEFAULT 0')
        con.execute('DROP TRIGGER IF EXISTS capture_media_loading')
    con.executescript("""
        CREATE TRIGGER IF NOT EXISTS capture_media_loading AFTER INSERT ON messages
        WHEN (NEW.local_type & 4294967295) IN (3,43)
        BEGIN
          INSERT OR IGNORE INTO message_media_policy(uid,automatic,range_exempt)
          SELECT NEW.uid,enabled,1 FROM media_loading_setting WHERE id=1;
        END;
    """)
    if not existing:
        con.execute("INSERT OR IGNORE INTO message_media_policy(uid,automatic) SELECT uid,1 FROM messages WHERE (local_type & 4294967295) IN (3,43)")
    con.commit()


def set_enabled(index, enabled):
    if type(enabled) is not bool:
        raise ValueError('auto_media_loading_must_be_boolean')
    with closing(sqlite3.connect(index, timeout=30)) as con, con:
        con.execute('UPDATE media_loading_setting SET enabled=? WHERE id=1', (int(enabled),))


def allows_original(index, uid):
    with closing(sqlite3.connect(Path(index).resolve().as_uri()+'?mode=ro', uri=True)) as con:
        row = con.execute('SELECT automatic,manual FROM message_media_policy WHERE uid=?', (uid,)).fetchone()
        return row is None or bool(row[0] or row[1])


def mark_manual(index, uid):
    with closing(sqlite3.connect(index, timeout=30)) as con, con:
        con.execute('UPDATE message_media_policy SET manual=1 WHERE uid=?', (uid,))
