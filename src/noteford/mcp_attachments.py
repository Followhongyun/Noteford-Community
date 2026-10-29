"""Read existing message-bound cache files; never resolve or download WeChat sources."""
from __future__ import annotations
import base64
import hashlib
import mimetypes
import re
from pathlib import Path


def read_attachment(reader, library_id, message_uid, offset=0, limit=262144, expected_sha256=None):
    from mcp.server.mcpserver.exceptions import ToolError
    from .downloaded_media import retained_download
    if offset and not expected_sha256:
        raise ToolError('attachment_hash_required_for_resume')
    parent = message_uid.split(':record:', 1)[0]
    grant = reader.grant(library_id)
    repo = reader.repository(library_id)
    found = repo.message_by_uid(parent)
    if not found or found['conversation']['username'] not in grant['conversations']:
        raise ToolError('mcp_scope_denied_or_message_missing')
    context = reader._registry().context(library_id)
    root = context.root / 'data/media-cache'
    item = found['message']
    kind = item['type']['kind']
    candidates = []
    from .mac_attachments import cached_file, entries, message_xml
    config = {'project_root': str(context.root), 'account': context.account, 'index_path': str(context.index_path)}
    if message_uid != parent:
        nested = next((entry for entry in entries(message_xml(config, parent), parent) if entry['uid'] == message_uid), None)
        if nested is None:
            raise ToolError('attachment_message_missing')
        kind = nested['kind']
    cached = cached_file(config, message_uid)
    if cached is not None and kind in ('file', 'voice'):
        candidates.append((cached, kind, 'cached'))
    # Nested images are only readable when a download is bound to that exact UID.
    for media_kind in ('image', 'video'):
        cached = retained_download(root, context.account, message_uid, media_kind)
        if cached:
            candidates.append((cached[0], media_kind, cached[2]))
    if message_uid == parent:
        with repo._connect() as con:
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='media_exports'").fetchone():
                row = con.execute('SELECT e.path,e.kind,e.status FROM media_exports e JOIN messages m ON m.uid=e.uid AND m.row_hash=e.row_hash WHERE e.uid=? AND m.account=?', (parent, context.account)).fetchone()
                if row and row[0] and row[2] in ('ready', 'ready_audio', 'encoded_voice', 'thumbnail_only'):
                    candidates.append((Path(row[0]), row[1], row[2]))
        digest = (item.get('media') or {}).get('md5')
        if isinstance(digest, str) and re.fullmatch('[a-fA-F0-9]{32}', digest):
            if kind == 'image':
                for suffix, quality in (('', 'original'), ('_thumb', 'thumbnail')):
                    for ext in ('jpg', 'png', 'gif', 'webp'):
                        candidates.append((root/'images'/f'{digest}{suffix}.{ext}', kind, quality))
            if kind == 'video':
                candidates.append((root/'video'/f'{digest}.mp4', kind, 'cached'))
        if kind == 'voice':
            server_id = str((item.get('source') or {}).get('server_id') or '')
            if re.fullmatch(r'[0-9]{1,80}', server_id):
                candidates.append((root/'voice-wav'/f'{server_id}.wav', kind, 'ready_audio'))
                candidates.append((root/'voice'/f'{server_id}.silk', kind, 'encoded_voice'))
        if kind == 'file':
            name = item.get('title')
            # Use the same deterministic message-bound cache name as MediaResolver.
            if isinstance(name, str) and name and Path(name).name == name and name not in ('.', '..'):
                uid = ''.join(c for c in parent if c.isalnum() or c in '-_')[:80]
                # Legacy names are truncated; only use one if it cannot alias another message.
                with repo._connect() as con:
                    aliases = sum(''.join(c for c in row[0] if c.isalnum() or c in '-_')[:80] == uid for row in con.execute('SELECT uid FROM messages WHERE account=?', (context.account,)))
                if aliases != 1:
                    raise ToolError('attachment_cache_identity_ambiguous')
                candidates.append((root/'files'/f'{uid}_{name}', kind, 'cached'))
    for path, media_kind, quality in candidates:
        resolved = path.resolve()
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
            continue
        try:
            with resolved.open('rb') as handle:
                digest = hashlib.file_digest(handle, 'sha256').hexdigest()
                handle.seek(0, 2)
                size = handle.tell()
                if expected_sha256 and digest != expected_sha256:
                    raise ToolError('attachment_changed_restart')
                if offset > size:
                    raise ToolError('attachment_offset_out_of_range')
                handle.seek(offset)
                data = handle.read(limit)
                handle.seek(0)
                if hashlib.file_digest(handle, 'sha256').hexdigest() != digest:
                    raise ToolError('attachment_changed_restart')
        except OSError:
            continue
        reader.grant(library_id, found['conversation']['username'])
        next_offset = offset + len(data)
        return {'library_id': library_id, 'message_uid': message_uid, 'kind': media_kind,
                'filename': resolved.name, 'mime_type': mimetypes.guess_type(resolved.name)[0] or 'application/octet-stream',
                'quality': quality, 'size': size, 'sha256': digest, 'offset': offset,
                'encoding': 'base64', 'data': base64.b64encode(data).decode(),
                'next_offset': next_offset if next_offset < size else None,
                'has_more': next_offset < size}
    raise ToolError('attachment_not_cached: 请先在 Noteford 取得附件；此工具不会下载或解密来源。')
