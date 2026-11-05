"""gallery-dl engine: JSON parsing, album bundles and error mapping.

The subprocess is stubbed with recorded gallery-dl output, so these tests are
deterministic and offline; the real CLI contract is exercised by scripts/live_probe.py.
"""

from __future__ import annotations

import json

import pytest

from media_bot.errors import (
    AuthRequiredError,
    EngineError,
    NoMediaFoundError,
    RateLimitedError,
    TimeoutError_,
    UnsupportedPlatformError,
)
from media_bot.extractors import gallerydl_engine as gd
from media_bot.extractors.base import MediaKind
from media_bot.extractors.gallerydl_engine import GalleryDlExtractor, _parse_ndjson

RECORDED = [
    {"url": "https://i.pinimg.com/originals/aa.jpg", "extension": "jpg", "width": 2048,
     "height": 3072, "filename": "pinned.jpg", "num": 1, "count": 3, "user": "somebody"},
    {"url": "https://i.pinimg.com/originals/bb.jpg", "extension": "jpg", "width": 1024,
     "height": 768, "filename": "second.jpg", "num": 2, "count": 3},
    {"url": "https://i.pinimg.com/videos/clip.mp4", "extension": "mp4", "width": 1080,
     "height": 1920, "filename": "clip.mp4", "num": 3, "count": 3},
]


@pytest.fixture
def engine(settings) -> GalleryDlExtractor:
    return GalleryDlExtractor(settings)


def stub_run(result: tuple[int, str, str]):
    async def _fake(self, cmd, **kwargs):
        self.last_cmd = cmd
        return result

    return _fake


async def test_album_bundle_from_recorded_json(engine, monkeypatch) -> None:
    monkeypatch.setattr(GalleryDlExtractor, "_run", stub_run((0, "\n".join(
        json.dumps(row) for row in RECORDED), "")))
    bundle = await engine.extract("https://pinterest.com/pin/1/")
    assert bundle.kind is MediaKind.ALBUM
    assert bundle.engine == "gallery-dl"
    assert len(bundle.items) == 3
    assert bundle.count == 3
    assert bundle.items[0].width == 2048
    assert bundle.items[2].kind is MediaKind.VIDEO
    assert bundle.variants[0].token == "all"
    assert bundle.uploader == "somebody"


async def test_preview_command_shape(engine, monkeypatch) -> None:
    captured = {}

    async def capture(self, cmd, **kwargs):
        captured["cmd"] = cmd
        return 0, json.dumps(RECORDED[0]), ""

    monkeypatch.setattr(GalleryDlExtractor, "_run", capture)
    await engine.extract("https://pinterest.com/pin/1/")
    cmd = captured["cmd"]
    assert "-J" in cmd and "--simulate" in cmd
    assert "--range" in cmd
    assert cmd[-1] == "https://pinterest.com/pin/1/"


async def test_single_image_is_not_an_album(engine, monkeypatch) -> None:
    monkeypatch.setattr(GalleryDlExtractor, "_run", stub_run((0, json.dumps(RECORDED[0]), "")))
    bundle = await engine.extract("https://pinterest.com/pin/1/")
    assert bundle.kind is MediaKind.IMAGE
    assert bundle.variants[0].token == "orig"


async def test_truncated_notice_when_album_is_larger_than_preview(engine, monkeypatch) -> None:
    payload = [dict(row, count=40) for row in RECORDED]
    monkeypatch.setattr(GalleryDlExtractor, "_run",
                        stub_run((0, "\n".join(json.dumps(row) for row in payload), "")))
    bundle = await engine.extract("https://pinterest.com/pin/1/")
    assert bundle.count == 40
    assert any("40" in note for note in bundle.notes)


async def test_empty_output_raises_nomedia(engine, monkeypatch) -> None:
    monkeypatch.setattr(GalleryDlExtractor, "_run", stub_run((0, "", "")))
    with pytest.raises(NoMediaFoundError):
        await engine.extract("https://pinterest.com/pin/1/")


@pytest.mark.parametrize("code,stderr,expected", [
    (1, "gallery-dl ERROR: Pinterest: Authorization required (403)", AuthRequiredError),
    (4, "gallery-dl: no extractor for this url", UnsupportedPlatformError),
    (1, "HTTP 429 Too Many Requests", RateLimitedError),
    (1, "some other failure", EngineError),
])
async def test_exit_codes_map_to_typed_errors(engine, monkeypatch, code, stderr, expected) -> None:
    monkeypatch.setattr(GalleryDlExtractor, "_run", stub_run((code, "", stderr)))
    with pytest.raises(expected):
        await engine.extract("https://pinterest.com/pin/1/")


async def test_timeout_becomes_timeout_error(engine, monkeypatch) -> None:
    async def blow_up(self, cmd, **kwargs):
        raise TimeoutError_()

    monkeypatch.setattr(GalleryDlExtractor, "_run", blow_up)
    with pytest.raises(TimeoutError_):
        await engine.extract("https://pinterest.com/pin/1/")


async def test_download_collects_written_files(engine, monkeypatch, tmp_path) -> None:
    nested = tmp_path / "pinterest" / "album"
    nested.mkdir(parents=True)
    (nested / "one.jpg").write_bytes(b"x" * 500)
    (nested / "two.jpg").write_bytes(b"y" * 700)
    (nested / "meta.json").write_text("{}")

    async def fake(self, cmd, **kwargs):
        assert "--destination" in cmd
        return 0, "", ""

    monkeypatch.setattr(GalleryDlExtractor, "_run", fake)
    from media_bot.extractors.base import Bundle, Variant

    bundle = Bundle(engine="gallery-dl", platform="Pinterest", url="https://x",
                    kind=MediaKind.ALBUM, count=2)
    bundle.variants = [Variant(token="all", label="الكل", kind=MediaKind.ALBUM,
                               format_spec="gallery", ext="jpg")]
    result = await engine.download(bundle, bundle.variants[0], tmp_path)
    assert result.bytes_total == 1200
    assert [p.name for p in result.files] == ["one.jpg", "two.jpg"]
    assert all(p.suffix != ".json" for p in result.files)


async def test_download_with_no_files_raises(engine, monkeypatch, tmp_path) -> None:
    async def fake(self, cmd, **kwargs):
        return 0, "", ""

    monkeypatch.setattr(GalleryDlExtractor, "_run", fake)
    from media_bot.extractors.base import Bundle

    bundle = Bundle(engine="gallery-dl", platform="Pinterest", url="https://x",
                    kind=MediaKind.ALBUM, count=1)
    with pytest.raises(EngineError):
        await engine.download(bundle, None, tmp_path)


def test_ndjson_parser_ignores_noise() -> None:
    lines = ["not json", "{bad", json.dumps(RECORDED[0]), "", "   "]
    assert len(_parse_ndjson("\n".join(lines))) == 1


def test_cookie_flag_is_forwarded(settings, monkeypatch, tmp_path) -> None:
    engine = GalleryDlExtractor(settings)
    captured = {}

    async def capture(self, cmd, **kwargs):
        captured["cmd"] = cmd
        return 0, json.dumps(RECORDED[0]), ""

    monkeypatch.setattr(GalleryDlExtractor, "_run", capture)
    import asyncio

    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text("# Netscape HTTP Cookie File\n")
    asyncio.run(engine.extract("https://pinterest.com/pin/1/", cookies_path=cookie_file))
    assert "--cookies" in captured["cmd"]
    assert str(cookie_file) in captured["cmd"]


def test_supports_routing(settings) -> None:
    engine = GalleryDlExtractor(settings)
    assert engine.supports("pinterest.com", "https://pinterest.com/x", want=gd.Want.IMAGE)
    assert not engine.supports("youtube.com", "https://youtube.com/watch?v=1", want=gd.Want.VIDEO)
    assert engine.supports("random.example", "https://random.example/a", want=gd.Want.AUTO)
