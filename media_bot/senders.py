"""Telegram senders and their real ceilings.

Facts this module encodes, from the Bot API documentation rather than guesswork:

* ``sendVideo``/``sendDocument`` through the hosted Bot API accept **50 MB**.
* ``getFile`` (downloading what a user sent you) is capped at **20 MB** for bots.
* A self-hosted *Local Bot API Server* raises the send ceiling to **2 GB**, and the bot
  must pass a *server-side path* instead of bytes — which only works when the bot and
  the API server share a filesystem.
* MTProto (a user session, not a bot token) also reaches **2 GB** (4 GB with Premium),
  but the file is then sent *from that user account*, not from the bot.

Choosing a sender is therefore a capability test, not a preference, and when the file
is over the ceiling the user is told exactly which ceiling blocked it and what to do.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.client.telegram import TelegramAPIServer, SimpleFilesPathWrapper
from aiogram.types import FSInputFile, Message

from .errors import FileTooLargeError, SenderError
from .extractors.base import MediaKind, human_duration, human_size

log = logging.getLogger("grabber.senders")

BOT_API_SEND_LIMIT = 50 * 1024 * 1024
LOCAL_API_SEND_LIMIT = 2000 * 1024 * 1024
MTPROTO_SEND_LIMIT = 2000 * 1024 * 1024
MTPROTO_PREMIUM_LIMIT = 4000 * 1024 * 1024
#: Telegram refuses caption text longer than this
CAPTION_LIMIT = 1024


@dataclass(slots=True)
class SendOutcome:
    message_ids: list[int]
    sender: str
    bytes_sent: int
    fallback_used: bool = False
    note: str | None = None


def build_bot(settings) -> Bot:
    """Create the Bot client, wiring the local API server when configured."""
    session = build_session(settings)
    if session is None:
        return Bot(token=settings.bot_token)
    return Bot(token=settings.bot_token, session=session)


def build_session(settings):
    """Aiohttp session pointed at a Local Bot API Server, or None for hosted API."""
    if settings.send_mode not in {"auto", "local_api"}:
        return None
    if not settings.local_api_url:
        return None
    from aiogram.client.session.aiohttp import AiohttpSession

    kwargs: dict[str, Any] = {"is_local": True}
    if settings.local_files_root and settings.bot_files_root:
        kwargs["wrap_local_file"] = SimpleFilesPathWrapper(
            Path(settings.local_files_root), Path(settings.bot_files_root)
        )
    server = TelegramAPIServer.from_base(settings.local_api_url, **kwargs)
    log.info("Telegram API base -> %s", server.base)
    return AiohttpSession(api=server)


def media_type_for(path: Path, kind: MediaKind) -> str:
    suffix = path.suffix.lower()
    if kind is MediaKind.AUDIO or suffix in {".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac", ".aac"}:
        return "audio"
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff"}:
        return "photo"
    if suffix == ".gif":
        return "animation"
    if kind is MediaKind.VIDEO or suffix in {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi"}:
        return "video"
    return "document"


class BotApiSender:
    """Hosted Bot API. 50 MB per file, sends bytes over HTTPS."""

    name = "bot_api"
    ceiling = BOT_API_SEND_LIMIT
    uses_paths = False

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def probe(self) -> dict[str, Any]:
        try:
            me = await self.bot.get_me()
            return {"available": True, "username": me.username, "id": me.id}
        except Exception as exc:
            return {"available": False, "error": str(exc)}

    async def send(self, chat_id: int, paths: list[Path], *, caption: str,
                   kind: MediaKind, force_document: bool = False,
                   duration: int | None = None, thumb: Path | None = None,
                   disable_notification: bool = False) -> SendOutcome:
        ids: list[int] = []
        total = 0
        oversized = [path for path in paths if path.stat().st_size > self.ceiling]
        if oversized and len(paths) == 1:
            raise FileTooLargeError(
                f"حجم الملف {human_size(oversized[0].stat().st_size)} يتجاوز حد Bot API "
                f"({human_size(self.ceiling)}).",
                detail=oversized[0].name,
            )
        for index, path in enumerate(paths):
            size = path.stat().st_size
            if size > self.ceiling:
                # A partial album is better than a silent failure: say what was skipped.
                log.warning("skipping oversized album member %s", path.name)
                continue
            text = caption if index == 0 else None
            sent = await self._send_one(chat_id, path, text, kind, force_document,
                                        duration if index == 0 else None, disable_notification)
            if sent is None:
                continue
            ids.append(sent.message_id)
            total += size
        if not ids:
            raise SenderError("رفض تيليجرام إرسال أي ملف من هذه المجموعة.")
        return SendOutcome(message_ids=ids, sender=self.name, bytes_sent=total,
                           note="تم تخطي الملفات التي تتجاوز 50 MB" if len(ids) < len(paths) else None)

    async def _send_one(self, chat_id: int, path: Path, text: str | None, kind: MediaKind,
                        force_document: bool, duration: int | None,
                        silent: bool) -> Message | None:
        bucket = "document" if force_document else media_type_for(path, kind)
        # Telegram only streams mp4 as a real video; anything else must go as a document
        # or the file arrives as an unplayable blob.
        if bucket == "video" and path.suffix.lower() not in {".mp4", ".m4v", ".mov", ".webm"}:
            bucket = "document"
        payload = FSInputFile(path, filename=path.name)
        caption = _clip(text)
        try:
            if bucket == "video":
                return await self.bot.send_video(chat_id, video=payload, caption=caption,
                                                 supports_streaming=True,
                                                 parse_mode=None, duration=duration or 0,
                                                 disable_notification=silent)
            if bucket == "audio":
                return await self.bot.send_audio(chat_id, audio=payload, caption=caption,
                                                 parse_mode=None, duration=duration or 0,
                                                 disable_notification=silent)
            if bucket == "photo" and caption:
                # sendPhoto does not take captions above the limit and silently drops some
                # metadata, so albums with text go as documents instead.
                return await self.bot.send_photo(chat_id, photo=payload, caption=caption,
                                                 parse_mode=None, disable_notification=silent)
            if bucket == "photo":
                return await self.bot.send_photo(chat_id, photo=payload,
                                                 disable_notification=silent)
            if bucket == "animation":
                return await self.bot.send_animation(chat_id, animation=payload, caption=caption,
                                                     parse_mode=None, disable_notification=silent)
            return await self.bot.send_document(chat_id, document=payload, caption=caption,
                                                parse_mode=None, disable_notification=silent)
        except Exception as exc:
            log.error("send failed (%s -> %s): %s", bucket, path.name, exc)
            raise SenderError(f"رفض تيليجرام إرسال {path.name}.", detail=str(exc)) from exc


class LocalApiSender(BotApiSender):
    """Local Bot API Server: 2 GB, and files are handed over as server-side paths."""

    name = "local_api"
    ceiling = LOCAL_API_SEND_LIMIT
    uses_paths = True

    async def probe(self) -> dict[str, Any]:
        base = await super().probe()
        base["transport"] = "local-server"
        return base

    async def _send_one(self, chat_id: int, path: Path, text: str | None, kind: MediaKind,
                        force_document: bool, duration: int | None,
                        silent: bool) -> Message | None:
        # The local server reads the path itself, so the file must exist on that host.
        return await super()._send_one(chat_id, path, text, kind, force_document, duration, silent)


def _clip(text: str | None) -> str | None:
    if not text:
        return None
    if len(text) <= CAPTION_LIMIT:
        return text
    return text[: CAPTION_LIMIT - 1] + "…"


def describe_limits() -> list[tuple[str, int, str]]:
    return [
        ("Bot API (استضافة تيليجرام)", BOT_API_SEND_LIMIT, "إرسال حتى 50 MB · تنزيل حتى 20 MB"),
        ("Local Bot API Server", LOCAL_API_SEND_LIMIT, "إرسال حتى 2 GB · يحتاج خادم محلي ومسار ملفات مشترك"),
        ("MTProto (حساب مستخدم)", MTPROTO_SEND_LIMIT, "إرسال حتى 2 GB (أو 4 GB مع Premium) · يظهر باسم الحساب"),
    ]


__all__ = [
    "BotApiSender", "LocalApiSender", "SendOutcome", "build_bot", "build_session",
    "media_type_for", "describe_limits", "BOT_API_SEND_LIMIT", "LOCAL_API_SEND_LIMIT",
    "MTPROTO_SEND_LIMIT", "CAPTION_LIMIT", "human_size", "human_duration",
]
