"""Deterministic, read-only material extraction from followed message records."""
import hashlib
import json
import re
import threading
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlsplit
from .decode import decode_message

KINDS = {'image', 'video', 'article', 'channel', 'link', 'file'}

# In-memory only: never persist decoded messages outside the library. Cache keys
# include the canonical database path, account and every authorization/filter bound.
_cache_lock = threading.RLock()
_snapshots = OrderedDict()
_decoded = OrderedDict()

def _revision(repository):
    path = getattr(repository, 'index_path', None)
    if path is None:
        return None
    path = Path(path).resolve()
    stamps = []
    for candidate in (path, Path(str(path) + '-wal')):
        try:
            stat = candidate.stat()
            # SQLite may create an empty WAL on the first read. It contains no
            # committed data and is equivalent to an absent WAL.
            stamps.append(None if candidate != path and stat.st_size == 0 else (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        except FileNotFoundError:
            stamps.append(None)
    return (str(path), repository.account, tuple(stamps))

def _decode_cached(repository, row):
    # Hash raw payloads instead of retaining a second copy of large XML/blobs.
    digest = hashlib.sha256()
    for value in (row['local_type'], row['message_content'], row['compress_content'], row['packed_info_data'], row['is_group']):
        value = value if isinstance(value, bytes) else repr(value).encode()
        digest.update(len(value).to_bytes(8, 'big'))
        digest.update(value)
    key = (str(getattr(repository, 'index_path', '')), repository.account, digest.digest())
    if key in _decoded:
        _decoded.move_to_end(key)
        return _decoded[key]
    decoded = decode_message(row['local_type'], row['message_content'], row['compress_content'], row['packed_info_data'], bool(row['is_group']))
    value = (decoded, extract_items(decoded))
    _decoded[key] = value
    if len(_decoded) > 20000:
        _decoded.popitem(last=False)
    return value

def extract_items(decoded):
    items = []
    kind = decoded.kind
    if kind in {'image', 'video', 'file'}:
        items.append((kind, '', decoded.title or decoded.text or {'image':'图片','video':'视频','file':'文件'}[kind]))
    elif kind.startswith('channel_') or kind in {'video_account_intro'}:
        try:
            channel_url = decoded.url if decoded.url and urlsplit(decoded.url).scheme in {'http', 'https'} and urlsplit(decoded.url).hostname else ''
        except ValueError:
            channel_url = ''
        items.append(('channel', channel_url, decoded.title or decoded.text or '视频号分享'))
    urls = []
    for url in [decoded.url, *re.findall(r'https?://[^\s<>"\u3000]+', decoded.text or '')]:
        if not url:
            continue
        url = url.rstrip('。，；！？、）】》.,;!')
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname:
                continue
        except ValueError:
            continue
        if url not in urls:
            urls.append(url)
    for url in urls:
        if kind.startswith('channel_') or kind == 'video_account_intro':
            continue
        host = (urlsplit(url).hostname or '').lower()
        category = 'article' if host == 'mp.weixin.qq.com' else 'channel' if host in {'channels.weixin.qq.com', 'finder.video.qq.com'} else 'link'
        items.append((category, url, decoded.title or url))
    return items

def list_materials(repository, *, category='', query='', offset=0, limit=50, allowed_conversations=None, from_timestamp=None, to_timestamp=None, include_unselected=False, link_messages=False):
    if category and category not in KINDS | {'media'}:
        raise ValueError('invalid_content_category')
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError('invalid_content_pagination')
    needle = query.strip().casefold()
    with _cache_lock:
        revision = _revision(repository)
        key = (revision, needle, None if allowed_conversations is None else tuple(sorted(allowed_conversations)), from_timestamp, to_timestamp, include_unselected, link_messages)
        cached = _snapshots.get(key) if revision is not None else None
        if cached is None:
            cached = _collect_materials(repository, needle, allowed_conversations, from_timestamp, to_timestamp, include_unselected, link_messages)
            # A concurrent sync must never label an old snapshot as current.
            if revision is not None and revision == _revision(repository):
                for previous in list(_snapshots):
                    if previous[0][:2] == revision[:2] and previous[0] != revision:
                        del _snapshots[previous]
                _snapshots[key] = cached
                if len(_snapshots) > 12:
                    _snapshots.popitem(last=False)
        else:
            _snapshots.move_to_end(key)
        categories, counts = cached
        items = categories[category]
        return deepcopy({'items':items[offset:offset+limit], 'total':len(items), 'counts':counts, 'next_offset':offset+limit if offset+limit<len(items) else None})

def _collect_materials(repository, needle, allowed_conversations, from_timestamp, to_timestamp, include_unselected, link_messages=False):
    groups = {}
    seen_sources = {}
    counts = dict.fromkeys(sorted(KINDS), 0)
    with repository._connect() as con:
        media_table = 'media_exports' if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_exports'").fetchone() else '(SELECT NULL AS uid, NULL AS status)'
        rows = con.execute(f'''SELECT m.*, c.username, c.display_name, c.is_group,
            sc.display_name AS sender_name, sm.username AS sender_username, e.status AS media_state
            FROM messages m JOIN conversations c ON c.account=m.account AND c.chat_table=m.chat_table
            LEFT JOIN conversation_selection s ON s.account=c.account AND s.username=c.username
            LEFT JOIN sender_map sm ON sm.account=m.account AND sm.source_db=m.source_db AND sm.sender_rowid=CAST(m.real_sender_id AS INTEGER)
            LEFT JOIN contacts sc ON sc.account=m.account AND sc.username=sm.username
            LEFT JOIN {media_table} e ON e.uid=m.uid
            WHERE m.account=? AND (? OR s.state IN ('following','paused'))
              AND (? IS NULL OR c.username IN (SELECT value FROM json_each(?)))
              AND (? IS NULL OR m.create_time>=?) AND (? IS NULL OR m.create_time<?)
            ORDER BY m.create_time DESC,m.uid DESC''',
            (repository.account, include_unselected, None if allowed_conversations is None else 1,
             json.dumps(allowed_conversations or []), from_timestamp, from_timestamp, to_timestamp, to_timestamp))
        for row in rows:
            decoded, extracted = _decode_cached(repository, row)
            for kind, url, title in extracted:
                if needle and needle not in ' '.join([title, decoded.text or '', url, row['display_name'], row['sender_name'] or '']).casefold():
                    continue
                identity = row['uid'] if link_messages and kind == 'link' else url or decoded.media_md5 or row['uid']
                key = hashlib.sha256(f'{repository.account}:{kind}:{identity}'.encode()).hexdigest()
                source = {'message_uid':row['uid'], 'conversation_id':row['username'], 'conversation_name':row['display_name'], 'is_group':bool(row['is_group']), 'sender_username':row['sender_username'], 'sender':row['sender_name'] or decoded.sender_override or row['sender_username'] or '未知发送者', 'timestamp':row['create_time']}
                if key not in groups:
                    groups[key] = {'id':key, 'category':kind, 'title':title[:500], 'url':url or None, 'text':decoded.text[:2000], 'availability':row['media_state'] or ('link_only' if url else 'message_only'), 'sources':[]}
                    counts[kind] += 1
                    seen_sources[key] = set()
                if row['uid'] not in seen_sources[key]:
                    seen_sources[key].add(row['uid'])
                    groups[key]['sources'].append(source)
    categories = {kind: [] for kind in KINDS | {'', 'media'}}
    for item in groups.values():
        categories[''].append(item)
        categories[item['category']].append(item)
        if item['category'] in {'image', 'video'}:
            categories['media'].append(item)
    return categories, counts
