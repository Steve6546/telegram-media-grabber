"""Splitting oversized media so a plain Bot API bot can still deliver it.

When there is no Local Bot API Server and no MTProto session, a 300 MB download cannot
be sent as one file. Cutting it into ``-c copy`` segments (no re-encode, so no quality
loss) and sending the parts as documents is the remaining honest path, and the user is
told they are getting parts.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .errors import EngineError
from .extractors.base import human_size
from .processing.ffmpeg import FFmpeg

log = logging.getLogger("grabber.bigfiles")

STREAMABLE_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".ts", ".avi", ".m4v"}
AUDIO_SUFFIXES = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"}


def can_split(path: Path) -> bool:
    return path.suffix.lower() in STREAMABLE_SUFFIXES | AUDIO_SUFFIXES


async def split(path: Path, *, ffmpeg: FFmpeg, chunk_mb: int = 45,
                out_dir: Path | None = None) -> list[Path]:
    """Cut ``path`` into <chunk_mb parts. Returns the segment files in order."""
    if not ffmpeg.available:
        raise EngineError("تقسيم الملفات الكبيرة يحتاج FFmpeg، وهو غير متوفر هنا.")
    if not can_split(path):
        raise EngineError(f"لا يمكن تقسيم صيغة {path.suffix} بأمان.")

    out_dir = out_dir or path.parent
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb <= chunk_mb:
        return [path]

    stem = path.stem[:80]
    segments = sorted(out_dir.glob(f"{stem}.part-*{path.suffix}"))
    if segments:
        log.info("segments for %s already present (%d)", path.name, len(segments))
        return segments

    duration = None
    from .processing.probe import probe

    info = await probe(path, ffprobe_path=ffmpeg.ffprobe_path)
    duration = info.duration
    if not duration:
        raise EngineError("تعذر معرفة مدة الملف، فلا يمكن تقسيمه.")

    chunk_seconds = max(5, int(duration * min(0.9, chunk_mb / size_mb)))
    pattern = str(out_dir / f"{stem}.part-%03d{path.suffix}")
    if path.suffix.lower() in AUDIO_SUFFIXES:
        args = ["-i", str(path), "-c", "copy", "-f", "segment",
                "-segment_time", str(chunk_seconds), "-reset_timestamps", "1", pattern]
    else:
        args = ["-i", str(path), "-map", "0", "-c", "copy", "-f", "segment",
                "-segment_time", str(chunk_seconds), "-reset_timestamps", "1",
                "-force_key_frames", f"expr:gte(t,n_forced*{chunk_seconds})", pattern]
    await ffmpeg.run(args, timeout=1800)

    produced = sorted(out_dir.glob(f"{stem}.part-*{path.suffix}"))
    if not produced:
        raise EngineError("لم يُنتج FFmpeg أي مقطع.")
    biggest = max(p.stat().st_size for p in produced)
    if biggest > chunk_mb * 1024 * 1024:
        log.warning("a segment exceeded the target size (%s)", human_size(biggest))
    log.info("split %s into %d segments (target %d MB, largest %s)", path.name, len(produced),
             chunk_mb, human_size(biggest))
    return produced


__all__ = ["split", "can_split", "STREAMABLE_SUFFIXES"]
