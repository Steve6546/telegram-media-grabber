"""Optional MTProto sender, and its honest limits.

Telethon talks to Telegram as a *user account*, not as a bot. That raises the upload
ceiling to 2 GB (4 GB with Premium) but brings two real constraints:

1. the session account must be able to see the recipient — i.e. it works for your own
   saved messages or for users who also message that account; it cannot broadcast to
   arbitrary bot users;
2. files appear as sent *by that account*, not by the bot.

Because of that this path is opt-in and the deployment report states plainly when it
was skipped. Nothing here pretends a bot token can do MTProto.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .errors import SenderError
from .extractors.base import MediaKind, human_size
from .senders import MTPROTO_PREMIUM_LIMIT, MTPROTO_SEND_LIMIT, SendOutcome, media_type_for

log = logging.getLogger("grabber.mtproto")


def telethon_available() -> bool:
    try:
        import telethon  # noqa: F401
    except ImportError:
        return False
    return True


class MtprotoSender:
    name = "mtproto"

    def __init__(self, settings) -> None:
        self.settings = settings
        self.client = None
        self.me: Any = None
        self.ceiling = MTPROTO_SEND_LIMIT

    async def connect(self) -> bool:
        if not (self.settings.mtproto_configured and telethon_available()):
            return False
        from telethon import TelegramClient

        session_path = self.settings.data_dir / self.settings.tg_session_name
        client = TelegramClient(str(session_path), self.settings.tg_api_id,
                                self.settings.tg_api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return False
        self.me = await client.get_me()
        # Premium accounts may upload up to 4 GB.
        if getattr(self.me, "premium", False):
            self.ceiling = MTPROTO_PREMIUM_LIMIT
        self.client = client
        log.info("MTProto session ready as %s (ceiling %s)",
                 getattr(self.me, "username", "?"), human_size(self.ceiling))
        return True

    async def close(self) -> None:
        if self.client:
            await self.client.disconnect()
            self.client = None

    async def probe(self) -> dict[str, Any]:
        return {
            "available": bool(self.client),
            "as": getattr(self.me, "username", None),
            "ceiling": self.ceiling,
            "premium": getattr(self.me, "premium", False),
        }

    async def send(self, chat_id: int, paths: list[Path], *, caption: str,
                   kind: MediaKind, force_document: bool = False,
                   duration: int | None = None, thumb: Path | None = None,
                   disable_notification: bool = False) -> SendOutcome:
        if not self.client:
            raise SenderError("جلسة MTProto غير موصولة.")
        total = sum(path.stat().st_size for path in paths)
        if total > self.ceiling:
            raise SenderError(
                f"مجموعة الملفات {human_size(total)} تتجاوز سقف MTProto {human_size(self.ceiling)}.",
                detail=str(paths),
            )
        ids: list[int] = []
        for index, path in enumerate(paths):
            file = _file_field(path, force_document, kind)
            sent = await self.client.send_file(
                chat_id, file,
                caption=(caption if index == 0 else None),
                force_document=force_document,
                supports_streaming=(not force_document and media_type_for(path, kind) == "video"),
                silent=disable_notification,
            )
            ids.append(sent.id)
        return SendOutcome(message_ids=ids, sender=self.name, bytes_sent=total)


def _file_field(path: Path, force_document: bool, kind: MediaKind) -> str:
    """Telethon accepts a local path directly; the server reads it from disk."""
    return str(path)


async def interactive_login(settings) -> bool:
    """Console login for the operator: phone, code, optional 2FA password."""
    if not telethon_available():
        print("telethon غير مثبّت:  pip install 'telegram-media-grabber[mtproto]'")
        return False
    from telethon import TelegramClient

    session_path = settings.data_dir / settings.tg_session_name
    async with TelegramClient(str(session_path), settings.tg_api_id, settings.tg_api_hash) as client:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"الجلسة موجودة مسبقاً باسم @{getattr(me, 'username', '?')}")
            return True
        await client.start()
        me = await client.get_me()
        print(f"تم تسجيل الدخول باسم @{getattr(me, 'username', me.id)}")
        return True


__all__ = ["MtprotoSender", "telethon_available", "interactive_login"]
