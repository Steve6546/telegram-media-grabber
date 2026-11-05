"""Registry routing: which engine runs, in what order, and what gets reported."""

from __future__ import annotations

import pytest

from media_bot.errors import (
    AuthRequiredError,
    DRMMediaError,
    NoMediaFoundError,
    TimeoutError_,
    UnsupportedPlatformError,
)
from media_bot.extractors.base import Bundle, Extractor, MediaKind, Variant, Want
from media_bot.extractors.registry import Registry
from media_bot.platform_detector import detect, engine_order

from .conftest import make_bundle


class SpyEngine(Extractor):
    """A configurable engine stub that records whether it ran."""

    provides = frozenset({MediaKind.VIDEO})

    def __init__(self, settings, engine_id: str, *, priority: int = 10,
                 result: Bundle | None = None, raises: Exception | None = None,
                 supported: bool = True) -> None:
        super().__init__(settings)
        self.engine_id = engine_id
        self.priority = priority
        self.result = result or make_bundle(engine=engine_id)
        self.raises = raises
        self.supported = supported
        self.extracts = 0
        self.downloads = 0

    def supports(self, host: str, url: str, *, want: Want = Want.AUTO) -> bool:
        return self.supported

    async def extract(self, url, *, host="", cookies_path=None, want=Want.AUTO) -> Bundle:
        self.extracts += 1
        if self.raises:
            raise self.raises
        return self.result

    async def download(self, bundle, variant, dest_dir, *, progress=None, cookies_path=None):
        from media_bot.extractors.base import DownloadResult

        self.downloads += 1
        target = dest_dir / f"{self.engine_id}.mp4"
        target.write_bytes(b"x" * 128)
        return DownloadResult(files=[target], bytes_total=128, engine=self.engine_id)


@pytest.fixture
def registry(settings):
    def build(*engines: SpyEngine) -> Registry:
        return Registry(settings, list(engines))

    return build


async def test_first_supporting_engine_wins(registry, settings) -> None:
    first = SpyEngine(settings, "a", priority=1)
    second = SpyEngine(settings, "b", priority=2)
    bundle = await registry(first, second).extract("https://example.test/v")
    assert bundle.engine == "a"
    assert (first.extracts, second.extracts) == (1, 0)


async def test_unsupported_engine_is_skipped(registry, settings) -> None:
    skip = SpyEngine(settings, "skip", priority=1, supported=False)
    work = SpyEngine(settings, "work", priority=2)
    bundle = await registry(skip, work).extract("https://example.test/v")
    assert bundle.engine == "work" and skip.extracts == 0


async def test_falls_through_after_first_engine_fails(registry, settings) -> None:
    bad = SpyEngine(settings, "bad", priority=1, raises=NoMediaFoundError())
    good = SpyEngine(settings, "good", priority=2)
    bundle = await registry(bad, good).extract("https://example.test/v")
    assert bundle.engine == "good"


async def test_drm_stops_the_search_entirely(registry, settings) -> None:
    """A protected stream must not go shopping for another engine to bypass it."""
    drm = SpyEngine(settings, "drm", priority=1, raises=DRMMediaError())
    other = SpyEngine(settings, "other", priority=2)
    with pytest.raises(DRMMediaError):
        await registry(drm, other).extract("https://example.test/v")
    assert other.extracts == 0


async def test_auth_error_is_reported_over_less_useful_ones(registry, settings) -> None:
    nope = SpyEngine(settings, "nope", priority=1, raises=NoMediaFoundError())
    auth = SpyEngine(settings, "auth", priority=2, raises=AuthRequiredError())
    with pytest.raises(AuthRequiredError):
        await registry(nope, auth).extract("https://example.test/v")


async def test_platform_verdict_beats_generic_timeout(registry, settings) -> None:
    slow = SpyEngine(settings, "slow", priority=1, raises=TimeoutError_())
    platform = SpyEngine(settings, "plat", priority=2, raises=UnsupportedPlatformError())
    with pytest.raises(UnsupportedPlatformError):
        await registry(slow, platform).extract("https://example.test/v")


async def test_engine_crash_is_wrapped_not_raised_raw(registry, settings) -> None:
    broken = SpyEngine(settings, "broken", priority=1, raises=RuntimeError("boom"))
    good = SpyEngine(settings, "good", priority=2)
    bundle = await registry(broken, good).extract("https://example.test/v")
    assert bundle.engine == "good"


async def test_no_candidates_raises_clear_error(registry, settings) -> None:
    reg = registry(SpyEngine(settings, "no", supported=False))
    with pytest.raises(UnsupportedPlatformError):
        await reg.extract("https://example.test/v")


async def test_download_dispatches_to_the_bundle_engine(registry, settings, tmp_path) -> None:
    a = SpyEngine(settings, "a", priority=1)
    b = SpyEngine(settings, "b", priority=2)
    bundle = await registry(a, b).extract("https://example.test/v")
    result = await registry(a, b).download(bundle, bundle.variants[0], tmp_path)
    assert result.engine == "a"
    assert (a.downloads, b.downloads) == (1, 0)


async def test_unknown_engine_id_on_download_is_reported(settings, tmp_path) -> None:
    a = SpyEngine(settings, "a")
    reg = Registry(settings, [a])
    bundle = make_bundle(engine="ghost")
    with pytest.raises(UnsupportedPlatformError):
        await reg.download(bundle, bundle.variants[0], tmp_path)


@pytest.mark.parametrize("url,platform,first_engine", [
    ("https://www.youtube.com/watch?v=x", "YouTube", "ytdlp"),
    ("https://vimeo.com/123", "Vimeo", "ytdlp"),
    ("https://www.tiktok.com/@user/video/123", "TikTok", "ytdlp"),
    ("https://www.pinterest.com/pin/123/", "Pinterest", "gallery-dl"),
    ("https://x.com/user/status/1", "X / Twitter", "ytdlp"),
    ("https://www.snapchat.com/spotlight/abc", "Snapchat", "ytdlp"),
    ("https://clips.twitch.tv/SomeClip", "Twitch", "ytdlp"),
    ("https://some-unknown-site.example/page", "موقع عام", "ytdlp"),
])
def test_known_platforms_route_as_designed(url, platform, first_engine) -> None:
    detected = detect(url)
    assert detected.label == platform
    assert engine_order(detected)[0] == first_engine


def test_image_appetite_promotes_gallery_dl() -> None:
    assert engine_order(detect("https://youtu.be/x"), want=Want.VIDEO)[0] == "ytdlp"
    assert engine_order(detect("https://pinterest.com/pin/1"), want=Want.IMAGE)[0] == "gallery-dl"


def test_generic_platform_lists_direct_last() -> None:
    assert engine_order(detect("https://random.example/page"), want=Want.AUTO)[-1] == "direct"


def test_whatsapp_declines_with_a_reason() -> None:
    platform = detect("https://wa.me/12345")
    assert platform.engines == ()
    assert platform.notes


def test_status_lists_every_engine(settings) -> None:
    reg = Registry(settings, [SpyEngine(settings, "a"), SpyEngine(settings, "b")])
    report = reg.status()
    assert [row["engine"] for row in report] == ["a", "b"]
    assert all("provides" in row for row in report)


def test_bundle_serialisation_roundtrip() -> None:
    bundle = make_bundle()
    bundle.variants.append(Variant(token="x", label="X", kind=MediaKind.IMAGE,
                                   format_spec="1", ext="jpg"))
    restored = Bundle.from_dict(bundle.to_dict())
    assert restored.kind == bundle.kind
    assert [v.token for v in restored.variants] == [v.token for v in bundle.variants]
    assert restored.title == bundle.title
