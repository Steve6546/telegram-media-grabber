"""FFmpeg operations that are actually needed, and nothing that is not.

Deliberately small: yt-dlp performs audio/video merging itself (this project only
tells it to), so this module covers container remux, audio extraction and trimming.
A "convert" button that copies bytes and renames them — as the previous generation of
these bots shipped — is exactly what this file refuses to do.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from ..errors import EngineError, StorageError

log = logging.getLogger("grabber.ffmpeg")

#: target containers for a lossless remux, keyed by what the source can be copied into
_REMUXABLE = {"mp4", "mkv", "webm", "mov", "avi", "ts"}
_AUDIO_FORMATS = {"mp3", "m4a", "aac", "ogg", "opus", "wav", "flac"}


class FFmpeg:
    def __init__(self, *, ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "ffprobe") -> None:
        self.ffmpeg_path = ffmpeg_path
        self.ffprobe_path = ffprobe_path

    @property
    def binary(self) -> str | None:
        return shutil.which(self.ffmpeg_path)

    @property
    def available(self) -> bool:
        return self.binary is not None

    def version(self) -> str:
        binary = self.binary
        if not binary:
            return "غير مثبّت"
        try:
            out = subprocess_line([binary, "-version"])
            return out.split("\n")[0].strip() if out else "غير معروف"
        except Exception as exc:  # pragma: no cover
            return f"خطأ: {exc}"

    async def run(self, args: list[str], *, timeout: float = 900) -> tuple[int, str]:
        binary = self.binary
        if not binary:
            raise EngineError(
                "FFmpeg غير مثبّت على الخادم، فلا يمكن تنفيذ هذه المعالجة.",
                detail=" ".join(args),
            )
        cmd = [binary, "-hide_banner", "-nostdin", "-y", *args]
        log.debug("ffmpeg %s", " ".join(cmd[1:]))
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except OSError as exc:
            raise EngineError("تعذر تشغيل FFmpeg.", detail=str(exc)) from exc
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise EngineError("تجاوزت المعالجة مهلة FFmpeg.") from None
        code = process.returncode or 0
        text = stderr.decode("utf-8", "replace")
        if code != 0:
            raise EngineError("فشل FFmpeg في معالجة الملف.", detail=_tail(text))
        return code, text

    async def remux(self, src: Path, target_ext: str, *, out_dir: Path | None = None) -> Path:
        """Change container with ``-c copy``: no re-encode, no quality loss."""
        target_ext = target_ext.lstrip(".").lower()
        if target_ext not in _REMUXABLE:
            raise EngineError(f"الصيغة {target_ext} غير قابلة للتحويل بلا إعادة ترميز.")
        out_dir = out_dir or src.parent
        destination = out_dir / (src.stem[:100] + f".{target_ext}")
        args = ["-i", str(src), "-map", "0", "-c", "copy"]
        if target_ext in {"mp4", "mov"}:
            # faststart moves the moov atom up front so the file streams while downloading.
            args += ["-movflags", "+faststart"]
        args.append(str(destination))
        await self.run(args)
        if not destination.is_file() or destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            raise StorageError("أنتج FFmpeg ملفاً فارغاً.")
        return destination

    async def to_audio(self, src: Path, target_ext: str, *, bitrate: str | None = None,
                       out_dir: Path | None = None) -> Path:
        target_ext = target_ext.lstrip(".").lower()
        if target_ext not in _AUDIO_FORMATS:
            raise EngineError(f"صيغة صوت غير معروفة: {target_ext}.")
        out_dir = out_dir or src.parent
        destination = out_dir / (src.stem[:100] + f".{target_ext}")
        codec = {"mp3": "libmp3lame", "m4a": "aac", "aac": "aac", "ogg": "libvorbis",
                 "opus": "libopus", "flac": "flac", "wav": "pcm_s16le"}[target_ext]
        args = ["-i", str(src), "-vn", "-c:a", codec]
        if bitrate and target_ext != "flac":
            args += ["-b:a", bitrate]
        args.append(str(destination))
        await self.run(args)
        if not destination.is_file() or destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            raise StorageError("أنتج FFmpeg ملفاً صوتياً فارغاً.")
        return destination

    async def trim(self, src: Path, *, start: float, end: float, out_dir: Path | None = None) -> Path:
        if end <= start:
            raise EngineError("نهاية المقطع يجب أن تكون بعد بدايته.")
        if end - start < 0.5:
            raise EngineError("المقطع قصير جداً (أقل من نصف ثانية).")
        out_dir = out_dir or src.parent
        destination = out_dir / (src.stem[:90] + f"-clip-{int(start)}-{int(end)}.mp4")
        await self.run(["-ss", str(start), "-to", str(end), "-i", str(src),
                        "-c", "copy", "-avoid_negative_ts", "make_zero", str(destination)])
        return destination

    async def shrink(self, src: Path, *, max_height: int, crf: int = 30,
                     out_dir: Path | None = None) -> Path:
        """Re-encode down to fit Telegram's size ceiling when a smaller quality is absent."""
        out_dir = out_dir or src.parent
        destination = out_dir / (src.stem[:90] + f"-{max_height}p.mp4")
        await self.run([
            "-i", str(src), "-vf", f"scale=-2:'min({max_height},ih)'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(destination),
        ], timeout=1800)
        return destination


def subprocess_line(cmd: list[str]) -> str:
    import subprocess

    return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout or ""


def _tail(text: str, lines: int = 6) -> str:
    parts = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(parts[-lines:])


__all__ = ["FFmpeg", "_REMUXABLE", "_AUDIO_FORMATS"]
