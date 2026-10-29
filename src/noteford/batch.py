from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .decode import decode_message
from .media import MediaResolver


@dataclass
class BatchResult:
    directory: str
    messages_file: str
    conversations_file: str
    manifest_file: str
    message_count: int
    conversation_count: int
    decode_errors: int
    since: int
    until: int


def _safe_text(value: str, max_chars: int) -> tuple[str, bool]:
    value = value.replace('\x00', '').strip()
    if len(value) <= max_chars:
        return value, False
    return value[:max_chars] + '…', True


def _contact_names(con: sqlite3.Connection, account: str) -> dict[str, str]:
    return {
        str(row[0]): str(row[1])
        for row in con.execute(
            'SELECT username, display_name FROM contacts WHERE account=?',
            (account,),
        )
    }


def _self_username(con: sqlite3.Connection, account: str) -> str | None:
    row = con.execute(
        'SELECT self_username FROM account_profile WHERE account=?',
        (account,),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def export_daily_batch(
    index_path: Path,
    account: str,
    since: int,
    until: int,
    output_root: Path,
    max_chars: int = 20_000,
    wechat_root: Path | None = None,
    media_db_paths: list[Path] | None = None,
    media_cache_root: Path | None = None,
    image_keys_path: Path | None = None,
    voice_decoder_path: Path | None = None,
    selected_only: bool = False,
) -> BatchResult:
    if until < since:
        raise ValueError('until must be >= since')
    if not index_path.is_file():
        raise FileNotFoundError(index_path)

    con = sqlite3.connect(index_path)
    con.row_factory = sqlite3.Row
    try:
        contacts = _contact_names(con, account)
        self_username = _self_username(con, account)
        selection_join = (
            '''
            JOIN conversation_selection cs
              ON cs.account=c.account AND cs.username=c.username
             AND cs.state='following'
            '''
            if selected_only
            else ''
        )
        query = f'''
        SELECT
            m.uid, m.source_db, m.chat_table, m.local_id, m.server_id,
            m.local_type, m.sort_seq, m.real_sender_id, m.create_time,
            m.status, m.message_content, m.compress_content,
            m.packed_info_data, m.wcdb_ct,
            c.username AS chat_username,
            c.display_name AS chat_display_name,
            c.is_group AS is_group,
            sm.username AS sender_username
        FROM messages m
        JOIN conversations c
          ON c.account=m.account AND c.chat_table=m.chat_table
        {selection_join}
        LEFT JOIN sender_map sm
          ON sm.account=m.account AND sm.source_db=m.source_db
         AND sm.sender_rowid=CAST(m.real_sender_id AS INTEGER)
        WHERE m.account=? AND m.create_time>=? AND m.create_time<=?
        ORDER BY m.create_time ASC, m.sort_seq ASC, m.server_id ASC, m.uid ASC
        '''
        rows = con.execute(query, (account, since, until))

        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        folder = output_root / f'batch-{stamp}'
        folder.mkdir(parents=True, exist_ok=False)
        messages_path = folder / 'messages.jsonl'
        conversations_path = folder / 'conversations.jsonl'
        manifest_path = folder / 'manifest.json'

        message_count = 0
        decode_errors = 0
        chat_counts: Counter[str] = Counter()
        kind_counts: Counter[str] = Counter()
        sender_unknown = 0
        media_status_counts: Counter[str] = Counter()
        conversation_packets: dict[str, dict[str, Any]] = {}
        resolver = (
            MediaResolver(
                wechat_root,
                account,
                media_db_paths=media_db_paths,
                image_keys_path=image_keys_path,
                voice_decoder_path=voice_decoder_path,
            )
            if wechat_root is not None
            else None
        )
        media_root = media_cache_root or (folder / 'media')

        with messages_path.open('w', encoding='utf-8', newline='\n') as handle:
            for row in rows:
                is_group = bool(row['is_group'])
                decoded = decode_message(
                    local_type=row['local_type'],
                    message_content=row['message_content'],
                    compress_content=row['compress_content'],
                    packed_info_data=row['packed_info_data'],
                    is_group=is_group,
                )

                sender_username = decoded.sender_override or row['sender_username']
                if decoded.kind in {'system', 'pat'} and not sender_username:
                    sender_username = 'system'
                sender_display = contacts.get(str(sender_username), str(sender_username or ''))
                if not sender_username:
                    sender_unknown += 1

                text, truncated = _safe_text(decoded.text, max_chars)
                if decoded.decode_error:
                    decode_errors += 1

                timestamp = int(row['create_time'] or 0)
                media = None
                if resolver is not None:
                    if decoded.kind == 'image':
                        media = resolver.export_image(
                            row['chat_table'], timestamp, decoded.media_md5, row['uid'], media_root
                        ).to_dict()
                    elif decoded.kind == 'video':
                        media = resolver.export_video(
                            timestamp, decoded.media_md5, row['uid'], media_root
                        ).to_dict()
                    elif decoded.kind == 'voice':
                        media = resolver.export_voice(
                            str(row['chat_username']), row['server_id'], row['uid'], media_root
                        ).to_dict()
                    elif decoded.kind == 'file':
                        media = resolver.export_file(
                            timestamp, decoded.title, row['uid'], media_root
                        ).to_dict()
                elif decoded.kind in {'image', 'video', 'voice', 'file'}:
                    media = {
                        'status': 'not_resolved',
                        'kind': decoded.kind,
                        'path': None,
                        'md5': decoded.media_md5,
                        'bytes': None,
                        'encrypted': decoded.kind == 'image',
                        'note': 'Media resolver was not configured for this export.',
                    }
                if media is not None:
                    media_status_counts[str(media['status'])] += 1

                item = {
                    'uid': row['uid'],
                    'timestamp': timestamp,
                    'time': datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                    'chat': {
                        'username': row['chat_username'],
                        'name': row['chat_display_name'],
                        'is_group': is_group,
                    },
                    'sender': {
                        'username': sender_username or None,
                        'name': sender_display or None,
                        'from_me': bool(sender_username and self_username and sender_username == self_username),
                    },
                    'type': {
                        'msg_type': decoded.msg_type,
                        'sub_type': decoded.sub_type,
                        'kind': decoded.kind,
                    },
                    'text': text,
                    'title': decoded.title,
                    'url': decoded.url,
                    'media': media,
                    'content_truncated': truncated,
                    'decode_error': decoded.decode_error,
                    'status': row['status'],
                    'source': {
                        'database': row['source_db'],
                        'table': row['chat_table'],
                        'local_id': row['local_id'],
                        'server_id': row['server_id'],
                        'sort_seq': row['sort_seq'],
                    },
                }
                handle.write(json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n')
                message_count += 1
                chat_username = str(row['chat_username'])
                chat_counts[chat_username] += 1
                kind_counts[decoded.kind] += 1

                packet = conversation_packets.setdefault(
                    chat_username,
                    {
                        'chat': item['chat'],
                        'first_timestamp': timestamp,
                        'last_timestamp': timestamp,
                        'messages': [],
                    },
                )
                packet['last_timestamp'] = timestamp
                packet['messages'].append(
                    {
                        'uid': item['uid'],
                        'timestamp': item['timestamp'],
                        'time': item['time'],
                        'sender': item['sender'],
                        'type': item['type'],
                        'text': item['text'],
                        'title': item['title'],
                        'url': item['url'],
                        'media': item['media'],
                    }
                )

        with conversations_path.open('w', encoding='utf-8', newline='\n') as handle:
            for packet in conversation_packets.values():
                packet['message_count'] = len(packet['messages'])
                handle.write(
                    json.dumps(packet, ensure_ascii=False, separators=(',', ':')) + '\n'
                )

        manifest = {
            'schema': 1,
            'account': account,
            'since': since,
            'until': until,
            'since_iso': datetime.fromtimestamp(since, timezone.utc).isoformat(),
            'until_iso': datetime.fromtimestamp(until, timezone.utc).isoformat(),
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'selected_only': bool(selected_only),
            'message_count': message_count,
            'conversation_count': len(chat_counts),
            'self_identity_resolved': bool(self_username),
            'decode_errors': decode_errors,
            'sender_unknown': sender_unknown,
            'kind_counts': dict(kind_counts.most_common()),
            'media_status_counts': dict(media_status_counts.most_common()),
            'unexpanded_media_messages': sum(
                count for status, count in media_status_counts.items()
                if status not in {'ready', 'ready_audio'}
            ),
            'unexpanded_media_kinds': {
                kind: kind_counts.get(kind, 0)
                for kind in ('image', 'voice', 'video')
                if kind_counts.get(kind, 0)
            },
            # Do not place private conversation names into a summary log. The
            # JSONL remains the private detailed artifact.
            'top_conversation_message_counts': sorted(chat_counts.values(), reverse=True)[:20],
            'messages_file': messages_path.name,
            'conversations_file': conversations_path.name,
        }
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

        return BatchResult(
            directory=str(folder.resolve()),
            messages_file=str(messages_path.resolve()),
            conversations_file=str(conversations_path.resolve()),
            manifest_file=str(manifest_path.resolve()),
            message_count=message_count,
            conversation_count=len(chat_counts),
            decode_errors=decode_errors,
            since=since,
            until=until,
        )
    finally:
        con.close()
