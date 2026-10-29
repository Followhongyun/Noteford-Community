from __future__ import annotations

import base64
import hashlib
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import zstandard as zstd


ZSTD_MAGIC = b'\x28\xb5\x2f\xfd'

TYPE_LABELS = {
    1: 'text',
    3: 'image',
    34: 'voice',
    42: 'contact_card',
    43: 'video',
    47: 'emoji',
    48: 'location',
    49: 'app',
    50: 'voip',
    10000: 'system',
    10002: 'revoke',
}

APP_SUBTYPE_LABELS = {
    1: 'text_share', 2: 'image_share', 3: 'audio_share', 4: 'video_share',
    5: 'link', 6: 'file', 7: 'webview', 8: 'gif', 10: 'location_sharing',
    13: 'brand', 14: 'chat_log_backup', 15: 'chat_log_migrate',
    16: 'card_ticket', 17: 'realtime_location', 19: 'merged_messages',
    21: 'mini_program_promo', 24: 'note', 33: 'mini_program',
    35: 'message_history', 36: 'mini_program', 40: 'channel_forward',
    44: 'channel_live_product', 51: 'channel_video', 53: 'group_chat_reference',
    57: 'quote', 62: 'pat', 63: 'channel_live', 74: 'channel_files',
    87: 'group_announcement', 88: 'group_note', 92: 'music',
    100: 'sticker_set', 101: 'ad', 107: 'open_link',
    113: 'video_account_intro', 116: 'channel_show_card', 117: 'channel_product',
    124: 'wechat_gift', 2000: 'transfer', 2001: 'red_envelope',
    2003: 'red_envelope_cover',
}


@dataclass
class DecodedContent:
    msg_type: int
    sub_type: int
    kind: str
    text: str
    sender_override: str | None = None
    url: str | None = None
    title: str | None = None
    media_md5: str | None = None
    thumbnail_url: str | None = None
    cover_url: str | None = None
    avatar_url: str | None = None
    author_username: str | None = None
    quote_kind: str | None = None
    quote_text: str | None = None
    quote_sender_name: str | None = None
    quote_media_md5: str | None = None
    quote_timestamp: int | None = None
    quote_server_id: str | None = None
    decode_error: str | None = None
    solitaire_key: str | None = None
    payment_receiver: str | None = None
    payment_expires_at: int | None = None
    source_display_name: str | None = None


def split_local_type(local_type: int | None) -> tuple[int, int]:
    value = int(local_type or 0)
    return value & 0xFFFFFFFF, (value >> 32) & 0xFFFFFFFF


def type_label(msg_type: int, sub_type: int) -> str:
    if msg_type == 49:
        return APP_SUBTYPE_LABELS.get(sub_type, 'app')
    return TYPE_LABELS.get(msg_type, 'unknown')


def _as_bytes(value: Any) -> bytes:
    if value is None:
        return b''
    if isinstance(value, bytes):
        return value
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode('utf-8', 'surrogatepass')
    return str(value).encode('utf-8', 'replace')


def decode_text_blob(value: Any) -> tuple[str, str | None]:
    raw = _as_bytes(value)
    if not raw:
        return '', None
    try:
        if raw.startswith(ZSTD_MAGIC):
            raw = zstd.ZstdDecompressor().decompress(raw, max_output_size=16 * 1024 * 1024)
        return raw.decode('utf-8', 'replace'), None
    except Exception as exc:
        # Never surface raw bytes or exception internals to reports.
        return '', exc.__class__.__name__


def _group_sender(text: str, is_group: bool) -> tuple[str | None, str]:
    if not is_group or ':\n' not in text:
        return None, text
    prefix, rest = text.split(':\n', 1)
    # wxid / openim / numeric member IDs contain no whitespace. Avoid splitting
    # normal prose that merely happens to contain a colon and newline.
    if prefix and len(prefix) <= 128 and not re.search(r'\s', prefix):
        return prefix, rest
    return None, text


def _xml_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    if '<' not in text or '>' not in text:
        return fields
    try:
        root = ET.fromstring(text)
        for tag in (
            'sourcedisplayname', 'title', 'des', 'url', 'appname', 'filename', 'fileext',
            'displayname', 'content', 'replacemsg', 'plain', 'pay_memo',
            'feeddesc', 'nickname', 'thumburl', 'thumbUrl', 'coverUrl',
            'avatar', 'authIconUrl', 'username', 'feedesc',
            'senderdes', 'receiverdes', 'scenetext', 'poiname', 'label',
        ):
            node = root.find('.//' + tag)
            if node is not None and node.text and node.text.strip():
                fields[tag] = node.text.strip()
    except ET.ParseError:
        # WeChat XML is not always strictly well-formed. Fall back to a bounded
        # tag extractor for the few fields useful to a daily summary.
        for tag in (
            'sourcedisplayname', 'title', 'des', 'url', 'displayname', 'content', 'replacemsg',
            'pay_memo', 'feeddesc', 'nickname', 'appname', 'username',
            'thumburl', 'thumbUrl', 'coverUrl', 'avatar', 'authIconUrl',
            'feedesc', 'senderdes', 'receiverdes', 'scenetext', 'poiname', 'label'
        ):
            match = re.search(
                rf'<{tag}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>',
                text,
                re.I | re.S,
            )
            if match:
                value = re.sub(r'<[^>]+>', '', match.group(1)).strip()
                if value:
                    fields[tag] = value
    return fields


def _payment_fields(text: str, *, red_envelope: bool = False) -> tuple[str | None, int | None]:
    """Only explicit payment metadata; never infer from labels or native URLs."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None, None
    pay = root.find('./appmsg/wcpayinfo') if root.tag != 'appmsg' else root.find('./wcpayinfo')
    if pay is None:
        return None, None
    # A red envelope's generic receiver is not necessarily its eligible user.
    # Only the explicit exclusive-recipient field establishes that restriction.
    receiver = (pay.findtext('exclusive_recv_username' if red_envelope else 'receiver_username') or '').strip()
    if not receiver or len(receiver) > 128 or not re.fullmatch(r'[A-Za-z0-9_@.\-]+', receiver):
        receiver = None
    raw_expiry = (pay.findtext('invalidtime') or '').strip()
    expires = int(raw_expiry) if raw_expiry.isascii() and raw_expiry.isdigit() and len(raw_expiry) == 10 else None
    if expires is not None and not 946684800 <= expires <= 4102444800:
        expires = None
    return receiver, expires


def _read_varint(data: bytes, offset: int) -> tuple[int, int] | None:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, offset
        shift += 7
    return None


def _protobuf_length_fields(data: bytes) -> dict[int, list[bytes]]:
    fields: dict[int, list[bytes]] = {}
    offset = 0
    while offset < len(data):
        head = _read_varint(data, offset)
        if head is None:
            break
        key, offset = head
        field = key >> 3
        wire = key & 0x07
        if wire == 0:
            skipped = _read_varint(data, offset)
            if skipped is None:
                break
            _, offset = skipped
        elif wire == 1:
            offset += 8
        elif wire == 2:
            length_info = _read_varint(data, offset)
            if length_info is None:
                break
            length, offset = length_info
            end = offset + length
            if end > len(data):
                break
            fields.setdefault(field, []).append(data[offset:end])
            offset = end
        elif wire == 5:
            offset += 4
        else:
            break
    return fields


def decode_packed_media_md5(value: Any, msg_type: int) -> str | None:
    raw = _as_bytes(value)
    if not raw:
        return None
    outer = _protobuf_length_fields(raw)
    nested_field = 3 if msg_type == 3 else 4 if msg_type == 43 else None
    md5_field = 4 if msg_type == 3 else 8 if msg_type == 43 else None
    if nested_field is None or md5_field is None:
        return None
    for nested in outer.get(nested_field, []):
        values = _protobuf_length_fields(nested).get(md5_field, [])
        for value in values:
            try:
                text = value.decode('ascii').lower()
            except UnicodeDecodeError:
                continue
            if re.fullmatch(r'[0-9a-f]{32}', text):
                return text
    return None


def _placeholder(msg_type: int, sub_type: int) -> str:
    kind = type_label(msg_type, sub_type)
    labels = {
        'image': '[图片]', 'voice': '[语音]', 'video': '[视频]',
        'emoji': '[表情]', 'contact_card': '[名片]', 'location': '[位置]', 'voip': '[音视频通话]',
        'file': '[文件]', 'mini_program': '[小程序]', 'merged_messages': '[合并聊天记录]',
        'channel_video': '[视频号]', 'quote': '[引用回复]', 'pat': '[拍一拍]',
        'transfer': '[转账]', 'red_envelope': '[红包]', 'group_announcement': '[群公告]',
        'system': '[系统消息]', 'revoke': '[撤回消息]',
    }
    return labels.get(kind, f'[{kind}]')


def _safe_xml_root(text: str) -> ET.Element | None:
    if '<' not in text or '>' not in text:
        return None
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def _node_text(node: ET.Element | None, path: str) -> str | None:
    if node is None:
        return None
    child = node.find(path)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _clean_markup(text: str) -> str:
    value = html.unescape(text or '')
    value = re.sub(r'<img\b[^>]*?/?>', '', value, flags=re.I | re.S)
    value = re.sub(
        r'<_wc_custom_link_\b[^>]*>(.*?)</_wc_custom_link_>',
        r'\1',
        value,
        flags=re.I | re.S,
    )
    value = re.sub(r'<[^>]+>', '', value)
    value = re.sub(r'\$[^$]{1,128}\$', '对方', value)
    value = re.sub(r'[ \t]+\n', '\n', value)
    value = re.sub(r'\n{3,}', '\n\n', value)
    return value.strip()


def _quote_details(text: str) -> dict[str, Any]:
    root = _safe_xml_root(text)
    if root is None:
        return {}
    appmsg = root.find('.//appmsg')
    refer = appmsg.find('refermsg') if appmsg is not None else None
    if appmsg is None or refer is None:
        return {}

    main_text = _node_text(appmsg, 'title') or '[引用回复]'
    sender_name = _node_text(refer, 'displayname')
    raw_content = _node_text(refer, 'content') or ''
    quoted_type_raw = _node_text(refer, 'type')
    try:
        quoted_type = int(quoted_type_raw or 0)
    except ValueError:
        quoted_type = 0

    quoted_kind = TYPE_LABELS.get(quoted_type, 'message')
    quoted_text = raw_content.strip()
    quote_md5 = None

    if quoted_type == 1:
        quoted_kind = 'text'
    elif quoted_type == 3:
        quoted_kind = 'image'
        quoted_text = '[图片]'
        match = re.search(
            r'<img\b[^>]*\bmd5=["\']([0-9a-fA-F]{32})["\']',
            raw_content,
            re.I | re.S,
        )
        if match:
            quote_md5 = match.group(1).lower()
    elif quoted_type == 34:
        quoted_kind = 'voice'
        quoted_text = '[语音]'
    elif quoted_type == 43:
        quoted_kind = 'video'
        quoted_text = '[视频]'
    elif quoted_type == 47:
        quoted_kind = 'emoji'
        quoted_text = '[表情]'
    elif quoted_type == 48:
        quoted_kind = 'location'
        quoted_text = '[位置]'
    elif quoted_type == 49:
        nested_fields = _xml_fields(raw_content)
        subtype_match = re.search(r'<type[^>]*>(\d+)</type>', raw_content, re.I)
        nested_subtype = int(subtype_match.group(1)) if subtype_match else 0
        quoted_kind = type_label(49, nested_subtype)
        quoted_text = (
            nested_fields.get('title')
            or nested_fields.get('des')
            or _placeholder(49, nested_subtype)
        )
    elif quoted_type == 42:
        quoted_kind = 'contact_card'
        quoted_text = '[名片]'
    elif '<' in quoted_text and '>' in quoted_text:
        quoted_text = _clean_markup(quoted_text) or '[消息]'

    timestamp_raw = _node_text(refer, 'createtime')
    try:
        quote_timestamp = int(timestamp_raw or 0) or None
    except ValueError:
        quote_timestamp = None
    quote_server_id = _node_text(refer, 'svrid')

    return {
        'main_text': main_text,
        'quote_kind': quoted_kind,
        'quote_text': quoted_text or '[消息]',
        'quote_sender_name': sender_name,
        'quote_media_md5': quote_md5,
        'quote_timestamp': quote_timestamp,
        'quote_server_id': quote_server_id,
    }


def _emoji_details(text: str) -> dict[str, str | None]:
    root = _safe_xml_root(text)
    if root is None:
        return {}
    emoji = root.find('.//emoji')
    if emoji is None:
        return {}
    return {
        'md5': (emoji.attrib.get('md5') or '').strip().lower() or None,
        'url': (
            (emoji.attrib.get('cdnurl') or '').strip()
            or (emoji.attrib.get('thumburl') or '').strip()
            or (emoji.attrib.get('externurl') or '').strip()
            or None
        ),
        'desc': (emoji.attrib.get('desc') or '').strip() or None,
    }


def _gif_details(text: str) -> dict[str, str | None]:
    root = _safe_xml_root(text)
    if root is None:
        return {}
    appmsg = root.find('.//appmsg')
    if appmsg is None:
        return {}
    md5 = _node_text(appmsg, './/emoticonmd5')
    encoded = _node_text(appmsg, './/emojiinfo')
    url = None
    if encoded:
        try:
            raw = base64.b64decode(encoded, validate=False)
            match = re.search(
                rb'https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%\-]+',
                raw,
            )
            if match:
                url = match.group(0).decode('ascii', 'ignore')
        except (ValueError, TypeError):
            pass
    return {
        'md5': (md5 or '').lower() or None,
        'url': url,
    }


def _contact_card_details(text: str) -> dict[str, str | None]:
    root = _safe_xml_root(text)
    if root is None:
        return {}
    node = root if root.tag == 'msg' else root.find('.//msg')
    if node is None:
        return {}
    return {
        'nickname': (
            (node.attrib.get('nickname') or '').strip()
            or (node.attrib.get('displayname') or '').strip()
            or None
        ),
        'username': (node.attrib.get('username') or '').strip() or None,
        'avatar': (
            (node.attrib.get('bigheadimgurl') or '').strip()
            or (node.attrib.get('smallheadimgurl') or '').strip()
            or None
        ),
    }


def _location_details(text: str) -> dict[str, str | None]:
    root = _safe_xml_root(text)
    if root is None:
        return {}
    node = root.find('.//location')
    if node is None:
        return {}
    poiname = (node.attrib.get('poiname') or '').strip() or None
    label = (node.attrib.get('label') or '').strip() or None
    return {
        'title': poiname or label,
        'text': label or poiname,
    }


def _render_sysmsg_template(root: ET.Element) -> str | None:
    content_template = root.find('.//content_template')
    if content_template is None:
        return None
    template = _node_text(content_template, 'template')
    plain = _node_text(content_template, 'plain')
    if plain:
        return _clean_markup(plain)
    if not template:
        return None

    rendered = template
    for link in content_template.findall('.//link'):
        name = (link.attrib.get('name') or '').strip()
        if not name:
            continue
        values: list[str] = []
        for member in link.findall('.//member'):
            nickname = _node_text(member, 'nickname')
            if nickname:
                values.append(nickname)
        if not values:
            plain_value = _node_text(link, 'plain')
            if plain_value:
                values.append(plain_value)
        replacement = '、'.join(values) if values else name
        rendered = rendered.replace('$' + name + '$', replacement)
    return _clean_markup(rendered)


def _render_system_message(text: str) -> str:
    if '<' not in text or '>' not in text:
        return text.strip() or '[系统消息]'

    root = _safe_xml_root(text)
    if root is not None and root.tag == 'sysmsg':
        sys_type = (root.attrib.get('type') or '').strip().lower()
        if sys_type == 'revokemsg':
            return (
                _node_text(root, './/revokemsg/content')
                or '撤回了一条消息'
            )
        if sys_type == 'mmchatroomtopmsg':
            nickname = _node_text(root, './/mmchatroomtopmsg/nickname') or '群成员'
            op = _node_text(root, './/mmchatroomtopmsg/op')
            return (
                f'{nickname} 取消置顶了一条消息'
                if op in {'0', '2'}
                else f'{nickname} 置顶了一条消息'
            )
        if sys_type == 'sysmsgtemplate':
            rendered = _render_sysmsg_template(root)
            if rendered:
                return rendered
        if sys_type == 'paymsg':
            content = _node_text(root, 'content') or _node_text(root, './/content')
            if content:
                return _clean_markup(content)
        content = (
            _node_text(root, 'content')
            or _node_text(root, './/content')
            or _node_text(root, './/replacemsg')
        )
        if content:
            return _clean_markup(content)

    cleaned = _clean_markup(text)
    return cleaned or '[系统消息]'


def decode_message(
    local_type: int | None,
    message_content: Any,
    compress_content: Any = None,
    packed_info_data: Any = None,
    is_group: bool = False,
) -> DecodedContent:
    msg_type, sub_type = split_local_type(local_type)
    kind = type_label(msg_type, sub_type)

    text, error = decode_text_blob(message_content)
    sender_override, text = _group_sender(text, is_group)

    # App messages sometimes store the richer XML in compress_content.
    if msg_type == 49 and compress_content:
        alt, alt_error = decode_text_blob(compress_content)
        if alt and '<' in alt:
            text = alt
            error = alt_error

    if msg_type == 1:
        return DecodedContent(msg_type, sub_type, kind, text, sender_override, decode_error=error)

    fields = _xml_fields(text)
    title = fields.get('title') or fields.get('displayname') or fields.get('nickname')
    url = fields.get('url')
    media_md5 = decode_packed_media_md5(packed_info_data, msg_type)
    thumbnail_url = fields.get('thumbUrl') or fields.get('thumburl')
    cover_url = fields.get('coverUrl') or thumbnail_url
    avatar_url = fields.get('avatar') or fields.get('authIconUrl')
    author_username = fields.get('username')

    if msg_type == 49:
        if kind == 'group_announcement':
            root = _safe_xml_root(text)
            app = root if root is not None and root.tag == 'appmsg' else (
                root.find('./appmsg') if root is not None else None
            )
            # Prefer WeChat's plain-text snapshot. The rich payload also contains
            # CDN keys and source metadata: only extract its text data items.
            body = _node_text(app, 'textannouncement')
            if not body:
                rich = _safe_xml_root(_node_text(app, 'announcement') or '')
                if rich is not None and rich.tag == 'group_notice_item':
                    parts = [
                        _node_text(item, 'datadesc')
                        for item in rich.findall('./datalist/dataitem')
                        if item.get('datatype') == '1'
                    ]
                    body = '\n'.join(part for part in parts if part) or None
            return DecodedContent(
                msg_type, sub_type, kind,
                body or '公告正文尚未同步，请在微信查看。',
                sender_override, title='群公告', decode_error=error,
            )

        if kind == 'quote':
            quote = _quote_details(text)
            return DecodedContent(
                msg_type,
                sub_type,
                kind,
                quote.get('main_text') or title or '[引用回复]',
                sender_override,
                title=quote.get('main_text') or title,
                quote_kind=quote.get('quote_kind'),
                quote_text=quote.get('quote_text'),
                quote_sender_name=quote.get('quote_sender_name'),
                quote_media_md5=quote.get('quote_media_md5'),
                quote_timestamp=quote.get('quote_timestamp'),
                quote_server_id=quote.get('quote_server_id'),
                decode_error=error,
            )

        if kind == 'gif':
            gif = _gif_details(text)
            return DecodedContent(
                msg_type,
                sub_type,
                kind,
                '[表情]',
                sender_override,
                media_md5=gif.get('md5'),
                thumbnail_url=gif.get('url'),
                decode_error=error,
            )

        if kind in {'transfer', 'red_envelope'}:
            payment_receiver, payment_expires_at = _payment_fields(text, red_envelope=kind == 'red_envelope')

        if kind == 'transfer':
            amount = fields.get('feedesc') or title or '微信转账'
            memo = fields.get('pay_memo') or '微信转账'
            return DecodedContent(
                msg_type,
                sub_type,
                kind,
                memo,
                sender_override,
                title=amount,
                payment_receiver=payment_receiver,
                payment_expires_at=payment_expires_at,
                decode_error=error,
            )

        if kind == 'red_envelope':
            label = (
                fields.get('senderdes')
                or fields.get('receiverdes')
                or fields.get('scenetext')
                or '微信红包'
            )
            return DecodedContent(
                msg_type,
                sub_type,
                kind,
                label,
                sender_override,
                title='微信红包',
                payment_receiver=payment_receiver,
                payment_expires_at=payment_expires_at,
                decode_error=error,
            )

        if kind in {
            'channel_video', 'channel_forward', 'channel_live',
            'channel_show_card', 'video_account_intro'
        }:
            channel_name = (
                fields.get('nickname')
                or fields.get('appname')
                or '视频号'
            )
            description = (
                fields.get('feeddesc')
                or fields.get('des')
                or fields.get('content')
            )
            rendered = description or _placeholder(msg_type, sub_type)
            return DecodedContent(
                msg_type, sub_type, kind, rendered, sender_override,
                url=None, title=channel_name, media_md5=media_md5,
                thumbnail_url=thumbnail_url, cover_url=cover_url,
                avatar_url=avatar_url, author_username=author_username,
                source_display_name=fields.get('sourcedisplayname') or fields.get('appname'),
                decode_error=error
            )

        if kind == 'group_chat_reference':
            # The outer title contains the human-readable snapshot. solitaire_info
            # tt/ex/r can be offset-length references, not title/example/entry text.
            # Never concatenate those internal values into the displayed message.
            body = title or '接龙正文尚未同步，请在微信查看。'
            solitaire_key = None
            try:
                outer = ET.fromstring(text)
                app = outer if outer.tag == 'appmsg' else outer.find('./appmsg')
                info = app.find('./extinfo/solitaire_info') if app is not None else None
                inner = ET.fromstring(info.text or '') if info is not None else None
                ids = inner.findall('./sid') if inner is not None and inner.tag == 'solitaire' else []
                sid = ids[0].text if len(ids) == 1 else None
                if sid and len(sid) <= 256 and sid.isascii() and all(32 < ord(char) < 127 for char in sid):
                    # Native identity only. Never derive identity from prose,
                    # initiator or participant names shared by separate chains.
                    solitaire_key = hashlib.sha256(('wechat-solitaire-sid:' + sid).encode('ascii')).hexdigest()
            except ET.ParseError:
                pass
            return DecodedContent(msg_type, sub_type, kind, body, sender_override,
                                  title='群接龙', solitaire_key=solitaire_key, decode_error=error)

        pieces = []
        if title:
            safe_title = _clean_markup(title) if '<' in title or '&lt;' in title else title.strip()
            if safe_title:
                pieces.append(safe_title)
        description = fields.get('des') or fields.get('content') or fields.get('replacemsg')
        if description:
            safe_description = (
                _clean_markup(description)
                if '<' in description or '&lt;' in description
                else description.strip()
            )
            if safe_description and safe_description not in pieces:
                pieces.append(safe_description)
        rendered = '：'.join(pieces[:2]) if pieces else _placeholder(msg_type, sub_type)
        return DecodedContent(
            msg_type, sub_type, kind, rendered, sender_override,
            url=url, title=title, media_md5=media_md5,
            thumbnail_url=thumbnail_url, cover_url=cover_url,
            avatar_url=avatar_url, author_username=author_username,
            source_display_name=fields.get('sourcedisplayname') or fields.get('appname'),
            decode_error=error
        )

    if msg_type in (10000, 10002):
        rendered = _render_system_message(text)
        return DecodedContent(
            msg_type, sub_type, kind, rendered, sender_override,
            media_md5=media_md5, decode_error=error
        )

    if msg_type == 47:
        emoji = _emoji_details(text)
        return DecodedContent(
            msg_type,
            sub_type,
            kind,
            emoji.get('desc') or '[表情]',
            sender_override,
            media_md5=emoji.get('md5'),
            thumbnail_url=emoji.get('url'),
            decode_error=error,
        )

    if msg_type == 48:
        location = _location_details(text)
        return DecodedContent(
            msg_type,
            sub_type,
            kind,
            location.get('text') or '[位置]',
            sender_override,
            title=location.get('title'),
            decode_error=error,
        )

    if msg_type == 42:
        card = _contact_card_details(text)
        return DecodedContent(
            msg_type,
            sub_type,
            kind,
            '[名片]',
            sender_override,
            title=card.get('nickname') or '联系人名片',
            avatar_url=card.get('avatar'),
            author_username=card.get('username'),
            decode_error=error,
        )

    return DecodedContent(
        msg_type, sub_type, kind, _placeholder(msg_type, sub_type),
        sender_override, url=url, title=title, media_md5=media_md5,
        thumbnail_url=thumbnail_url, cover_url=cover_url,
        avatar_url=avatar_url, author_username=author_username,
        source_display_name=fields.get('sourcedisplayname') or fields.get('appname'),
        decode_error=error
    )
