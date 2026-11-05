"""yt-dlp engine: analysis and download for audio/video on every site the extractor set covers."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

import yt_dlp

from ..errors import (
    AuthRequiredError,
    DRMMediaError,
    EngineError,
    NoMediaFoundError,
    UnsupportedPlatformError,
    classify,
)
from ..config import USER_AGENT
from ..quality import build_variants
from ..security import looks_like_media_url
from .base import Bundle, DownloadResult, Extractor, MediaItem, MediaKind, ProgressHook, Variant, Want

log = logging.getLogger("grabber.ytdlp")

MAX_PLAYLIST_ITEMS = 25
# Phrases yt-dlp actually emits when content needs the user's own credentials. Bare
# substrings like "auth" or "age" were dropped: they also match "oauth" and "message".
_LOGIN_HINTS = ("logged-in", "logged in", "login required", "log in", "cookies", "sign in",
                "private video", "members-only", "authentication required", "http error 401",
                "http error 403", "age restriction", "age-restricted", "verify your age",
                "account needs")


def _scrub(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


class YtdlpExtractor(Extractor):
    engine_id = "ytdlp"
    provides = frozenset({MediaKind.VIDEO, MediaKind.AUDIO, MediaKind.LIVE, MediaKind.IMAGE})
    priority = 10

    def supports(self, host: str, url: str, *, want: Want = Want.AUTO) -> bool:
        # A bare file link is better served by the direct engine (no page analysis).
        if looks_like_media_url(url) and want is not Want.VIDEO:
            return False
        return want in (Want.AUTO, Want.VIDEO, Want.AUDIO)

    # ---------------- options ----------------

    def _base_opts(self, cookies_path: Path | None = None) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "ignoreerrors": False,
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": self.settings.read_timeout_seconds,
            "noplaylist": True,
            "windowsfilenames": True,
            "concurrent_fragment_downloads": 4,
            "http_headers": {
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.8,ar;q=0.6",
            },
        }
        if self.settings.resume:
            opts["continuedl"] = True
        if cookies_path:
            opts["cookiefile"] = str(cookies_path)
        if self.settings.use_aria2 and shutil_which(self.settings.aria2c_path):
            opts["external_downloader"] = {"http": self.settings.aria2c_path, "https": self.settings.aria2c_path}
            opts["external_downloader_args"] = {
                "aria2c": ["--max-connection-per-server=%d" % self.settings.aria2_connections,
                           "--min-split-size=1M", "--split=%d" % self.settings.aria2_connections,
                           "--max-concurrent-downloads=2", "--file-allocation=none"],
            }
            log.info("aria2c detected at %s", shutil_which(self.settings.aria2c_path))
        return opts

    # ---------------- extraction ----------------

    async def extract(self, url: str, *, host: str = "", cookies_path: Path | None = None,
                      want: Want = Want.AUTO) -> Bundle:
        info = await asyncio.to_thread(self._probe, url, cookies_path)
        return self._bundle_from_info(url, info)

    def _probe(self, url: str, cookies_path: Path | None) -> dict[str, Any]:
        opts = self._base_opts(cookies_path)
        opts["skip_download"] = True
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False, process=False) or {}
        except yt_dlp.utils.DownloadError as exc:
            raise self._to_error(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - unexpected engine internals
            raise EngineError(detail=f"{type(exc).__name__}: {exc}") from exc

    def _to_error(self, raw: str) -> Exception:
        lowered = raw.lower()
        if "unsupported url" in lowered or "no working extractor" in lowered:
            return UnsupportedPlatformError(detail=raw)
        if any(hint in lowered for hint in _LOGIN_HINTS):
            return AuthRequiredError(detail=raw)
        return classify(raw)

    def _bundle_from_info(self, url: str, info: dict[str, Any]) -> Bundle:
        from ..platform_detector import detect

        platform = detect(url)
        if not info:
            raise NoMediaFoundError()

        if info.get("_type") == "playlist" or info.get("entries"):
            return self._playlist_bundle(url, info, platform.label)

        formats = [fmt for fmt in (info.get("formats") or []) if isinstance(fmt, dict)]
        has_drm = bool(info.get("has_drm")) or any(fmt.get("has_drm") for fmt in formats)
        if has_drm:
            raise DRMMediaError(detail=f"{platform.label} reports DRM-protected streams")

        duration = info.get("duration") or 0
        variants = build_variants(formats, duration=duration, preferred_ext="mp4")
        if not variants:
            raise NoMediaFoundError("لا توجد صيغ قابلة للتنزيل خلف هذا الرابط.")

        kind = MediaKind.VIDEO
        if all(v.kind is MediaKind.AUDIO for v in variants):
            kind = MediaKind.AUDIO
        live = bool(info.get("is_live")) or bool(info.get("live_status") in {"is_live", "was_live", "upcoming"})
        if live:
            kind = MediaKind.LIVE

        notes: list[str] = []
        if live:
            notes.append("بث مباشر: الجودة المتاحة تتغير حسب اللحظة، والتسجيل قد يظهر لاحقاً")
        if not any(v.filesize for v in variants):
            notes.append("الأحجام تقديرية: المنصة لا تُعلن حجم الملف مسبقاً")

        bundle = self.new_bundle(url, platform=platform.label, kind=kind)
        bundle.title = _scrub(info.get("title") or "") or None
        bundle.thumbnail = info.get("thumbnail")
        bundle.duration = int(duration) if duration else None
        bundle.uploader = _scrub(info.get("uploader") or info.get("channel") or "") or None
        bundle.webpage_url = info.get("webpage_url") or url
        bundle.variants = variants
        bundle.count = 1
        bundle.has_drm = has_drm
        bundle.live = live
        bundle.total_bytes = self._max_total(variants)
        bundle.notes = notes
        return bundle

    @staticmethod
    def _max_total(variants: list[Variant]) -> int | None:
        sizes = [v.filesize for v in variants if v.filesize]
        return max(sizes) if sizes else None

    def _playlist_bundle(self, url: str, info: dict[str, Any], label: str) -> Bundle:
        entries = [entry for entry in (info.get("entries") or []) if isinstance(entry, dict)]
        shown = entries[:MAX_PLAYLIST_ITEMS]
        if not shown:
            raise NoMediaFoundError("قائمة التشغيل لا تحتوي عناصر يمكن قراءتها.")

        bundle = self.new_bundle(url, platform=label, kind=MediaKind.ALBUM)
        bundle.title = _scrub(info.get("title") or "") or None
        bundle.thumbnail = shown[0].get("thumbnail")
        bundle.webpage_url = info.get("webpage_url") or url
        bundle.count = len(entries)
        bundle.uploader = _scrub(info.get("uploader") or info.get("channel") or "") or None
        bundle.duration = sum(int(entry.get("duration") or 0) for entry in entries) or None
        bundle.items = [
            MediaItem(
                url=entry.get("url") or entry.get("webpage_url") or entry.get("id") or "",
                kind=MediaKind.VIDEO,
                name=_scrub(entry.get("title")) or f"العنصر {index + 1}",
            )
            for index, entry in enumerate(shown)
        ]
        bundle.items = [item for item in bundle.items if item.url]
        if not bundle.items:
            raise NoMediaFoundError("عناصر قائمة التشغيل بلا روابط قابلة للتنزيل.")

        notes = [f"قائمة تشغيل ({len(entries)} عنصراً) — اختر عنصراً ثم الجودة"]
        if len(entries) > MAX_PLAYLIST_ITEMS:
            notes.append(f"يعرض هنا أول {MAX_PLAYLIST_ITEMS} عنصراً فقط")
        bundle.notes = notes
        return bundle

    # ---------------- download ----------------

    async def download(self, bundle: Bundle, variant: Variant | None, dest_dir: Path, *,
                       progress: ProgressHook | None = None,
                       cookies_path: Path | None = None) -> DownloadResult:
        if bundle.has_drm:
            raise DRMMediaError()
        if bundle.live and variant is None:
            raise EngineError("البث المباشر يحتاج تحديد جودة صريحة.")
        chosen = variant or (bundle.variants[0] if bundle.variants else None)
        if chosen is None:
            raise NoMediaFoundError()

        target = str(dest_dir / "%(title).120B [%(id)s].%(ext)s")
        opts = self._base_opts(cookies_path)
        opts.update({
            "skip_download": False,
            "outtmpl": {"default": target},
            "paths": {"temp": str(dest_dir / "_part")},
            "format": chosen.format_spec,
            "noplaylist": True,
        })
        if chosen.merge:
            opts["merge_output_format"] = chosen.ext if chosen.ext in {"mp4", "mkv", "webm", "mov"} else "mkv"
        if chosen.kind is MediaKind.AUDIO and chosen.ext == "mp3":
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "0",
            }]

        loop = asyncio.get_running_loop()
        hook = _ProgressBridge(loop, progress, chosen.filesize) if progress else None
        if hook:
            opts["progress_hooks"] = [hook.on_progress]

        try:
            info = await asyncio.to_thread(self._run_download, bundle.url, opts)
        except yt_dlp.utils.DownloadError as exc:
            raise self._to_error(str(exc)) from exc
        finally:
            if hook:
                hook.finish()

        files = sorted(
            (path for path in dest_dir.rglob("*") if path.is_file() and not path.name.startswith("._")),
            key=lambda path: path.stat().st_size,
            reverse=True,
        )
        if not files:
            raise EngineError("انتهى المحرك بلا ملف على القرص.", detail=f"dest={dest_dir}")
        # yt-dlp reports exactly what it finished writing; trust that over a size guess,
        # because a merged file and its leftover .f137/.f140 fragments can look similar.
        # Dedupe on the resolved path: the reported path and the rglob path can differ in
        # separator or case, and double-counting would inflate the size and send twice.
        ordered: list[Path] = []
        seen: set[str] = set()
        for path in _reported_paths(info) + files:
            key = str(path.resolve()).casefold()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(path)
        files = ordered
        total = sum(path.stat().st_size for path in files)
        if progress:
            await progress(100.0, "اكتمل التنزيل")
        return DownloadResult(
            files=files,
            bytes_total=total,
            engine=self.engine_id,
            merged=chosen.merge,
        )

    def _run_download(self, url: str, opts: dict[str, Any]) -> dict[str, Any]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True) or {}

    def availability(self) -> dict[str, Any]:
        from yt_dlp.extractor import gen_extractor_classes

        classes = [cls for cls in gen_extractor_classes() if not cls.__name__.startswith("_")]
        return {"engine": self.engine_id, "extractors": len(classes),
                "aria2c": bool(shutil_which(self.settings.aria2c_path))}


class _ProgressBridge:
    """yt-dlp reports progress from its worker thread; this forwards to the loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop, hook: ProgressHook,
                 expected_bytes: int | None) -> None:
        self.loop = loop
        self.hook = hook
        self.expected = expected_bytes
        self.last = 0.0
        self._future = None

    def on_progress(self, datum: dict[str, Any]) -> None:
        status = datum.get("status")
        if status == "finished":
            self._emit(100.0, "اكتمل التنزيل")
            return
        if status != "downloading":
            return
        done = datum.get("downloaded_bytes") or 0
        total = datum.get("total_bytes") or datum.get("total_bytes_estimate") or self.expected
        percent = min(99.0, (done / total) * 100) if total else 0.0
        speed = datum.get("speed")
        text = None
        if speed:
            from ..extractors.base import human_size

            text = f"{human_size(done)} / {human_size(total)} · {human_size(speed)}/ث"
        elif percent:
            text = f"{percent:.0f}%"
        self._emit(percent, text)

    def _emit(self, percent: float, text: str | None) -> None:
        import time

        now = time.monotonic()
        if percent < 100 and now - self.last < 1.0:
            return
        self.last = now
        try:
            self._future = asyncio.run_coroutine_threadsafe(self.hook(percent, text), self.loop)
        except RuntimeError:  # pragma: no cover - loop already closed
            pass

    def finish(self) -> None:
        if self._future and not self._future.done():
            self._future.cancel()


def shutil_which(binary: str) -> str | None:
    import shutil

    return shutil.which(binary)


def _reported_paths(info: dict[str, Any]) -> list[Path]:
    """Paths yt-dlp says it finished writing, most useful first."""
    candidates: list[str] = []
    for download in (info.get("requested_downloads") or []):
        if isinstance(download, dict) and download.get("filepath"):
            candidates.append(download["filepath"])
    if info.get("filepath"):
        candidates.append(info["filepath"])
    seen: list[Path] = []
    for entry in candidates:
        path = Path(entry)
        if path.is_file() and path not in seen:
            seen.append(path)
    return seen


__all__ = ["YtdlpExtractor", "MAX_PLAYLIST_ITEMS"]
