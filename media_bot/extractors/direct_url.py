"""Direct-link engine: files whose URL already points at the media itself.

Two jobs here. One is speed — a link ending in ``.mp4`` needs no page analysis.
The other is safety: redirects are followed by hand so every hop is re-validated
against the SSRF rules, instead of letting an open redirect walk us to 169.254.169.254.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp

from ..errors import (
    BlockedHostError,
    EngineError,
    NoMediaFoundError,
    StorageError,
    InvalidURLError,
)
from ..config import USER_AGENT
from ..security import check_url, sanitize_filename
from .base import (
    Bundle,
    DownloadResult,
    Extractor,
    MediaKind,
    ProgressHook,
    Variant,
    Want,
    human_size,
)

log = logging.getLogger("grabber.direct")

MAX_HOPS = 6

_EXT_KIND: dict[str, MediaKind] = {
    **{ext: MediaKind.VIDEO for ext in ("mp4", "m4v", "mkv", "webm", "mov", "avi", "flv", "ts", "3gp")},
    **{ext: MediaKind.AUDIO for ext in ("mp3", "m4a", "opus", "ogg", "wav", "flac", "aac", "aiff")},
    **{ext: MediaKind.IMAGE for ext in ("jpg", "jpeg", "png", "gif", "webp", "heic", "heif", "avif",
                                        "bmp", "tiff", "svg")},
    **{ext: MediaKind.FILE for ext in ("zip", "pdf", "epub", "tar", "gz", "rar", "7z", "iso")},
}

_MIME_KIND: dict[str, MediaKind] = {
    "video": MediaKind.VIDEO,
    "audio": MediaKind.AUDIO,
    "image": MediaKind.IMAGE,
}

_TEXT_TYPES = ("text/html", "application/xhtml", "text/plain", "application/json",
               "application/xml", "text/xml")


def kind_for(ext: str | None, content_type: str | None) -> MediaKind:
    if ext:
        cleaned = ext.lower().lstrip(".")
        if cleaned in _EXT_KIND:
            return _EXT_KIND[cleaned]
    if content_type:
        major = content_type.split(";")[0].strip().lower().split("/")[0]
        if major in _MIME_KIND:
            return _MIME_KIND[major]
    return MediaKind.FILE


def filename_from(url: str, disposition: str | None, content_type: str | None) -> str:
    if disposition:
        match = re.search(r"filename\*=UTF-8''([^;]+)|filename=\"?([^\";]+)\"?", disposition, re.I)
        if match:
            return unquote(match.group(1) or match.group(2) or "").strip()
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    if name:
        return name
    if content_type:
        # Same mime→extension table the extractor uses, so a name and a variant never
        # disagree about what container the file is in.
        ext = _ext_from(url, content_type)
        return f"media.{ext}" if ext else "media"
    return "media"


class DirectUrlExtractor(Extractor):
    engine_id = "direct"
    provides = frozenset({MediaKind.VIDEO, MediaKind.AUDIO, MediaKind.IMAGE, MediaKind.FILE})
    priority = 5

    def supports(self, host: str, url: str, *, want: Want = Want.AUTO) -> bool:
        return want in (Want.AUTO, Want.VIDEO, Want.AUDIO, Want.IMAGE)

    _REDIRECTS = (301, 302, 303, 307, 308)
    _METHOD_ERRORS = (400, 403, 405, 501)

    def _next_hop(self, current: str, location: str) -> str:
        candidate = urljoin(current, location)
        try:
            return check_url(candidate)
        except (BlockedHostError, InvalidURLError) as exc:
            raise BlockedHostError(
                f"التحويل يقود إلى عنوان محظور ({urlsplit(candidate).hostname}).",
                detail=str(exc),
            ) from exc

    async def _resolve(self, session: aiohttp.ClientSession, url: str,
                       method: str) -> tuple[str, dict[str, str]]:
        """Follow redirects by hand so every hop is re-validated against SSRF rules."""
        current = check_url(url)
        for _ in range(MAX_HOPS):
            response = await session.request(method, current, allow_redirects=False)
            async with response:
                if response.status in self._REDIRECTS:
                    location = response.headers.get("Location")
                    if not location:
                        raise NoMediaFoundError("تحويل بلا وجهة واضحة.")
                    current = self._next_hop(current, location)
                    continue
                return current, dict(response.headers) | {"_status": str(response.status)}
        raise NoMediaFoundError("الرابط يدور أكثر من الحد المسموح.")

    async def _head(self, url: str) -> tuple[str, dict[str, str]]:
        timeout = aiohttp.ClientTimeout(total=self.settings.request_timeout_seconds,
                                        connect=self.settings.request_timeout_seconds)
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        ) as session:
            final, headers = await self._resolve(session, url, "HEAD")
            status = int(headers.get("_status", "0"))
            # Many image CDNs answer HEAD with 405/403 or drop Content-Length; a GET of the
            # same resolved URL is the honest way to learn what the file really is.
            if status >= 400 or status in self._METHOD_ERRORS or not headers.get("Content-Length"):
                try:
                    get_final, get_headers = await self._resolve(session, url, "GET")
                except (aiohttp.ClientError, NoMediaFoundError):
                    if status >= 400:
                        raise NoMediaFoundError(f"الخادم ردّ بـ {status} على الرابط المباشر.")
                    return final, headers
                merged = {**headers, **{k: v for k, v in get_headers.items() if v}}
                return get_final, merged
            return final, headers

    async def extract(self, url: str, *, host: str = "", cookies_path: Path | None = None,
                      want: Want = Want.AUTO) -> Bundle:
        final_url, headers = await self._head(url)
        content_type = headers.get("Content-Type", "")
        status = int(headers.get("_status", "200") or 200)
        # A refusal is reported as a refusal. Checking this before the content-type sniff
        # matters: error pages are HTML, and calling a 403 "not a media file" sends the
        # user down the wrong path.
        if status >= 400:
            raise NoMediaFoundError(
                f"الخادم رفض الرابط المباشر (HTTP {status}).",
                detail=f"final_url={final_url} content_type={content_type}",
            )
        if any(token in content_type.lower() for token in _TEXT_TYPES):
            # This is a web page, not a file. Report the weakest possible error so the
            # registry surfaces the real page-analysis engine's verdict instead of this.
            raise NoMediaFoundError("الرابط صفحة HTML لا ملف وسائط مباشر.")

        ext = _ext_from(final_url, content_type)
        kind = kind_for(ext, content_type)
        length = headers.get("Content-Length")
        size = int(length) if length and length.isdigit() else None
        name = filename_from(final_url, headers.get("Content-Disposition"), content_type)

        bundle = self.new_bundle(url, platform="رابط مباشر", kind=kind)
        bundle.title = name
        bundle.webpage_url = final_url
        bundle.total_bytes = size
        bundle.count = 1
        bundle.variants = [Variant(
            token="orig",
            label=f"الملف الأصلي ({kind.value})",
            kind=kind,
            format_spec="direct",
            ext=ext or "bin",
            filesize=size,
            merge=False,
            note="يُنسخ كما هو بلا إعادة ترميز" if size else "الحجم غير معلن من الخادم",
        )]
        if size and size > self.settings.max_file_bytes:
            bundle.notes.append(
                f"الحجم {human_size(size)} يتجاوز حد المشروع {human_size(self.settings.max_file_bytes)}"
            )
        if kind is MediaKind.IMAGE:
            bundle.thumbnail = final_url
        return bundle

    async def download(self, bundle: Bundle, variant: Variant | None, dest_dir: Path, *,
                       progress: ProgressHook | None = None,
                       cookies_path: Path | None = None) -> DownloadResult:
        final_url, _ = await self._head(bundle.url)
        name = sanitize_filename(bundle.title or "media")
        target = dest_dir / name
        partial = dest_dir / (name + ".part")

        resume_from = partial.stat().st_size if (self.settings.resume and partial.is_file()) else 0
        headers = {"User-Agent": USER_AGENT}
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"

        timeout = aiohttp.ClientTimeout(total=None, sock_read=self.settings.read_timeout_seconds,
                                        connect=self.settings.request_timeout_seconds)
        # A 206 means the server honoured our Range header, so we keep appending to the
        # .part file. A 200 means it ignored it: restart cleanly into the final name.
        append_mode = False
        current = final_url
        hop = 0
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                while True:
                    response = await session.get(current, headers=headers, allow_redirects=False)
                    async with response:
                        if response.status in (301, 302, 303, 307, 308):
                            location = response.headers.get("Location")
                            if not location or hop >= MAX_HOPS:
                                raise NoMediaFoundError("الرابط يدور بلا نهاية أو بلا وجه واضح.")
                            # Re-validate every hop: an open redirect could point at us.
                            current = check_url(urljoin(current, location))
                            hop += 1
                            continue
                        if response.status not in (200, 206):
                            raise NoMediaFoundError(f"الخادم ردّ بـ {response.status}.")
                        append_mode = response.status == 206
                        if not append_mode:
                            resume_from = 0
                        raw_total = response.headers.get("Content-Length")
                        total = ((int(raw_total) + resume_from)
                                 if raw_total and raw_total.isdigit() else None)
                        done = resume_from
                        chunk_index = 0
                        last_emit = 0.0
                        sink = partial if append_mode else target
                        with open(sink, "ab" if append_mode else "wb") as handle:
                            async for chunk in response.content.iter_chunked(1024 * 512):
                                handle.write(chunk)
                                done += len(chunk)
                                chunk_index += 1
                                if progress and chunk_index % 8 == 0:
                                    now = time.monotonic()
                                    if now - last_emit >= 1.0:
                                        last_emit = now
                                        percent = min(99.0, done / total * 100) if total else 0.0
                                        await progress(percent, f"{human_size(done)} / {human_size(total)}")
                    break
        except aiohttp.ClientError as exc:
            raise EngineError("انقطع الاتصال أثناء التنزيل المباشر.", detail=str(exc)) from exc
        except OSError as exc:
            raise StorageError(detail=str(exc)) from exc

        if append_mode:
            partial.replace(target)
        if not target.is_file() or target.stat().st_size == 0:
            raise NoMediaFoundError("الملف المنزّل فارغ.")
        size = target.stat().st_size
        if progress:
            await progress(100.0, f"{human_size(size)}")
        return DownloadResult(files=[target], bytes_total=size, engine=self.engine_id)

    def availability(self) -> dict[str, Any]:
        return {"engine": self.engine_id, "resumable": self.settings.resume}


def _ext_from(url: str, content_type: str | None) -> str | None:
    tail = unquote(urlsplit(url).path).rsplit(".", 1)
    if len(tail) == 2 and 1 < len(tail[1]) <= 6 and tail[1].isalnum():
        return tail[1].lower()
    if content_type:
        subtype = content_type.split(";")[0].strip().split("/")[-1].lower()
        mapping = {"jpeg": "jpg", "svg+xml": "svg", "x-m4v": "m4v", "webp": "webp",
                   "mpeg3": "mp3", "mpeg": "mp3", "mp4": "mp4", "x-matroska": "mkv",
                   "ogg": "ogg", "x-flac": "flac", "pdf": "pdf", "zip": "zip"}
        return mapping.get(subtype, subtype if len(subtype) <= 5 else None)
    return None


__all__ = ["DirectUrlExtractor", "kind_for", "filename_from", "_ext_from"]
