"""Media the user already has inside Telegram.

The legitimate reading of "content the user has permission to access": files in the
user's own chats. Telegram only exposes what the bot can see in the conversation, so
this path serves forwarded/sent media — it cannot reach other people's private chats.

Two real ceilings apply, and the code states them instead of failing quietly:
``getFile`` downloads are capped at 20 MB per file, while sending via Bot API is 50 MB.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.types import Message

from .errors import FileTooLargeError, NoMediaFoundError, StorageError
from .extractors.base import DownloadResult, MediaKind, ProgressHook, human_size
from .security import sanitize_filename

log = logging.getLogger("grabber.tgmedia")

#: Telegram's getFile limit for bots
BOT_API_FILE_LIMIT = 20 * 1024 * 1024

_FIELD_FOR_KIND: dict[str, MediaKind] = {
    "video": MediaKind.VIDEO,
    "animation": MediaKind.VIDEO,
    "video_note": MediaKind.VIDEO,
    "audio": MediaKind.AUDIO,
    "voice": MediaKind.AUDIO,
    "photo": MediaKind.IMAGE,
    "document": MediaKind.FILE,
    "sticker": MediaKind.IMAGE,
}


class TelegramMediaRef:
    def __init__(self, *, file_id: str, kind: MediaKind, size: int | None, name: str,
                 mime: str | None, duration: int | None, width: int | None,
                 height: int | None, source: str, original_chat: str | None) -> None:
        self.file_id = file_id
        self.kind = kind
        self.size = size
        self.name = name
        self.mime = mime
        self.duration = duration
        self.width = width
        self.height = height
        self.source = source
        self.original_chat = original_chat

    def describe(self) -> str:
        parts = [self.kind.value, self.name]
        if self.size:
            parts.append(human_size(self.size))
        if self.duration:
            parts.append(f"{self.duration}s")
        if self.width and self.height:
            parts.append(f"{self.width}x{self.height}")
        return " · ".join(str(part) for part in parts)


def ref_from_message(message: Message) -> TelegramMediaRef | None:
    """Pick the largest media object attached to ``message``."""
    original = message.forward_origin if hasattr(message, "forward_origin") else None
    origin_chat = getattr(original, "sender_chat", None) or getattr(original, "sender", None)
    source_label = getattr(origin_chat, "title", None) or getattr(origin_chat, "username", None)

    for attribute, kind in _FIELD_FOR_KIND.items():
        payload: Any = getattr(message, attribute, None)
        if not payload:
            continue
        if attribute == "photo":
            # Photo sizes are an array; the last entry is the highest resolution.
            best = payload[-1]
            file_id, size = best.file_id, best.file_size
            mime, name = None, f"photo-{best.file_unique_id}.jpg"
            duration, width, height = None, best.width, best.height
        else:
            file_id = getattr(payload, "file_id", None)
            if not file_id:
                continue
            size = getattr(payload, "file_size", None)
            mime = getattr(payload, "mime_type", None)
            duration = getattr(payload, "duration", None)
            width = getattr(payload, "width", None)
            height = getattr(payload, "height", None)
            name = (getattr(payload, "file_name", None)
                    or getattr(payload, "emoji", None)
                    or getattr(payload, "title", None)
                    or f"{attribute}-{getattr(payload, 'file_unique_id', 'file')}")
            if mime and not Path(name).suffix:
                name = f"{name}{_ext_from_mime(mime)}"
        if not file_id:
            continue
        return TelegramMediaRef(
            file_id=file_id, kind=kind, size=size, name=sanitize_filename(name, fallback=attribute),
            mime=mime, duration=duration, width=width, height=height,
            source=f"telegram:{attribute}", original_chat=source_label,
        )
    return None


class TelegramMediaService:
    def __init__(self, bot: Bot, settings) -> None:
        self.bot = bot
        self.settings = settings

    async def download(self, ref: TelegramMediaRef, dest_dir: Path, *,
                       progress: ProgressHook | None = None) -> DownloadResult:
        if ref.size and ref.size > BOT_API_FILE_LIMIT:
            raise FileTooLargeError(
                f"حجم الملف {human_size(ref.size)} يتجاوز حد getFile في Bot API "
                f"({human_size(BOT_API_FILE_LIMIT)}).",
                detail=f"file_id={ref.file_id}",
            )

        try:
            tg_file = await self.bot.get_file(ref.file_id)
        except Exception as exc:
            raise NoMediaFoundError("تعذر على البوت الوصول لهذا الملف.", detail=str(exc)) from exc

        url = self.bot.session.api.file_url(self.bot.token, tg_file.file_path)
        target = dest_dir / sanitize_filename(ref.name, fallback="telegram-media")
        total = ref.size or tg_file.file_size or 0
        done = 0
        try:
            with open(target, "wb") as handle:
                async for chunk in self.bot.session.stream_content(url, chunk_size=256 * 1024):
                    handle.write(chunk)
                    done += len(chunk)
                    if progress and total:
                        await progress(min(99.0, done / total * 100),
                                       f"{human_size(done)} / {human_size(total)}")
        except OSError as exc:
            raise StorageError(detail=str(exc)) from exc

        if not target.is_file() or target.stat().st_size == 0:
            raise NoMediaFoundError("ملف تيليجرام المستلم فارغ.")
        size = target.stat().st_size
        if progress:
            await progress(100.0, human_size(size))
        log.info("saved telegram media %s (%d bytes) from %s", target.name, size, ref.source)
        return DownloadResult(files=[target], bytes_total=size, engine="telegram")


def _ext_from_mime(mime: str) -> str:
    table = {
        "video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-matroska": ".mkv",
        "video/webm": ".webm", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/ogg": ".ogg",
        "audio/opus": ".opus", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "image/gif": ".gif", "application/pdf": ".pdf", "application/zip": ".zip",
    }
    return table.get(mime.lower(), "")


__all__ = ["TelegramMediaRef", "TelegramMediaService", "ref_from_message",
           "BOT_API_FILE_LIMIT"]
