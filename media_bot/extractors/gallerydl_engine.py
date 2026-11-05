"""gallery-dl engine: images, albums and galleries on the sites yt-dlp does not cover.

Driven as a subprocess on purpose: gallery-dl documents its Python API as unstable
between releases, while its CLI flags and ``-J`` JSON output are a stable contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from ..errors import (
    AuthRequiredError,
    EngineError,
    NoMediaFoundError,
    RateLimitedError,
    TimeoutError_,
    UnsupportedPlatformError,
)
from ..extractors.base import (
    Bundle,
    DownloadResult,
    Extractor,
    MediaItem,
    MediaKind,
    ProgressHook,
    Variant,
    Want,
    human_size,
)
from ..platform_detector import detect

log = logging.getLogger("grabber.gallerydl")

PREVIEW_LIMIT = 16
_TIMEOUT_FETCH = 150.0
_TIMEOUT_DOWNLOAD = 900.0


def _command(extra: list[str], url: str, cookies_path: Path | None) -> list[str]:
    cmd = [sys.executable, "-m", "gallery_dl", "--no-color", "-q"]
    if cookies_path:
        cmd += ["--cookies", str(cookies_path)]
    cmd += extra + [url]
    return cmd


class GalleryDlExtractor(Extractor):
    engine_id = "gallery-dl"
    provides = frozenset({MediaKind.IMAGE, MediaKind.ALBUM, MediaKind.VIDEO})
    priority = 20

    def supports(self, host: str, url: str, *, want: Want = Want.AUTO) -> bool:
        platform = detect(url)
        image_capable = any(kind in (MediaKind.IMAGE, MediaKind.ALBUM) for kind in platform.kinds)
        if want in (Want.IMAGE,):
            return True
        if want in (Want.VIDEO, Want.AUDIO) and not image_capable:
            return False
        return image_capable or platform.key == "generic"

    # ---------------- extraction ----------------

    async def extract(self, url: str, *, host: str = "", cookies_path: Path | None = None,
                      want: Want = Want.AUTO) -> Bundle:
        code, stdout, stderr = await self._run(
            _command(["-J", "--simulate", "--range", f"1-{PREVIEW_LIMIT}"], url, cookies_path),
            timeout=_TIMEOUT_FETCH,
        )
        if code != 0:
            raise self._error_for(code, stderr + stdout)

        records = _parse_ndjson(stdout)
        if not records:
            raise NoMediaFoundError("لم يُرجع محرك الصور أي ملف وسائط لهذا الرابط.")

        platform = detect(url)
        bundle = self.new_bundle(url, platform=platform.label,
                                 kind=MediaKind.ALBUM if len(records) > 1 else MediaKind.IMAGE)
        bundle.items = [_item_from_record(rec, index) for index, rec in enumerate(records)]
        bundle.items = [item for item in bundle.items if item.url]
        if not bundle.items:
            raise NoMediaFoundError("الرابط لا يحتوي ملفات صور قابلة للتنزيل.")
        bundle.count = max((rec.get("count") or 0) for rec in records) or len(bundle.items)
        first = records[0]
        bundle.title = _title(first, url)
        bundle.webpage_url = first.get("url") or url
        bundle.total_bytes = sum(item.filesize or 0 for item in bundle.items) or None
        bundle.thumbnail = bundle.items[0].url
        bundle.uploader = first.get("user") or first.get("author") or None

        bundle.variants = [
            Variant(
                token="all" if len(bundle.items) > 1 else "orig",
                label=(f"كل الصور ({min(bundle.count, len(bundle.items))})"
                       if len(bundle.items) > 1 else "الصورة الأصلية"),
                kind=bundle.kind,
                format_spec="gallery",
                ext=bundle.items[0].ext or "jpg",
                filesize=bundle.total_bytes,
                merge=False,
                note="تُنزل النسخة الأصلية كما هي بلا إعادة ترميز",
            )
        ]
        if any(item.height and item.height >= 1000 for item in bundle.items):
            bundle.notes.append("توجد صور بدقة عالية (1000px فأكثر) ضمن الألبوم")
        if bundle.count > len(bundle.items):
            bundle.notes.append(
                f"الألبوم فيه {bundle.count} صورة، والعرض هنا {len(bundle.items)}؛ التنزيل يشمل الكل"
            )
        if want is Want.VIDEO:
            bundle.notes.append("هذا محرك صور: أرسل رابط الفيديو مباشرة أو استخدم المنصة الأساسية")
        return bundle

    # ---------------- download ----------------

    async def download(self, bundle: Bundle, variant: Variant | None, dest_dir: Path, *,
                       progress: ProgressHook | None = None,
                       cookies_path: Path | None = None) -> DownloadResult:
        expected = max(bundle.count, len(bundle.items))
        code, stdout, stderr = await self._run(
            _command(["--destination", str(dest_dir)], bundle.url, cookies_path),
            timeout=_TIMEOUT_DOWNLOAD,
            progress=progress,
            dest_dir=dest_dir,
            expected=expected,
        )
        if code != 0:
            raise self._error_for(code, stderr + stdout)

        files = sorted((p for p in dest_dir.rglob("*") if p.is_file() and p.suffix.lower() != ".json"),
                       key=lambda p: (len(p.parts), p.name))
        if not files:
            raise EngineError("انتهى محرك الصور بلا ملفات على القرص.", detail=f"dest={dest_dir}")
        total = sum(p.stat().st_size for p in files)
        if progress:
            await progress(100.0, f"{len(files)} ملف · {human_size(total)}")
        return DownloadResult(files=files, bytes_total=total, engine=self.engine_id)

    # ---------------- plumbing ----------------

    async def _run(self, cmd: list[str], *, timeout: float, progress: ProgressHook | None = None,
                   dest_dir: Path | None = None, expected: int = 0) -> tuple[int, str, str]:
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:  # pragma: no cover - broken env
            raise EngineError("تعذر تشغيل محرك الصور.", detail=str(exc)) from exc

        watcher: asyncio.Task[None] | None = None
        if progress and dest_dir and expected:
            watcher = asyncio.create_task(
                _watch_output(progress, dest_dir, expected)
            )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError_(
                f"تجاوز محرك الصور مهلة {int(timeout)} ثانية على هذا الرابط."
            ) from None
        finally:
            if watcher:
                watcher.cancel()

        return (
            process.returncode or 0,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
        )

    def _error_for(self, code: int, text: str) -> Exception:
        lowered = text.lower()
        if any(hint in lowered for hint in ("403", "login", "cookies", "authorization")):
            return AuthRequiredError(detail=text[-500:])
        if "429" in lowered or "rate limit" in lowered:
            return RateLimitedError(detail=text[-500:])
        if "unsupported" in lowered or "no extractor" in lowered or code == 4:
            return UnsupportedPlatformError(detail=text[-500:])
        return EngineError(detail=text[-500:] or f"exit={code}")


def _parse_ndjson(stdout: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            records.append(data)
    return records


def _item_from_record(record: dict[str, Any], index: int) -> MediaItem:
    url = record.get("url") or record.get("source_url") or ""
    ext = (record.get("extension") or Path(url).suffix.lstrip(".") or "").lower() or None
    return MediaItem(
        url=url,
        kind=MediaKind.IMAGE if ext not in {"mp4", "webm", "mov", "gif"} else MediaKind.VIDEO,
        name=record.get("filename") or record.get("basename"),
        width=record.get("width"),
        height=record.get("height"),
        filesize=record.get("filesize"),
        ext=ext,
        source=record.get("date"),
        index=record.get("num") or index + 1,
    )


def _title(record: dict[str, Any], url: str) -> str | None:
    for key in ("title", "description", "post_id", "tag_string"):
        value = record.get(key)
        if value:
            text = re.sub(r"\s+", " ", str(value)).strip()
            return text[:90]
    from urllib.parse import urlsplit

    return urlsplit(url).path.strip("/").split("/")[-1][:60] or None


async def _watch_output(progress: ProgressHook, dest_dir: Path, expected: int) -> None:
    seen = 0
    while True:
        await asyncio.sleep(1.5)
        files = [p for p in dest_dir.rglob("*") if p.is_file() and p.suffix.lower() != ".json"]
        count = len(files)
        if count != seen:
            seen = count
            percent = min(99.0, count / expected * 100) if expected else 0.0
            total = sum(p.stat().st_size for p in files)
            await progress(percent, f"{count}/{expected} ملف · {human_size(total)}")


__all__ = ["GalleryDlExtractor", "PREVIEW_LIMIT"]
