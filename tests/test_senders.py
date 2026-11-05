"""Sender ceilings and message-shape decisions.

The Bot API limits here are not decoration: 50 MB is the real send ceiling and the
sender must refuse rather than let Telegram fail the upload silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiogram.types import URLInputFile

from media_bot.errors import FileTooLargeError, SenderError
from media_bot.extractors.base import MediaKind
from media_bot.senders import (
    BOT_API_SEND_LIMIT,
    CAPTION_LIMIT,
    LocalApiSender,
    _clip,
    media_type_for,
)
from media_bot.senders import BotApiSender


class Sent:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class StubBot:
    """Records which Telegram method the sender chose, with what arguments."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._id = 100
        self.refuse = refuse

    async def get_me(self):
        class Me:
            username = "stub_bot"
            id = 7
        return Me()

    async def _record(self, method: str, **kwargs):
        if self.refuse:
            raise RuntimeError("Telegram 400: wrong file identifier")
        self._id += 1
        self.calls.append((method, kwargs))
        return Sent(self._id)

    async def send_video(self, chat_id, video=None, **kw):
        return await self._record("video", chat_id=chat_id, video=video, **kw)

    async def send_audio(self, chat_id, audio=None, **kw):
        return await self._record("audio", chat_id=chat_id, audio=audio, **kw)

    async def send_photo(self, chat_id, photo=None, **kw):
        return await self._record("photo", chat_id=chat_id, photo=photo, **kw)

    async def send_animation(self, chat_id, animation=None, **kw):
        return await self._record("animation", chat_id=chat_id, animation=animation, **kw)

    async def send_document(self, chat_id, document=None, **kw):
        return await self._record("document", chat_id=chat_id, document=document, **kw)


def write_file(tmp_path: Path, name: str, size: int) -> Path:
    path = tmp_path / name
    with open(path, "wb") as handle:
        handle.truncate(size)
    return path


async def test_video_goes_as_video_with_streaming(tmp_path) -> None:
    bot = StubBot()
    sender = BotApiSender(bot)
    path = write_file(tmp_path, "clip.mp4", 1024)
    outcome = await sender.send(11, [path], caption="مرحباً", kind=MediaKind.VIDEO)
    assert bot.calls[0][0] == "video"
    assert bot.calls[0][1]["supports_streaming"] is True
    assert outcome.bytes_sent == 1024 and outcome.sender == "bot_api"


async def test_matroska_is_sent_as_document(tmp_path) -> None:
    """Telegram cannot stream mkv, so sending it as video would produce an unplayable blob."""
    bot = StubBot()
    sender = BotApiSender(bot)
    path = write_file(tmp_path, "film.mkv", 2048)
    await sender.send(11, [path], caption=None, kind=MediaKind.VIDEO)
    assert bot.calls[0][0] == "document"


async def test_audio_extension_selects_audio(tmp_path) -> None:
    bot = StubBot()
    await BotApiSender(bot).send(11, [write_file(tmp_path, "song.mp3", 500)],
                                caption="🎵", kind=MediaKind.AUDIO)
    assert bot.calls[0][0] == "audio"


async def test_gif_goes_as_animation(tmp_path) -> None:
    bot = StubBot()
    await BotApiSender(bot).send(11, [write_file(tmp_path, "loop.gif", 300)],
                                caption=None, kind=MediaKind.VIDEO)
    assert bot.calls[0][0] == "animation"


async def test_single_file_over_50mb_is_refused(tmp_path) -> None:
    sender = BotApiSender(StubBot())
    big = write_file(tmp_path, "huge.mp4", BOT_API_SEND_LIMIT + 1)
    with pytest.raises(FileTooLargeError) as caught:
        await sender.send(11, [big], caption=None, kind=MediaKind.VIDEO)
    assert "50" in str(caught.value) or "MB" in str(caught.value)


async def test_album_skips_oversized_members_and_says_so(tmp_path) -> None:
    bot = StubBot()
    sender = BotApiSender(bot)
    good = write_file(tmp_path, "ok.jpg", 1000)
    bad = write_file(tmp_path, "too-big.jpg", BOT_API_SEND_LIMIT + 1)
    outcome = await sender.send(11, [good, bad], caption="album", kind=MediaKind.ALBUM)
    assert len(outcome.message_ids) == 1
    assert outcome.note and "تخطي" in outcome.note


async def test_force_document_overrides_type(tmp_path) -> None:
    bot = StubBot()
    await BotApiSender(bot).send(11, [write_file(tmp_path, "clip.mp4", 700)],
                                caption=None, kind=MediaKind.VIDEO, force_document=True)
    assert bot.calls[0][0] == "document"


async def test_telegram_refusal_becomes_sender_error(tmp_path) -> None:
    sender = BotApiSender(StubBot(refuse=True))
    with pytest.raises(SenderError):
        await sender.send(11, [write_file(tmp_path, "a.mp4", 10)], caption=None,
                          kind=MediaKind.VIDEO)


async def test_caption_is_clipped_to_telegram_limit(tmp_path) -> None:
    bot = StubBot()
    long = "ك" * (CAPTION_LIMIT + 400)
    await BotApiSender(bot).send(11, [write_file(tmp_path, "a.mp3", 10)], caption=long,
                                kind=MediaKind.AUDIO)
    sent_caption = bot.calls[0][1]["caption"]
    assert len(sent_caption) <= CAPTION_LIMIT
    assert sent_caption.endswith("…")


def test_clip_helper_keeps_short_text() -> None:
    assert _clip("short") == "short"
    assert _clip(None) is None


async def test_probe_reports_availability() -> None:
    status = await BotApiSender(StubBot()).probe()
    assert status["available"] is True and status["username"] == "stub_bot"


async def test_local_sender_has_the_larger_ceiling(tmp_path) -> None:
    bot = StubBot()
    sender = LocalApiSender(bot)
    assert sender.ceiling == 2000 * 1024 * 1024
    big = write_file(tmp_path, "bigfile.mp4", 60 * 1024 * 1024)
    outcome = await sender.send(11, [big], caption=None, kind=MediaKind.VIDEO)
    assert outcome.bytes_sent == 60 * 1024 * 1024


def test_media_type_mapping() -> None:
    assert media_type_for(Path("a.webm"), MediaKind.VIDEO) == "video"
    assert media_type_for(Path("a.png"), MediaKind.IMAGE) == "photo"
    assert media_type_for(Path("a.bin"), MediaKind.FILE) == "document"
    assert media_type_for(Path("a.opus"), MediaKind.AUDIO) == "audio"


def test_url_input_file_is_available_for_server_side_fetch() -> None:
    # aiogram >=3.14 renamed URLInputFile's timeout kwarg; pin the constructor shape we use
    file = URLInputFile(url="https://example.test/thumb.jpg", timeout=20)
    assert file.url.endswith("thumb.jpg")
