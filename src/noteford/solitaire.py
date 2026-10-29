"""Native WeChat solitaire membership, never inferred from message prose."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path


def native_memberships(database: Path, index_path: Path, account: str) -> dict[str, str]:
    links: dict[str, str] = {}
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as native, \
            sqlite3.connect(index_path.as_uri() + '?mode=ro', uri=True) as index:
        tables = {r[0] for r in native.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in tables:
            if not re.fullmatch(r'Solitaire_[0-9a-f]{32}', table):
                continue
            suffix = table.removeprefix('Solitaire_')
            valid_table = 'SolitaireValid_' + suffix
            if valid_table not in tables:
                continue
            # These are WeChat's native group records (UNIQUE(header, footer)),
            # not titles extracted from chat text. The validity table explicitly
            # links message numbers to these records. Ambiguous headers fail closed.
            groups: dict[str, list[str]] = {}
            for header, footer, sid in native.execute(f'SELECT header,footer,sid FROM "{table}"'):
                if not header:
                    continue
                if sid and len(sid) <= 256 and sid.isascii() and all(32 < ord(c) < 127 for c in sid):
                    key = hashlib.sha256(('wechat-solitaire-sid:' + sid).encode('ascii')).hexdigest()
                else:
                    identity = json.dumps([account, suffix, header, footer], ensure_ascii=False)
                    key = hashlib.sha256(('wechat-solitaire-record:' + identity).encode()).hexdigest()
                groups.setdefault(header, []).append(key)
            candidates: dict[str, set[str]] = {}
            for title, valid, local_id, seq, timestamp in native.execute(
                f'SELECT title,valid,local_id,sort_seq,create_time FROM "{valid_table}"'
            ):
                keys = groups.get(title, [])
                if valid != 1 or len(keys) != 1:
                    continue
                rows = index.execute(
                    'SELECT uid FROM messages WHERE account=? AND chat_table=? '
                    'AND local_id=? AND sort_seq=? AND create_time=? AND local_type=?',
                    (account, 'Msg_' + suffix, local_id, seq, timestamp, (53 << 32) | 49),
                ).fetchall()
                if len(rows) == 1:
                    candidates.setdefault(rows[0][0], set()).add(keys[0])
            links.update({uid: next(iter(keys)) for uid, keys in candidates.items() if len(keys) == 1})
    return links


def refresh_native_memberships(*, wechat_root: Path, account: str, project_root: Path,
                               candidates_path: Path, index_path: Path,
                               allow_memory_rescan: bool = True) -> dict:
    from .refresh import refresh_database, _write_json_atomic
    source = wechat_root / account / 'db_storage/solitaire/solitaire.db'
    target = index_path.with_name('solitaire-memberships.json')
    if not source.is_file():
        _write_json_atomic(target, {'account': account, 'links': {}})
        return {'status': 'unavailable', 'linked': 0}
    try:
        result = refresh_database(source, account, project_root, candidates_path, index_path,
                                  allow_memory_rescan=allow_memory_rescan, index_all_messages=False)
        links = native_memberships(Path(result.decrypted), index_path, account)
        _write_json_atomic(target, {'account': account, 'links': links})
        return {'status': 'ready', 'linked': len(links)}
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        # A failed refresh must not label an old native snapshot as current.
        _write_json_atomic(target, {'account': account, 'links': {}})
        return {'status': 'unavailable', 'linked': 0}


def read_memberships(index_path: Path, account: str) -> dict[str, str]:
    try:
        data = json.loads(index_path.with_name('solitaire-memberships.json').read_text(encoding='utf-8'))
        if data.get('account') == account and isinstance(data.get('links'), dict):
            return data['links']
    except (OSError, ValueError):
        pass
    return {}
