"""Engine registry: routes a URL to the right engine and keeps the bot engine-agnostic.

Ordering rule: the platform table says which engine usually wins, every other healthy
engine is still tried, and a DRM refusal stops the search instead of shopping around
for a way around it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..errors import (
    AuthRequiredError,
    BlockedHostError,
    DRMMediaError,
    GrabberError,
    NetworkError,
    NoMediaFoundError,
    RateLimitedError,
    TimeoutError_,
    UnsupportedPlatformError,
)
from ..platform_detector import detect, engine_order, host_of
from .base import Bundle, DownloadResult, Extractor, ProgressHook, Variant, Want

log = logging.getLogger("grabber.registry")


class Registry:
    def __init__(self, settings, engines: list[Extractor]) -> None:
        self.settings = settings
        self.engines = engines
        self._by_id = {engine.engine_id: engine for engine in engines}

    def get(self, engine_id: str) -> Extractor | None:
        return self._by_id.get(engine_id)

    def candidates(self, url: str, *, want: Want = Want.AUTO) -> list[Extractor]:
        platform = detect(url)
        host = host_of(url)
        ordered: list[Extractor] = []
        for engine_id in engine_order(platform, want=want):
            engine = self._by_id.get(engine_id)
            if engine and engine not in ordered and engine.supports(host, url, want=want):
                ordered.append(engine)
        for engine in sorted(self.engines, key=lambda item: item.priority):
            if engine not in ordered and engine.supports(host, url, want=want):
                ordered.append(engine)
        return ordered

    async def extract(self, url: str, *, want: Want = Want.AUTO,
                      cookies_path: Path | None = None) -> Bundle:
        host = host_of(url)
        attempts = self.candidates(url, want=want)
        if not attempts:
            raise UnsupportedPlatformError(f"لا محرك يقبل المضيف {host or url}.")

        deferred: list[GrabberError] = []
        for engine in attempts:
            try:
                bundle = await engine.extract(url, host=host, cookies_path=cookies_path, want=want)
            except DRMMediaError:
                raise  # a protected stream stays protected; no engine shopping for it
            except GrabberError as exc:
                log.info("%s refused %s: %s", engine.engine_id, url[:80], exc)
                deferred.append(exc)
                continue
            except Exception as exc:  # pragma: no cover - engine bug
                log.exception("%s crashed on %s", engine.engine_id, url[:80])
                deferred.append(GrabberError(f"سقوط داخلي في {engine.engine_id}.", detail=str(exc)))
                continue
            bundle.engine = engine.engine_id
            log.info("%s handled %s (%d variants, %d items)", engine.engine_id, url[:80],
                     len(bundle.variants), len(bundle.items))
            return bundle

        # Report the most actionable failure, not whichever engine happened to run last.
        raise self._most_informative(deferred) if deferred else NoMediaFoundError(
            "لم يعثر أي محرك على وسائط خلف هذا الرابط."
        )

    @staticmethod
    def _most_informative(errors: list[GrabberError]) -> GrabberError:
        rank = {
            AuthRequiredError: 0,
            RateLimitedError: 1,
            BlockedHostError: 2,
            UnsupportedPlatformError: 3,
            TimeoutError_: 4,
            NetworkError: 5,
            NoMediaFoundError: 6,
        }
        return min(errors, key=lambda exc: rank.get(type(exc), 7))

    async def download(self, bundle: Bundle, variant: Variant | None, dest: Path, *,
                       progress: ProgressHook | None = None,
                       cookies_path: Path | None = None) -> DownloadResult:
        engine = self._by_id.get(bundle.engine)
        if engine is None:
            raise UnsupportedPlatformError(f"المحرك {bundle.engine} غير مسجّل.")
        return await engine.download(bundle, variant, dest, progress=progress,
                                    cookies_path=cookies_path)

    def status(self) -> list[dict[str, Any]]:
        report: list[dict[str, Any]] = []
        for engine in sorted(self.engines, key=lambda item: item.priority):
            entry: dict[str, Any] = {
                "engine": engine.engine_id,
                "priority": engine.priority,
                "provides": sorted(kind.value for kind in engine.provides),
            }
            probe = getattr(engine, "availability", None)
            if callable(probe):
                try:
                    entry.update(probe())
                except Exception as exc:  # pragma: no cover - diagnostics only
                    entry["error"] = str(exc)
            report.append(entry)
        return report


def build_registry(settings) -> Registry:
    from .direct_url import DirectUrlExtractor
    from .gallerydl_engine import GalleryDlExtractor
    from .ytdlp_engine import YtdlpExtractor

    return Registry(settings, [
        YtdlpExtractor(settings),
        GalleryDlExtractor(settings),
        DirectUrlExtractor(settings),
    ])


__all__ = ["Registry", "build_registry"]
