from __future__ import annotations

import json
import hashlib
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .image_crypto import decrypt_v2_bytes, image_extension, load_image_keys
from .voice_decode import VoiceDecoder
from .image_decode import decode_wxgf
from .downloaded_media import retained_download


@dataclass
class MediaArtifact:
    status: str
    kind: str
    path: str | None
    md5: str | None
    bytes: int | None
    encrypted: bool
    note: str | None = None
    original_status: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def latest_decrypted_media_databases(project_root: Path, account: str) -> list[Path]:
    state_dir = project_root / 'data' / 'state' / account / 'message'
    out: list[Path] = []
    if not state_dir.is_dir():
        return out
    for state_file in sorted(state_dir.glob('media_*.json')):
        try:
            payload = json.loads(state_file.read_text(encoding='utf-8'))
            path = Path(str(payload.get('decrypted') or ''))
        except (OSError, ValueError, TypeError):
            continue
        if path.is_file():
            out.append(path)
    return out


class MediaResolver:
    def __init__(
        self,
        wechat_root: Path,
        account: str,
        media_db_paths: list[Path] | None = None,
        image_keys_path: Path | None = None,
        voice_decoder_path: Path | None = None,
        max_copy_bytes: int | None = None,
        image_resources=None,
    ):
        self.account = account
        self.account_root = (wechat_root / account).resolve()
        self.media_db_paths = [path.resolve() for path in (media_db_paths or [])]
        self.image_keys = load_image_keys(image_keys_path) if image_keys_path else None
        self.voice_decoder = VoiceDecoder(voice_decoder_path)
        self.max_copy_bytes = max_copy_bytes
        self.force_copy = False
        self.image_resources = image_resources

    @staticmethod
    def _month(timestamp: int) -> str:
        return datetime.fromtimestamp(int(timestamp)).strftime('%Y-%m')

    @staticmethod
    def _safe_uid(uid: str) -> str:
        return ''.join(ch for ch in uid if ch.isalnum() or ch in '-_')[:80]

    def _copy(
        self,
        source: Path,
        destination: Path,
        *,
        kind: str,
        md5: str | None,
        status: str,
        encrypted: bool,
        note: str | None = None,
    ) -> MediaArtifact:
        if not source.is_file():
            return MediaArtifact('missing', kind, None, md5, None, encrypted, note)
        size = source.stat().st_size
        if self.max_copy_bytes is not None and size > self.max_copy_bytes:
            return MediaArtifact(
                'too_large', kind, None, md5, size, encrypted,
                f'source exceeds {self.max_copy_bytes} byte copy limit',
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not self.force_copy and destination.is_file() and destination.stat().st_size == size:
            return MediaArtifact(
                status, kind, str(destination.resolve()), md5, size, encrypted, note
            )
        if shutil.disk_usage(destination.parent).free < size + 2 * 1024 * 1024 * 1024:
            return MediaArtifact('insufficient_space', kind, None, md5, size, encrypted)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=destination.name + '.',
                suffix='.tmp',
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
            shutil.copyfile(source, temp_path)
            temp_path.replace(destination)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return MediaArtifact(
            status, kind, str(destination.resolve()), md5, size, encrypted, note
        )

    def export_image(
        self,
        chat_table: str,
        timestamp: int,
        md5: str | None,
        uid: str,
        destination_root: Path,
    ) -> MediaArtifact:
        retained = retained_download(destination_root, self.account, uid, 'image')
        if retained is not None:
            path, size, verification = retained
            return MediaArtifact('ready', 'image', str(path.resolve()), md5, size, False,
                                 'Independent CDN download; ' + verification)
        if not md5:
            return MediaArtifact('missing_id', 'image', None, None, None, True)
        chat_hash = chat_table[4:] if chat_table.startswith('Msg_') else chat_table
        folder = (
            self.account_root / 'msg' / 'attach' / chat_hash / self._month(timestamp) / 'Img'
        )
        # Resolve the exact resource content only after this message's own
        # originals fail, and before accepting its thumbnail. Resource file
        # names are per-message identifiers, not content hashes.
        def candidates():
            if getattr(self, 'original_allowed', None) is not None and not self.original_allowed(uid):
                yield folder / f'{md5}_t.dat', 'encrypted_thumbnail', None
                return
            yield folder / f'{md5}_h.dat', 'encrypted_original', None
            yield folder / f'{md5}.dat', 'encrypted_original', None
            if self.image_resources is not None:
                digest, paths = self.image_resources.find_image(uid, force=self.force_copy)
                for path in paths:
                    if path.resolve().is_relative_to(self.account_root):
                        yield path, 'encrypted_original', digest
            yield folder / f'{md5}_t.dat', 'encrypted_thumbnail', None

        fallback: MediaArtifact | None = None
        for source, status, expected_digest in candidates():
            if source.is_file():
                try:
                    raw = source.read_bytes()
                except OSError:
                    if fallback is None:
                        fallback = MediaArtifact('unreadable', 'image', None, md5, None, True)
                    continue
                if image_extension(raw) != 'bin' or self.image_keys is not None or hasattr(self, 'decrypt_image'):
                    try:
                        plain = (self.decrypt_image(raw, self.image_keys) if hasattr(self, 'decrypt_image')
                                 else raw if image_extension(raw) != 'bin' else decrypt_v2_bytes(raw, *self.image_keys))
                        if expected_digest and hashlib.md5(plain).hexdigest() != expected_digest:
                            continue
                        ext = image_extension(plain)
                    except (OSError, ValueError):
                        ext = 'bin'
                        plain = b''
                    if plain and ext != 'bin':
                        suffix = '_thumb' if source.name.endswith('_t.dat') else ''
                        destination = destination_root / 'images' / f'{md5}{suffix}.{ext}'
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if not destination.is_file() or destination.read_bytes() != plain:
                            temp_path: Path | None = None
                            try:
                                with tempfile.NamedTemporaryFile(
                                    dir=destination.parent,
                                    prefix=destination.name + '.',
                                    suffix='.tmp',
                                    delete=False,
                                ) as handle:
                                    handle.write(plain)
                                    temp_path = Path(handle.name)
                                temp_path.replace(destination)
                            finally:
                                if temp_path is not None:
                                    temp_path.unlink(missing_ok=True)
                        image_status = (
                            'wxgf_container' if ext == 'wxgf'
                            else 'ready_thumbnail' if suffix else 'ready'
                        )
                        if ext == 'wxgf':
                            converted, conversion_status = getattr(self, 'image_decoder', decode_wxgf)(plain, destination.with_suffix('.png'))
                            if converted is not None:
                                return MediaArtifact('ready_thumbnail' if suffix else 'ready', 'image',
                                    str(converted.resolve()), md5, converted.stat().st_size, False,
                                    'Decoded locally from WXGF without resizing.')
                            image_status = conversion_status
                        artifact = MediaArtifact(
                            image_status, 'image', str(destination.resolve()), md5,
                            len(plain), False,
                            'Only a local thumbnail is available.' if image_status == 'ready_thumbnail'
                            else 'Local WXGF conversion unavailable or failed.' if ext == 'wxgf'
                            else None,
                        )
                        if image_status == 'ready_thumbnail':
                            artifact.original_status = fallback.status if fallback is not None else 'missing'
                        if image_status in {'ready', 'ready_thumbnail'}:
                            return artifact
                        if fallback is None:
                            fallback = artifact
                        continue
                if expected_digest:
                    continue
                name = source.name
                artifact = self._copy(
                    source, destination_root / 'images-encrypted' / name,
                    kind='image', md5=md5, status=status, encrypted=True,
                    note='WeChat 4.x image container copied read-only; cached image key is unavailable or did not validate.',
                )
                if fallback is None:
                    fallback = artifact
        return fallback or MediaArtifact('missing', 'image', None, md5, None, True)

    def export_video(
        self,
        timestamp: int,
        md5: str | None,
        uid: str,
        destination_root: Path,
    ) -> MediaArtifact:
        retained = retained_download(destination_root, self.account, uid, 'video')
        if retained is not None:
            path, size, verification = retained
            return MediaArtifact('ready', 'video', str(path.resolve()), md5, size, False,
                                 'Independent CDN download; ' + verification)
        if not md5:
            return MediaArtifact('missing_id', 'video', None, None, None, False)
        folder = self.account_root / 'msg' / 'video' / self._month(timestamp)
        def candidates():
            yield folder / f'{md5}.mp4', 'ready', None
            allowed = getattr(self, 'original_allowed', None)
            if self.image_resources is not None and (allowed is None or allowed(uid)):
                digest, paths = self.image_resources.find_video(uid, force=self.force_copy)
                for path in paths:
                    if path.resolve().is_relative_to(self.account_root):
                        yield path, 'ready', digest
            yield folder / f'{md5}.jpg', 'thumbnail_only', None
            yield folder / f'{md5}_thumb.jpg', 'thumbnail_only', None

        for source, status, expected_digest in candidates():
            if status == 'ready' and getattr(self, 'original_allowed', None) is not None and not self.original_allowed(uid):
                continue
            if source.is_file():
                if expected_digest:
                    try:
                        with source.open('rb') as handle:
                            if hashlib.file_digest(handle, 'md5').hexdigest() != expected_digest:
                                continue
                    except OSError:
                        continue
                name = source.name
                return self._copy(
                    source, destination_root / 'video' / name,
                    kind='video', md5=md5, status=status, encrypted=False,
                    note=None if status == 'ready' else 'Only a local video thumbnail is available.',
                )
        return MediaArtifact('missing', 'video', None, md5, None, False)

    def export_voice(
        self,
        chat_username: str,
        server_id: str | int | None,
        uid: str,
        destination_root: Path,
    ) -> MediaArtifact:
        if not server_id:
            return MediaArtifact('missing_id', 'voice', None, None, None, False)
        for database in self.media_db_paths:
            con = sqlite3.connect(f'file:{database.as_posix()}?mode=ro', uri=True)
            try:
                chat = con.execute(
                    'SELECT rowid FROM Name2Id WHERE user_name=? LIMIT 1',
                    (chat_username,),
                ).fetchone()
                if not chat:
                    continue
                row = con.execute(
                    '''
                    SELECT voice_data FROM VoiceInfo
                    WHERE chat_name_id=? AND CAST(svr_id AS TEXT)=?
                    ORDER BY create_time DESC LIMIT 1
                    ''',
                    (chat[0], str(server_id)),
                ).fetchone()
                if not row or not row[0]:
                    continue
                data = bytes(row[0])
                if self.max_copy_bytes is not None and len(data) > self.max_copy_bytes:
                    return MediaArtifact('too_large', 'voice', None, None, len(data), False)
                destination = destination_root / 'voice' / f'{self._safe_uid(str(server_id))}.silk'
                destination.parent.mkdir(parents=True, exist_ok=True)
                if shutil.disk_usage(destination.parent).free < len(data) + 2 * 1024 * 1024 * 1024:
                    return MediaArtifact('insufficient_space', 'voice', None, None, len(data), False)
                temp_path: Path | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        dir=destination.parent,
                        prefix=destination.name + '.',
                        suffix='.tmp',
                        delete=False,
                    ) as handle:
                        handle.write(data)
                        temp_path = Path(handle.name)
                    temp_path.replace(destination)
                finally:
                    if temp_path is not None:
                        temp_path.unlink(missing_ok=True)

                if self.voice_decoder.available:
                    wav = destination_root / 'voice-wav' / f'{self._safe_uid(str(server_id))}.wav'
                    decoded = self.voice_decoder.decode(destination, wav)
                    if decoded.ok:
                        return MediaArtifact(
                            'ready_audio', 'voice', decoded.wav_path, None,
                            Path(decoded.wav_path).stat().st_size if decoded.wav_path else None,
                            False,
                            f'Original SILK retained at {destination.resolve()}',
                        )

                return MediaArtifact(
                    'encoded_voice', 'voice', str(destination.resolve()), None, len(data), False,
                    'WeChat voice payload exported from decrypted media database; external SILK decoder not configured or conversion failed.',
                )
            finally:
                con.close()
        return MediaArtifact('missing', 'voice', None, None, None, False)

    def export_file(
        self,
        timestamp: int,
        filename: str | None,
        uid: str,
        destination_root: Path,
    ) -> MediaArtifact:
        if not filename:
            return MediaArtifact('missing_name', 'file', None, None, None, False)
        safe_name = Path(filename).name
        if safe_name != filename or not safe_name:
            return MediaArtifact('unsafe_name', 'file', None, None, None, False)
        source = self.account_root / 'msg' / 'file' / self._month(timestamp) / safe_name
        destination = destination_root / 'files' / f'{self._safe_uid(uid)}_{safe_name}'
        return self._copy(
            source, destination, kind='file', md5=None, status='ready', encrypted=False
        )
