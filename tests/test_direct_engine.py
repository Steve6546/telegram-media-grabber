"""Direct-link engine against a real local HTTP server (no external network)."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from media_bot.errors import BlockedHostError, NoMediaFoundError
from media_bot.extractors import direct_url as direct_module
from media_bot.extractors.base import MediaKind
from media_bot.extractors.direct_url import DirectUrlExtractor, _ext_from, filename_from, kind_for

PAYLOAD = b"JUNK" * 4096  # 16 KB


def make_app() -> web.Application:
    app = web.Application()

    async def video(request: web.Request) -> web.Response:
        return web.Response(body=PAYLOAD, content_type="video/mp4")

    async def image(request: web.Request) -> web.Response:
        return web.Response(body=PAYLOAD, content_type="image/jpeg")

    async def html(request: web.Request) -> web.Response:
        return web.Response(text="<html>not media</html>", content_type="text/html")

    async def nometadata(request: web.Request) -> web.Response:
        # chunked response: no Content-Length at all
        response = web.StreamResponse()
        response.content_type = "video/webm"
        await response.prepare(request)
        await response.write(PAYLOAD)
        await response.write_eof()
        return response

    async def redirect(request: web.Request) -> web.Response:
        raise web.HTTPFound("/real/video.mp4")

    async def refused(request: web.Request) -> web.Response:
        return web.Response(status=403, text="denied")

    async def nohead(request: web.Request) -> web.Response:
        if request.method == "HEAD":
            return web.Response(status=405, text="no HEAD here")
        return web.Response(body=PAYLOAD, content_type="image/png")

    app.router.add_get("/real/video.mp4", video)
    app.router.add_get("/real/pic.jpg", image)
    app.router.add_get("/page", html)
    app.router.add_get("/chunked", nometadata)
    app.router.add_get("/go", redirect)
    app.router.add_get("/private", refused)
    app.router.add_get("/head-refused.png", nohead)
    return app


@pytest.fixture
async def server():
    app = make_app()
    test_server = TestServer(app)
    await test_server.start_server()
    base = f"http://{test_server.host}:{test_server.port}"
    yield base
    await test_server.close()


@pytest.fixture
def allow_loopback(monkeypatch):
    """The test server lives on 127.0.0.1, which production rules must block.

    Only the engine's own reference is swapped, so the SSRF rules themselves stay
    under test in test_security.py and in test_redirect_to_private_is_blocked below.
    """
    monkeypatch.setattr(direct_module, "check_url", lambda url: url)


async def test_video_url_extracts_size_and_kind(server, allow_loopback) -> None:
    engine = DirectUrlExtractor(_settings_having_urls())
    bundle = await engine.extract(f"{server}/real/video.mp4")
    assert bundle.kind is MediaKind.VIDEO
    assert bundle.total_bytes == len(PAYLOAD)
    assert bundle.engine == "direct"
    assert bundle.variants[0].token == "orig"
    assert bundle.variants[0].filesize == len(PAYLOAD)


async def test_image_url_is_typed_as_image(server, allow_loopback) -> None:
    engine = DirectUrlExtractor(_settings_having_urls())
    bundle = await engine.extract(f"{server}/real/pic.jpg")
    assert bundle.kind is MediaKind.IMAGE
    assert bundle.thumbnail and bundle.thumbnail.endswith("pic.jpg")


async def test_html_page_is_not_media(server, allow_loopback) -> None:
    engine = DirectUrlExtractor(_settings_having_urls())
    with pytest.raises(NoMediaFoundError):
        await engine.extract(f"{server}/page")


async def test_refused_url_reports_status_not_html(server, allow_loopback) -> None:
    engine = DirectUrlExtractor(_settings_having_urls())
    with pytest.raises(NoMediaFoundError) as caught:
        await engine.extract(f"{server}/private")
    assert "403" in str(caught.value)


async def test_chunked_stream_without_content_length_still_downloads(server, tmp_path, allow_loopback) -> None:
    engine = DirectUrlExtractor(_settings_having_urls())
    bundle = await engine.extract(f"{server}/chunked")
    assert bundle.total_bytes is None
    result = await engine.download(bundle, bundle.variants[0], tmp_path)
    assert result.bytes_total == len(PAYLOAD)
    assert list(tmp_path.glob("chunked*"))


async def test_download_follows_redirect(server, tmp_path, allow_loopback) -> None:
    engine = DirectUrlExtractor(_settings_having_urls())
    bundle = await engine.extract(f"{server}/go")
    result = await engine.download(bundle, bundle.variants[0], tmp_path)
    assert result.bytes_total == len(PAYLOAD)


async def test_head_refusal_falls_back_to_get(server, allow_loopback) -> None:
    engine = DirectUrlExtractor(_settings_having_urls())
    bundle = await engine.extract(f"{server}/head-refused.png")
    assert bundle.kind is MediaKind.IMAGE
    assert bundle.total_bytes == len(PAYLOAD)


async def test_resume_continues_a_partial_file(server, tmp_path, allow_loopback) -> None:
    engine = DirectUrlExtractor(_settings_having_urls())
    bundle = await engine.extract(f"{server}/real/video.mp4")
    name = "video.mp4"
    (tmp_path / (name + ".part")).write_bytes(PAYLOAD[:4096])

    # The fake server ignores Range, answering 200; the engine must then restart cleanly
    # rather than appending and producing a corrupted file.
    result = await engine.download(bundle, bundle.variants[0], tmp_path)
    produced = tmp_path / name
    assert produced.read_bytes() == PAYLOAD
    assert result.bytes_total == len(PAYLOAD)


async def test_progress_hook_reaches_100(server, tmp_path, allow_loopback) -> None:
    engine = DirectUrlExtractor(_settings_having_urls())
    bundle = await engine.extract(f"{server}/real/video.mp4")
    seen: list[float] = []

    async def report(percent: float, text: str | None) -> None:
        seen.append(percent)

    await engine.download(bundle, bundle.variants[0], tmp_path, progress=report)
    assert seen and seen[-1] == 100.0


async def test_redirect_to_private_is_blocked_with_real_rules() -> None:
    """No monkeypatching here: the actual SSRF gate must reject a private redirect."""
    engine = DirectUrlExtractor(_settings_having_urls())
    with pytest.raises(BlockedHostError):
        engine._next_hop("https://good.example/x.mp4", "http://169.254.169.254/latest/meta-data/")


def _settings_having_urls():
    from media_bot.config import Settings

    settings = Settings()
    settings.request_timeout_seconds = 15
    settings.read_timeout_seconds = 30
    return settings


def test_ext_from_url_and_mime() -> None:
    assert _ext_from("https://x/a.webp", None) == "webp"
    assert _ext_from("https://x/a", "video/mp4") == "mp4"
    # an unmapped subtype yields no extension rather than a invented one
    assert _ext_from("https://x/a", "application/octet-stream") is None
    assert _ext_from("https://x/a.tar.gz", None) == "gz"


def test_kind_mapping() -> None:
    assert kind_for("mp3", None) is MediaKind.AUDIO
    assert kind_for(None, "image/webp") is MediaKind.IMAGE
    assert kind_for(None, "application/zip") is MediaKind.FILE
    assert kind_for("weird", "application/vnd.thing") is MediaKind.FILE


def test_filename_from_disposition() -> None:
    assert filename_from("https://x/a.mp4", 'attachment; filename="My Video.mp4"', None) == "My Video.mp4"
    assert filename_from("https://x/b.mp4", "attachment; filename*=UTF-8''%D9%81%D9%8A%D9%84%D9%85.mp4",
                         None) == "فيلم.mp4"
    assert filename_from("https://x/c.png", None, "image/png") == "c.png"
    assert filename_from("https://x/", None, "audio/mpeg") == "media.mp3"
