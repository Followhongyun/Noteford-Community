from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VoiceDecodeResult:
    ok: bool
    wav_path: str | None
    reason: str | None


class VoiceDecoder:
    """Optional external SILK v3 decoder adapter.

    The core project does not download or bundle decoder binaries. When an
    explicitly configured decoder is present, it is invoked without a shell,
    writes PCM to a private temporary path, and ffmpeg converts that PCM to WAV.
    """

    def __init__(
        self,
        decoder_path: Path | None,
        *,
        ffmpeg_path: Path | None = None,
        sample_rate: int = 24000,
        timeout_seconds: int = 30,
    ):
        self.decoder_path = decoder_path.resolve() if decoder_path else None
        resolved_ffmpeg = str(ffmpeg_path) if ffmpeg_path else shutil.which('ffmpeg')
        self.ffmpeg_path = Path(resolved_ffmpeg).resolve() if resolved_ffmpeg else None
        self.sample_rate = int(sample_rate)
        self.timeout_seconds = int(timeout_seconds)

    @property
    def available(self) -> bool:
        return bool(
            self.decoder_path
            and self.decoder_path.is_file()
            and self.ffmpeg_path
            and self.ffmpeg_path.is_file()
            and 8000 <= self.sample_rate <= 48000
        )

    def decode(self, silk_path: Path, wav_path: Path) -> VoiceDecodeResult:
        if not self.available:
            return VoiceDecodeResult(False, None, 'decoder_unavailable')
        if not silk_path.is_file():
            return VoiceDecodeResult(False, None, 'silk_missing')

        wav_path.parent.mkdir(parents=True, exist_ok=True)
        pcm_path = wav_path.with_suffix('.pcm.tmp')
        wav_tmp = wav_path.with_suffix('.wav.tmp')
        for path in (pcm_path, wav_tmp):
            path.unlink(missing_ok=True)

        try:
            first = subprocess.run(
                [str(self.decoder_path), str(silk_path), str(pcm_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
            if first.returncode != 0 or not pcm_path.is_file() or pcm_path.stat().st_size == 0:
                return VoiceDecodeResult(False, None, 'silk_decoder_failed')

            second = subprocess.run(
                [
                    str(self.ffmpeg_path), '-y', '-v', 'error',
                    '-f', 's16le', '-ar', str(self.sample_rate), '-ac', '1',
                    '-i', str(pcm_path), str(wav_tmp),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout_seconds,
                check=False,
                shell=False,
            )
            if second.returncode != 0 or not wav_tmp.is_file() or wav_tmp.stat().st_size <= 44:
                return VoiceDecodeResult(False, None, 'ffmpeg_conversion_failed')
            wav_tmp.replace(wav_path)
            return VoiceDecodeResult(True, str(wav_path.resolve()), None)
        except (OSError, subprocess.SubprocessError):
            return VoiceDecodeResult(False, None, 'decoder_execution_failed')
        finally:
            pcm_path.unlink(missing_ok=True)
            wav_tmp.unlink(missing_ok=True)
