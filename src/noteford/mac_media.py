"""Read local Mac media without modifying or instrumenting official WeChat."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

from .assets import latest_decrypted_database
from .image_crypto import (V2_MAGIC, _valid_aes_key, find_recent_thumbnail_templates,
                           load_image_keys, save_image_keys)
from .libraries import account_username
from .mac_live_sync import managed_archive, _storage
from .media import MediaArtifact, MediaResolver, latest_decrypted_media_databases
from .media_resources import ImageResourceLookup, read_image_resources, read_video_resources
from .voice_decode import VoiceDecodeResult


def media_error(artifact: MediaArtifact) -> str | None:
    """Local absence does not establish CDN expiry or user deletion."""
    if artifact.status == 'ready':
        return None
    status = artifact.original_status or artifact.status
    if status.startswith('encrypted_'):
        return 'local_decryption_unavailable'
    if status == 'image_decoder_busy':
        return 'download_busy'
    if status.startswith(('wxgf_', 'unsupported_', 'image_decode', 'image_dimensions')):
        return 'unsupported_download_format'
    if status in {'insufficient_space', 'too_large'}:
        return status
    if status == 'unreadable':
        return 'local_media_unreadable'
    return 'local_original_unavailable'


def kvcomm_roots(home: Path | None = None) -> list[Path]:
    data = (home or Path.home()) / 'Library/Containers/com.tencent.xinWeChat/Data'
    return [data / 'Documents/app_data/net/kvcomm', data / 'Library/Caches/kvcomm']


def local_image_keys(account_root: Path, destination: Path,
                     cache_roots: list[Path] | None = None) -> tuple[bytes, int] | None:
    """Verify an account-derived key against real V2 ciphertext before use.

    The Mac derivation is documented by wx-cli-again's image extraction notes:
    https://github.com/jackwener/wx-cli-again (kvcomm UIN + wxid, MD5 prefix).
    Cache filenames are only candidates; no unverified key is persisted.
    """
    probes = []
    for sample in find_recent_thumbnail_templates(account_root, keep=3):
        try:
            with sample.open('rb') as handle:
                header = handle.read(31)
            if len(header) == 31 and header.startswith(V2_MAGIC):
                probes.append(header[15:31])
        except OSError:
            continue
    if not probes:
        return load_image_keys(destination)
    cached = load_image_keys(destination)
    if cached and all(_valid_aes_key(cached[0], p) for p in probes):
        return cached
    account = account_root.name
    username = account_username(account)
    suffix = account.rsplit('_', 1)[-1]
    if not re.fullmatch(r'[0-9a-fA-F]{4}', suffix):
        return None
    for root in cache_roots if cache_roots is not None else kvcomm_roots():
        if not root.is_dir() or root.is_symlink():
            continue
        for candidate in root.glob('key_*.statistic'):
            match = re.fullmatch(r'key_(\d{1,10})_.*\.statistic', candidate.name)
            if not match or candidate.is_symlink():
                continue
            uin = match[1]
            if hashlib.md5(uin.encode()).hexdigest()[:4] != suffix.lower():
                continue
            key = hashlib.md5((uin + username).encode()).hexdigest()[:16].encode()
            if not all(_valid_aes_key(key, p) for p in probes):
                continue
            xor = int(uin) & 255
            save_image_keys(destination, key, xor)
            return key, xor
    return None


class MacImageResources(ImageResourceLookup):
    def __init__(self, account_root: Path, project: Path, index: Path):
        super().__init__(account_root, index, project/'data/runtime', project/'unused')
        self.project = project

    def _refresh(self) -> None:
        self._checked = time.monotonic()
        try:
            database = latest_decrypted_database(self.project, self.account_root.name, 'hardlink', 'hardlink')
            if database is None:
                raise FileNotFoundError()
            fingerprint = (str(database), database.stat().st_size, database.stat().st_mtime_ns)
            if fingerprint == self._fingerprint:
                return
            self._resources = read_image_resources(database, self.account_root)
            self._video_resources = read_video_resources(database, self.account_root)
            self._fingerprint = fingerprint
        except (OSError, ValueError, sqlite3.Error):
            self._resources, self._video_resources, self._fingerprint = {}, {}, None


class MacVoiceDecoder:
    """Bundled SILK decoder; PCM is wrapped as WAV locally, without ffmpeg."""
    def __init__(self):
        self.binary = (Path(sys._MEIPASS)/'mac-native/noteford-silk' if getattr(sys, 'frozen', False)
                       else Path(__file__).resolve().parents[2]/'build/mac-native/noteford-silk')

    @property
    def available(self) -> bool:
        return self.binary.is_file()

    def decode(self, silk_path: Path, wav_path: Path) -> VoiceDecodeResult:
        if not self.available:
            return VoiceDecodeResult(False, None, 'decoder_unavailable')
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix='silk-', dir=wav_path.parent) as work:
                pcm = Path(work)/'audio.pcm'
                result = subprocess.run([str(self.binary), str(silk_path), str(pcm)],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=30, check=False)
                if result.returncode != 0:
                    return VoiceDecodeResult(False, None, 'silk_decoder_failed')
                if not pcm.stat().st_size or pcm.stat().st_size > 64 * 1024 * 1024:
                    return VoiceDecodeResult(False, None, 'invalid_decoded_audio')
                temporary = Path(work)/'audio.wav'
                with wave.open(str(temporary), 'wb') as target:
                    target.setnchannels(1)
                    target.setsampwidth(2)
                    target.setframerate(24000)
                    target.writeframes(pcm.read_bytes())
                temporary.chmod(0o600)
                temporary.replace(wav_path)
            return VoiceDecodeResult(True, str(wav_path.resolve()), None)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return VoiceDecodeResult(False, None, 'silk_decoder_failed')


def media_resolver(archive: Path, account: str, project: Path, index: Path,
                   keys_path: Path) -> MediaResolver:
    # Imported archives must never silently read a live account's source data.
    root = archive
    if archive.resolve() == managed_archive().resolve():
        try:
            root = _storage(account).parent.parent
        except OSError:
            pass  # Retained Noteford media remains readable while offline.
    resolver = MediaResolver(root, account,
        media_db_paths=latest_decrypted_media_databases(project, account),
        image_keys_path=keys_path,
        image_resources=MacImageResources(root/account, project, index))
    resolver.voice_decoder = MacVoiceDecoder()
    from .mac_image_decode import decode_wxgf, decrypt_dat
    resolver.image_decoder = decode_wxgf
    resolver.decrypt_image = decrypt_dat
    if root != archive:
        resolver.image_keys = local_image_keys(root/account, keys_path)
    return resolver
