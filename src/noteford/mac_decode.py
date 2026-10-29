"""Mac additions to the shared message decoder; Windows keeps its decoder."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, urlsplit
from .decode import decode_message as shared_decode, decode_text_blob


def _text(node, path: str) -> str:
    found = node.find(path) if node is not None else None
    return (found.text or '').strip() if found is not None else ''


def _xml(value: str):
    if not value or len(value) > 4 * 1024 * 1024 or '<!DOCTYPE' in value.upper() or '<!ENTITY' in value.upper():
        return None
    try:
        return ET.fromstring(value)
    except ET.ParseError:
        return None


def app_message_xml(message_content, compress_content=None):
    """Reuse the Mac blob/alternate-XML and sender-prefix decoding rules."""
    raw, _ = decode_text_blob(message_content)
    if compress_content:
        richer, _ = decode_text_blob(compress_content)
        if richer and '<' in richer:
            raw = richer
    root = _xml(raw[raw.find('<'):] if '<' in raw else raw)
    return root if root is not None and root.tag == 'appmsg' else (
        root.find('./appmsg') if root is not None else None)


def parse_finder_feed(xml) -> dict | None:
    """Parse a type-51 feed without changing the existing DecodedContent API.

    Missing text and invalid/missing integers are None. No URL is fetched.
    """
    app = app_message_xml(xml)
    feed = app.find('./finderFeed') if app is not None else None
    if feed is None:
        return None

    def integer(node, tag):
        try:
            return int(_text(node, tag))
        except ValueError:
            return None

    media = []
    cover = None
    for node in feed.findall('./mediaList/media'):
        url = _text(node, 'url') or None
        url_token = _text(node, 'urlToken') or None
        if not url_token and url:
            try:
                url_token = parse_qs(urlsplit(url).query).get('token', [None])[0]
            except ValueError:
                pass
        item = {
            'url': url, 'url_token': url_token,
            'decode_key': _text(node, 'decodeKey') or None,
            'duration': integer(node, 'videoPlayDuration'),
            'media_type': integer(node, 'mediaType'),
            'thumb_url': _text(node, 'thumbUrl') or None,
            'cover_url': _text(node, 'coverUrl') or None,
            'full_cover_url': _text(node, 'fullCoverUrl') or None,
            'width': integer(node, 'width'), 'height': integer(node, 'height'),
        }
        cover = cover or item['full_cover_url'] or item['cover_url'] or item['thumb_url']
        media.append(item)
    return {
        'object_id': _text(feed, 'objectId') or None,
        'object_nonce_id': _text(feed, 'objectNonceId') or None,
        'nickname': _text(feed, 'nickname') or None,
        'username': _text(feed, 'username') or None,
        'desc': _text(feed, 'desc') or None,
        'feed_type': integer(feed, 'feedType'), 'cover_url': cover, 'media': media,
    }


def _record_text(record, depth=0, budget=None) -> list[str]:
    if record is None:
        return []
    if depth >= 5:
        return ['[嵌套聊天记录过深，暂未展开]']
    budget = budget if budget is not None else [2000]
    result = []
    labels = {'2':'图片','3':'语音','4':'视频','5':'链接','6':'位置','7':'音频',
              '8':'文件','16':'名片','17':'聊天记录','19':'小程序'}
    for item in record.findall('./datalist/dataitem'):
        if budget[0] <= 0:
            result.append('[聊天记录较长，剩余条目暂未展开]')
            break
        budget[0] -= 1
        kind = item.get('datatype', '')
        sender = _text(item, 'sourcename') or _text(item, 'datasource/sourcename')
        stamp = _text(item, 'sourcetime')
        body = _text(item, 'datadesc') if kind == '1' else ''
        if kind == '17':
            nested = item.find('./recordinfo')
            if nested is None:
                nested = _xml(_text(item, 'recordxml') or _text(item, 'recorditem'))
            body = '\n'.join(_record_text(nested, depth + 1, budget))
        if not body:
            title = _text(item, 'datatitle')
            body = '[' + labels.get(kind, '暂不支持的消息') + ']' + (' ' + title if title else '')
        heading = ' · '.join(part for part in (sender, stamp) if part)
        result.append((heading + '\n' if heading else '') + body)
    return result


def decode_message(local_type, message_content, compress_content=None, packed_info_data=None, is_group=False):
    decoded = shared_decode(local_type, message_content, compress_content, packed_info_data, is_group)
    if decoded.msg_type != 49:
        return decoded
    raw, _ = decode_text_blob(message_content)
    if compress_content:
        richer, _ = decode_text_blob(compress_content)
        if richer and '<' in richer:
            raw = richer
    # Group sender prefix precedes the actual XML document.
    root = _xml(raw[raw.find('<'):] if '<' in raw else raw)
    app = root if root is not None and root.tag == 'appmsg' else (root.find('./appmsg') if root is not None else None)
    if app is None:
        return decoded
    if decoded.kind == 'app' and decoded.sub_type == 109 and app.find('./weappinfo') is not None:
        decoded.kind = 'mini_program'
    if decoded.kind in {'mini_program', 'mini_program_promo'}:
        # Page cover and app icon are distinct from encrypted CDN attachments.
        cover = _text(app, 'weapppagethumbrawurl')
        if cover.startswith(('https://', 'http://')) and not decoded.thumbnail_url:
            decoded.thumbnail_url = decoded.cover_url = cover
        icon = _text(app, 'weappinfo/weappiconurl')
        if icon.startswith(('https://', 'http://')):
            decoded.avatar_url = icon
    if decoded.kind.startswith('channel_') or decoded.kind == 'video_account_intro':
        feed = app.find('./finderFeed')
        if feed is not None:
            decoded.text = _text(feed, 'desc') or _text(feed, 'feeddesc') or decoded.text
            decoded.title = _text(feed, 'nickname') or decoded.title
            decoded.avatar_url = _text(feed, 'avatar') or decoded.avatar_url
            decoded.author_username = _text(feed, 'username') or decoded.author_username
            media = feed.find('./mediaList/media')
            decoded.cover_url = _text(media, 'fullCoverUrl') or _text(media, 'coverUrl') or decoded.cover_url
    if decoded.kind in {'merged_messages', 'message_history'}:
        record = _xml(_text(app, 'recorditem'))
        if record is None:
            record = app.find('./recordinfo')
        rows = _record_text(record)
        decoded.text = '\n\n'.join(rows) if rows else '聊天记录正文尚未同步。'
    if decoded.kind == 'link':
        articles = app.findall('./mmreader/category/item')
        if len(articles) > 1:
            # Flattening XML fields loses all but one article in a subscription
            # digest. Retain each article's own title, body and original URL.
            pieces = []
            for number, article in enumerate(articles[:100], 1):
                title = _text(article, 'title') or '文章'
                description = _text(article, 'digest')
                url = _text(article, 'url')
                pieces.append('\n'.join(part for part in (
                    f'{number}. {title}', description,
                    url if url.startswith(('https://', 'http://')) else '',
                ) if part))
            if len(articles) > 100:
                pieces.append('[合集较长，其余文章暂未展开]')
            decoded.text = '\n\n'.join(pieces)
            decoded.title = _text(app, 'title') or '公众号文章合集'
            decoded.url = None  # Each article keeps its own link in the body.
            cover = _text(articles[0], 'cover')
            if cover.startswith(('https://', 'http://')):
                decoded.thumbnail_url = decoded.cover_url = cover
    return decoded
