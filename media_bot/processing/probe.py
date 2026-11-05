"""Media probing: what a downloaded file actually contains.

Used to confirm the delivered resolution instead of repeating what the site claimed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("grabber.probe")


@dataclass(slots=True)
class MediaInfo:
    path: Path
    size_bytes: int
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    vcodec: str | None = None
    acodec: str | None = None
    bitrate_kbps: int | None = None
    container: str | None = None
    nb_streams: int | None = None
    error: str | None = None

    @property
    def label(self) -> str:
        parts = []
        if self.height:
            parts.append(f"{self.width}x{self.height}")
        if self.vcodec:
            parts.append(self.vcodec)
        if self.acodec:
            parts.append(self.acodec)
        elif self.duration is None and not self.height:
            parts.append("بلا فيديو")
        if self.duration:
            parts.append(f"{int(self.duration)}s")
        if self.bitrate_kbps:
            parts.append(f"{self.bitrate_kbps} kbps")
        return " · ".join(parts) or (self.container or "ملف")


class FFprobeUnavailable(RuntimeError):
    pass


async def probe(path: Path, *, ffprobe_path: str = "ffprobe") -> MediaInfo:
    """Read real stream metadata with ffprobe; degrade to size-only if absent."""
    path = Path(path)
    size = path.stat().st_size if path.exists() else 0
    binary = shutil.which(ffprobe_path)
    if not binary:
        return MediaInfo(path=path, size_bytes=size, container=path.suffix.lstrip("."),
                         error="ffprobe غير متاح")

    cmd = [
        binary, "-v", "error", "-hide_banner", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    except (asyncio.TimeoutError, OSError) as exc:
        return MediaInfo(path=path, size_bytes=size, error=f"ffprobe failed: {exc}")

    if process.returncode != 0:
        return MediaInfo(path=path, size_bytes=size,
                         error=stderr.decode("utf-8", "replace")[:200])
    try:
        data = json.loads(stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError as exc:
        return MediaInfo(path=path, size_bytes=size, error=f"bad ffprobe json: {exc}")

    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})

    def _int(source: dict, key: str) -> int | None:
        value = source.get(key)
        try:
            return int(value) if value not in (None, "N/A") else None
        except (TypeError, ValueError):
            return None

    def _float(source: dict, key: str) -> float | None:
        value = source.get(key) or (fmt.get(key) if not source else None)
        try:
            return float(value) if value not in (None, "N/A") else None
        except (TypeError, ValueError):
            return None

    duration = _float(fmt, "duration") or _float(video, "duration") or _float(audio, "duration")
    bitrate = _int(fmt, "bit_rate")
    return MediaInfo(
        path=path,
        size_bytes=size,
        duration=duration,
        width=_int(video, "width"),
        height=_int(video, "height"),
        vcodec=video.get("codec_name"),
        acodec=audio.get("codec_name"),
        bitrate_kbps=int(bitrate / 1000) if bitrate else None,
        container=fmt.get("format_name") or path.suffix.lstrip("."),
        nb_streams=_int(fmt, "nb_streams") or len(streams),
    )


__all__ = ["probe", "MediaInfo", "FFprobeUnavailable"]
