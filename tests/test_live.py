"""Live smoke tests against public, permitted URLs.

Deselected by default (`-m "not live"` implicitly via the marker config) because third-party
sites change and network availability is not the code's fault. Run them with:

    python -m pytest -q -m live

URLs here were each fetched and confirmed to exist before being listed.
"""

from __future__ import annotations

import pytest

from media_bot.config import Settings
from media_bot.extractors.base import MediaKind, Want
from media_bot.extractors.direct_url import DirectUrlExtractor
from media_bot.extractors.registry import build_registry
from media_bot.security import check_url

pytestmark = pytest.mark.live

# Creative-commons / openly licensed samples and stable direct-file endpoints.
DIRECT_VIDEO = "https://download.samplelib.com/mp4/sample-5s.mp4"
DIRECT_IMAGE = "https://picsum.photos/2000/1200"
YOUTUBE_CC = "https://www.youtube.com/watch?v=aqz-KE-bpKQ"


@pytest.fixture
def settings(tmp_path) -> Settings:
    cfg = Settings(data_dir=tmp_path / "live-data")
    cfg.validate()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.downloads_dir.mkdir(parents=True, exist_ok=True)
    return cfg


async def test_direct_video_analyses_and_downloads(settings, tmp_path) -> None:
    engine = DirectUrlExtractor(settings)
    bundle = await engine.extract(check_url(DIRECT_VIDEO))
    assert bundle.kind is MediaKind.VIDEO
    assert bundle.total_bytes and bundle.total_bytes > 100_000
    result = await engine.download(bundle, bundle.variants[0], tmp_path)
    assert result.bytes_total == bundle.total_bytes


async def test_direct_image_analyses(settings) -> None:
    engine = DirectUrlExtractor(settings)
    bundle = await engine.extract(check_url(DIRECT_IMAGE))
    assert bundle.kind is MediaKind.IMAGE
    assert bundle.thumbnail


async def test_ytdlp_reports_only_real_qualities(settings) -> None:
    registry = build_registry(settings)
    bundle = await registry.extract(check_url(YOUTUBE_CC), want=Want.VIDEO)
    assert bundle.engine == "ytdlp"
    heights = {v.height for v in bundle.variants if v.height}
    assert heights, "expected at least one real height"
    # whatever it lists must be downloadable
    target = min(bundle.variants, key=lambda v: (v.filesize or 10**12))
    result = await registry.download(bundle, target, settings.downloads_dir)
    assert result.bytes_total > 0
    assert result.files


async def test_unsupported_host_fails_with_a_typed_error(settings) -> None:
    from media_bot.errors import GrabberError

    registry = build_registry(settings)
    with pytest.raises(GrabberError) as caught:
        await registry.extract(check_url("https://example.com/definitely-not-media"))
    assert caught.value.code in {"E_NOMEDIA", "E_PLATFORM", "E_AUTH", "E_NET", "E_TIMEOUT"}
