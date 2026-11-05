"""Relaying media the user already has inside Telegram.

This is the legitimate reading of "content the user is allowed to access": the bot can
reach only what appears in its own conversations, and getFile caps at 20 MB.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from aiogram.types import Animation, Audio, Chat, Document, File, Message, PhotoSize, User, Video, Voice

from media_bot.config import Settings
from media_bot.errors import FileTooLargeError, NoMediaFoundError
from media_bot.extractors.base import MediaKind
from media_bot.telegram_media import BOT_API_FILE_LIMIT, TelegramMediaService, ref_from_message

CHAT = Chat(id=55, type="private")
FROM = User(id=55, is_bot=False, first_name="Test")


def message(**fields) -> Message:
    base = dict(chat=CHAT, from_user=FROM, date=datetime(2026, 1, 1))
    return Message.model_construct(id=1, **{**base, **fields})


def test_video_message_is_recognised() -> None:
    video = Video(file_id="vid1", file_unique_id="u1", duration=42, width=1280, height=720,
                  mime_type="video/mp4", file_size=9_000_000, file_name="holiday.mp4")
    ref = ref_from_message(message(video=video))
    assert ref.kind is MediaKind.VIDEO
    assert ref.name == "holiday.mp4"
    assert ref.duration == 42 and (ref.width, ref.height) == (1280, 720)
    assert ref.size == 9_000_000
    assert "video" in ref.describe()


def test_photo_takes_the_highest_resolution_size() -> None:
    sizes = [
        PhotoSize(file_id="p1", file_unique_id="u1", width=90, height=90),
        PhotoSize(file_id="p3", file_unique_id="u3", width=800, height=600),
        PhotoSize(file_id="p5", file_unique_id="u5", width=2560, height=1920, file_size=2_000_000),
    ]
    ref = ref_from_message(message(photo=sizes))
    assert ref.kind is MediaKind.IMAGE
    assert (ref.width, ref.height) == (2560, 1920)
    assert ref.file_id == "p5"
    assert ref.name.endswith(".jpg")


def test_document_without_extension_gets_one_from_mime() -> None:
    doc = Document(file_id="d1", file_unique_id="u1", file_size=1234,
                   mime_type="audio/opus", file_name="voice-note")
    ref = ref_from_message(message(document=doc))
    assert ref.name == "voice-note.opus"
    assert ref.kind is MediaKind.FILE


def test_voice_and_audio_map_to_audio_kind() -> None:
    voice = Voice(file_id="v1", file_unique_id="uv", duration=7, mime_type="audio/ogg",
                  file_size=1000)
    assert ref_from_message(message(voice=voice)).kind is MediaKind.AUDIO
    audio = Audio(file_id="a1", file_unique_id="ua", duration=200, performer="X",
                  title="Track", mime_type="audio/mpeg", file_size=5000)
    ref = ref_from_message(message(audio=audio))
    assert ref.kind is MediaKind.AUDIO and ref.name.endswith(".mp3")


def test_animation_is_video() -> None:
    anim = Animation(file_id="g1", file_unique_id="ug", width=320, height=240, duration=3,
                     mime_type="video/mp4", file_name="loop.gif")
    assert ref_from_message(message(animation=anim)).kind is MediaKind.VIDEO


def test_plain_text_message_has_no_media() -> None:
    assert ref_from_message(message(text="just words")) is None


def test_sticker_falls_back_to_image_kind() -> None:
    from aiogram.types import Sticker

    sticker = Sticker(file_id="s1", file_unique_id="us", type="regular", width=512, height=512,
                      is_animated=False, is_video=False, emoji=":)", set_name="pack")
    ref = ref_from_message(message(sticker=sticker))
    assert ref.kind is MediaKind.IMAGE


def test_dangerous_filename_is_sanitised() -> None:
    doc = Document(file_id="d2", file_unique_id="u2", file_size=10,
                   mime_type="video/mp4", file_name="../../../../etc/passwd.mp4")
    ref = ref_from_message(message(document=doc))
    assert "/" not in ref.name and ".." not in ref.name


class StubBot:
    """Mimics aiogram's file access surface."""

    def __init__(self, *, file_size: int | None = None, deny_get_file: bool = False) -> None:
        self.token = "1:stub"
        self.file_size = file_size
        self.deny_get_file = deny_get_file

    async def get_file(self, file_id: str) -> File:
        if self.deny_get_file:
            raise RuntimeError("Telegram 400: file is too big for bots")
        return File(file_id=file_id, file_unique_id="u", file_path=f"documents/{file_id}.mp4",
                    file_size=self.file_size)

    class _Session:
        class _Api:
            @staticmethod
            def file_url(token: str, path: str) -> str:
                return f"https://api.telegram.org/file/bot{token}/{path}"

        api = _Api()

        @staticmethod
        async def stream_content(url: str, chunk_size: int = 65536):
            for _ in range(4):
                yield b"chunk"

    session = _Session()


def settings_for(tmp_path) -> Settings:
    settings = Settings(data_dir=tmp_path / "data")
    settings.bot_token = "1:stub"
    settings.validate()
    return settings


async def test_download_streams_the_file_into_the_job_dir(tmp_path) -> None:
    ref = ref_from_message(message(video=Video(
        file_id="vid9", file_unique_id="u9", duration=1, width=64, height=64,
        mime_type="video/mp4", file_size=20, file_name="tiny.mp4")))
    service = TelegramMediaService(StubBot(file_size=20), settings_for(tmp_path))
    dest = tmp_path / "job"
    dest.mkdir()
    seen: list[float] = []

    async def report(percent: float, text: str | None) -> None:
        seen.append(percent)

    result = await service.download(ref, dest, progress=report)
    assert result.files[0].name == "tiny.mp4"
    assert result.files[0].read_bytes() == b"chunkchunkchunkchunk"
    assert result.bytes_total == 20
    assert seen[-1] == 100.0


async def test_files_over_the_getfile_limit_refuse_before_downloading(tmp_path) -> None:
    ref = ref_from_message(message(document=Document(
        file_id="big", file_unique_id="ub", file_size=BOT_API_FILE_LIMIT + 1,
        mime_type="video/mp4", file_name="big.mp4")))
    service = TelegramMediaService(StubBot(), settings_for(tmp_path))
    with pytest.raises(FileTooLargeError) as caught:
        await service.download(ref, tmp_path)
    assert "getFile" in str(caught.value)


async def test_get_file_refusal_becomes_nomedia(tmp_path) -> None:
    ref = ref_from_message(message(video=Video(
        file_id="gone", file_unique_id="ug", duration=1, width=64, height=64,
        mime_type="video/mp4", file_size=10, file_name="gone.mp4")))
    service = TelegramMediaService(StubBot(deny_get_file=True), settings_for(tmp_path))
    with pytest.raises(NoMediaFoundError):
        await service.download(ref, tmp_path)


def test_limit_constant_matches_documented_value() -> None:
    assert BOT_API_FILE_LIMIT == 20 * 1024 * 1024
