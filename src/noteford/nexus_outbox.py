"""Transactional, filehelper-only outbox. No WeChat hooks or message polling."""
from __future__ import annotations
import hashlib
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from .decode import decode_text_blob, split_local_type
from .mac_decode import app_message_xml, parse_finder_feed
from .product_workspace import require_active

FILEHELPER = 'Msg_' + hashlib.md5(b'filehelper').hexdigest()

def ensure_outbox(con):
    con.execute("""CREATE TABLE IF NOT EXISTS nexus_outbox (
        task_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, message_uid TEXT NOT NULL,
        payload TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0, author_version INTEGER NOT NULL DEFAULT 0)""")
    if 'author_version' not in {r[1] for r in con.execute('PRAGMA table_info(nexus_outbox)')}:
        con.execute('ALTER TABLE nexus_outbox ADD COLUMN author_version INTEGER NOT NULL DEFAULT 0')


def owner_scope(index_path):
    for root in Path(index_path).resolve().parents:
        marker = root / '.product-workspace.json'
        if marker.is_file():
            epoch = require_active(root)
            value = json.loads(marker.read_text())
            lease = json.loads((Path(value['installation']) / 'data/runtime/product-account/active-workspace.json').read_text())
            if lease.get('epoch') != epoch:
                raise PermissionError('product_workspace_changed')
            owner = lease.get('accountId', '')
            if not re.fullmatch(r'[a-f0-9-]{36}', owner):
                raise PermissionError('product_account_required')
            require_active(root, epoch)
            return root, owner, epoch
    return None

_LINK = re.compile(r'https://(?:channels\.weixin\.qq\.com/finder-preview/|weixin\.qq\.com/sph/)'
                   r'''[^\s<>"'\[\]，。；！？（）“”‘’]+''')


def _links(text):
    # Match exact hosts and path prefixes; never return surrounding prose.
    return list(dict.fromkeys(match.group(0).rstrip('.,;!?)') for match in _LINK.finditer(text)))


def message_items(row):
    msg_type, subtype = split_local_type(row['local_type'])
    base = {'message_uid': row['uid'], 'create_time': row['create_time']}
    if msg_type == 49 and subtype == 51:
        app = app_message_xml(row['message_content'], row['compress_content'])
        if app is None:
            return []
        # The pure parser accepts XML, while the blob adapter handles zstd.
        from xml.etree.ElementTree import tostring
        finder = parse_finder_feed(tostring(app, encoding='unicode'))
        if finder is None:
            return []
        finder['media'] = [{key: value for key, value in media.items() if key != 'full_cover_url'}
                           for media in finder['media']]
        url = app.findtext('./url')
        return [{**base, 'kind': 'finder_card', 'url': url.strip() if url and url.strip() else None,
                 'finder': finder}]
    if msg_type == 1:
        text, _ = decode_text_blob(row['message_content'])
        urls = _links(text)
    elif msg_type == 49 and subtype in (0, 5):
        app = app_message_xml(row['message_content'], row['compress_content'])
        urls = (_links(app.findtext('./url') or '') if app is not None
                and (subtype == 5 or app.findtext('./type') == '5') else [])
    else:
        return []
    return [{**base, 'kind': 'link', 'url': url, 'finder': None} for url in urls]



def enqueue_message(con, scope, wechat_account, table, row):
    if scope is None or table != FILEHELPER:
        return False
    root, owner, _ = scope
    changed = False
    for item in message_items(row):
        # Cards carry only the information required by Nexus's strict resolver.
        finder = item.get('finder')
        if finder:
            item['finder'] = {k: finder.get(k) or '' for k in ('object_id', 'username', 'nickname', 'desc')}
        material = json.dumps([owner, wechat_account, item['message_uid'], item['kind'],
                               item.get('url'), (finder or {}).get('object_id')], separators=(',', ':'))
        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, material))
        item.update(task_id=task_id, owner_id=owner, version=2)
        if len(json.dumps(item).encode()) > 65536:
            raise ValueError('nexus_task_too_large')
        con.execute('INSERT OR IGNORE INTO nexus_outbox(task_id,owner_id,message_uid,payload) VALUES(?,?,?,?)',
                    (task_id, owner, row['uid'], json.dumps(item, ensure_ascii=False)))
        changed = changed or bool(con.execute('SELECT changes()').fetchone()[0])
    return changed


def notify_committed(scope):
    if scope:
        # Advisory notification only. The DB transaction is authoritative; startup,
        # wake and reconnect drain it even if this process dies before notifying.
        root, _, _ = scope
        directory = root / 'data/runtime/nexus-events'
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(directory / 'wake', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(uuid.uuid4().hex.encode())
        except OSError:
            pass


def pending(index_path, owner, limit=50):
    if not Path(index_path).exists():
        return []
    with sqlite3.connect(index_path) as con:
        ensure_outbox(con)
        # Upgrade only previously authorized deliveries, never other chats/history.
        con.row_factory = sqlite3.Row
        for saved in con.execute('SELECT task_id,message_uid FROM nexus_outbox WHERE owner_id=? AND author_version=0 LIMIT 100', (owner,)).fetchall():
            row = con.execute('SELECT * FROM messages WHERE uid=? AND chat_table=?', (saved['message_uid'], FILEHELPER)).fetchone()
            items = message_items(row) if row else []
            card = next((item for item in items if item['kind']=='finder_card' and item['finder'].get('username')), None)
            if card:
                card['finder'] = {k: card['finder'].get(k) or '' for k in ('object_id','username','nickname','desc')}
                card.update(task_id=saved['task_id'], owner_id=owner, version=2)
                con.execute('UPDATE nexus_outbox SET payload=?,acknowledged=0,author_version=1 WHERE task_id=? AND owner_id=?', (json.dumps(card,ensure_ascii=False),saved['task_id'],owner))
            else:
                con.execute('UPDATE nexus_outbox SET author_version=1 WHERE task_id=? AND owner_id=?',(saved['task_id'],owner))
        return [json.loads(r[0]) for r in con.execute(
            'SELECT payload FROM nexus_outbox WHERE owner_id=? AND acknowledged=0 ORDER BY rowid LIMIT ?', (owner, limit))]


def acknowledge(index_path, owner, task_id):
    if Path(index_path).exists():
        with sqlite3.connect(index_path) as con:
            ensure_outbox(con)
            con.execute('UPDATE nexus_outbox SET acknowledged=1, payload=? WHERE owner_id=? AND task_id=?', ('{}', owner, task_id))
