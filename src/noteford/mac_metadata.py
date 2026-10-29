"""Mac archive metadata. Source databases are always opened read-only."""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .assets import latest_decrypted_database
from .decode import _protobuf_length_fields
from .image_crypto import image_extension
from .libraries import account_username


def _read(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)


def register_sources(project: Path, account: str, session: Path, contact: Path,
                     head: Path | None, auxiliary: list[Path] | None = None) -> None:
    entries = [('session', 'session', session), ('contact', 'contact', contact), ('head_image', 'head_image', head)]
    for source in auxiliary or []:
        category = 'message' if source.stem.startswith('media_') or source.stem == 'message_resource' else source.stem
        entries.append((category, source.stem, source))
    for category, name, source in entries:
        target = project / 'data' / 'state' / account / category / f'{name}.json'
        if source is None:
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({'decrypted': str(source.resolve()),
                              'source': 'decrypted_archive'})
        try:
            if target.read_text(encoding='utf-8') == payload:
                continue
        except FileNotFoundError:
            pass
        temporary = target.with_suffix('.json.tmp')
        temporary.write_text(payload, encoding='utf-8')
        temporary.chmod(0o600)
        temporary.replace(target)


def sender_databases(databases: list[Path]) -> list[Path]:
    result = []
    for path in databases:
        con = _read(path)
        try:
            # Older/manual plaintext exports can omit Name2Id. Leave their
            # unknown sender unknown, rather than retaining a stale mapping.
            if 'user_name' in {r[1] for r in con.execute('PRAGMA table_info(Name2Id)')}:
                result.append(path)
        finally:
            con.close()
    return result


def sync_directory(index: Path, account: str, contact: Path) -> int:
    """Include address-book entries even when the Mac has no recent session.

    Mac 4.x distinguishes contacts (1), joined rooms (2), cached non-friend
    members (3), and enterprise contacts (5). Never turn every cached group
    member into a friend. Older exports without these fields keep their
    session directory, with known message tables added when resolvable.
    """
    source = _read(contact)
    source.row_factory = sqlite3.Row
    try:
        records = [dict(r) for r in source.execute('SELECT * FROM contact')]
    finally:
        source.close()
    con = sqlite3.connect(index)
    try:
        existing = {r[0] for r in con.execute('SELECT username FROM conversations WHERE account=?', (account,))}
        message_tables = {r[0] for r in con.execute('SELECT DISTINCT chat_table FROM messages WHERE account=?', (account,))}
        added, kinds = [], []
        for row in records:
            username = row.get('username')
            if not username:
                continue
            table = 'Msg_' + hashlib.md5(username.encode('utf-8')).hexdigest()
            is_group = username.endswith('@chatroom')
            kind = 'groups' if is_group else 'official' if username.startswith('gh_') or (int(row.get('verify_flag') or 0) & 8) else 'people'
            kinds.append((account, username, kind))
            address_entry = (row.get('local_type') in {1, 2, 5}
                             and not row.get('delete_flag')
                             and username != account_username(account))
            if username in existing or not (address_entry or table in message_tables):
                continue
            display = (row.get('remark') or '').strip() or (row.get('nick_name') or '').strip() or username
            added.append((account, table, username, display, int(is_group)))
        con.execute('CREATE TABLE IF NOT EXISTS mac_contact_kinds ('
                    'account TEXT, username TEXT, kind TEXT, PRIMARY KEY(account,username))')
        with con:
            con.execute('DELETE FROM mac_contact_kinds WHERE account=?', (account,))
            con.executemany('INSERT OR REPLACE INTO mac_contact_kinds VALUES(?,?,?)', kinds)
            con.executemany('INSERT OR IGNORE INTO conversations '
                            '(account,chat_table,username,display_name,is_group,last_timestamp,sort_timestamp) '
                            'VALUES(?,?,?,?,?,0,0)', added)
    finally:
        con.close()
    return len(added)


def contact_kinds(index: Path, account: str) -> dict[str, str]:
    con = _read(index)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='mac_contact_kinds'").fetchone():
            return {}
        return dict(con.execute('SELECT username,kind FROM mac_contact_kinds WHERE account=?', (account,)))
    finally:
        con.close()


def room_nicknames(blob) -> dict[str, str]:
    if not isinstance(blob, bytes) or len(blob) > 16 * 1024 * 1024:
        return {}
    out = {}
    for member in _protobuf_length_fields(blob).get(1, []):
        fields = _protobuf_length_fields(member)
        try:
            username = fields.get(1, [b''])[0].decode('utf-8').strip()
            nickname = fields.get(2, [b''])[0].decode('utf-8').strip()
        except UnicodeError:
            continue
        if username and nickname and len(username) <= 256 and len(nickname) <= 512:
            out[username] = nickname
    return out


def sync_group_members(index: Path, account: str, contact: Path) -> int:
    source = _read(contact)
    try:
        tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = []
        if {'chat_room', 'chatroom_member'} <= tables:
            rooms = source.execute('SELECT id,username,owner,ext_buffer FROM chat_room').fetchall()
            for room_id, room, owner, blob in rooms:
                nicknames = room_nicknames(blob)
                for username, remark, nickname in source.execute(
                    'SELECT c.username,c.remark,c.nick_name FROM chatroom_member m '
                    'JOIN contact c ON c.id=m.member_id WHERE m.room_id=?', (room_id,)
                ):
                    if not username:
                        continue
                    room_name = nicknames.get(username, '')
                    display = (remark or '').strip() or room_name or (nickname or '').strip() or username
                    rows.append((account, room, username, display, room_name, int(username == owner)))
    finally:
        source.close()
    con = sqlite3.connect(index)
    try:
        con.execute('CREATE TABLE IF NOT EXISTS mac_group_members ('
                    'account TEXT, room TEXT, username TEXT, display_name TEXT, '
                    'room_nickname TEXT, is_owner INTEGER, PRIMARY KEY(account,room,username))')
        with con:
            con.execute('DELETE FROM mac_group_members WHERE account=?', (account,))
            con.executemany('INSERT OR REPLACE INTO mac_group_members VALUES(?,?,?,?,?,?)', rows)
    finally:
        con.close()
    return len(rows)


def group_members(index: Path, account: str, room: str | None = None) -> list[dict] | None:
    con = _read(index)
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='mac_group_members'").fetchone():
            return None
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(
            'SELECT room,username,display_name AS name,room_nickname,is_owner '
            'FROM mac_group_members WHERE account=? AND (? IS NULL OR room=?) '
            'ORDER BY username', (account, room, room))]
    finally:
        con.close()


def account_avatar(head: Path | None, username: str) -> str | None:
    if head is None:
        return None
    con = _read(head)
    try:
        row = con.execute('SELECT image_buffer FROM head_image WHERE username=?', (username,)).fetchone()
    finally:
        con.close()
    data = bytes(row[0]) if row and row[0] else b''
    if len(data) > 1024 * 1024:
        return None
    mime = {'jpg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp'}.get(image_extension(data))
    return f'data:{mime};base64,{base64.b64encode(data).decode()}' if mime else None


AVATAR_HOSTS = {'wx.qlogo.cn', 'thirdwx.qlogo.cn', 'mmhead.c2c.wechat.com',
                'mmhead.hk.wechat.com', 'wework.qpic.cn', 'wwcdn.weixin.qq.com'}


def avatar_url(raw: str) -> str | None:
    try:
        url = urlsplit(raw)
        if (url.scheme not in {'http', 'https'} or url.hostname not in AVATAR_HOSTS
                or url.username or url.password or url.port not in {None, 80, 443}):
            return None
        return urlunsplit(('https', url.hostname, url.path, url.query, ''))
    except (ValueError, TypeError):
        return None


def avatar_fields(resolver, names, local: dict) -> dict[str, dict]:
    """Return local images first, otherwise a lazy, account-scoped CDN source.

    The desktop's existing image proxy downloads only visible avatars and
    caches them in this library. No network access occurs on an API request.
    """
    result = {name: {'avatar_path': local.get(name)} for name in names if name}
    contact = latest_decrypted_database(resolver.project_root, resolver.account, 'contact', 'contact')
    missing = [name for name in result if not local.get(name)]
    if not contact or not missing:
        return result
    con = _read(contact)
    try:
        columns = {r[1] for r in con.execute('PRAGMA table_info(contact)')}
        if not {'small_head_url', 'big_head_url'} <= columns:
            return result
        for name, small, big in con.execute(
            'SELECT username,small_head_url,big_head_url FROM contact '
            'WHERE username IN (SELECT value FROM json_each(?))', (json.dumps(missing),)
        ):
            url = avatar_url(small) or avatar_url(big)
            if url:
                result[name].update(avatar_remote_url=url, avatar_library_id=resolver.project_root.name)
    finally:
        con.close()
    return result
