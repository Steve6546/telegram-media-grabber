"""Application wiring: build every component once and connect them.

Keeping this in one place is what lets the tests build the same graph with fakes, and
lets ``doctor`` report exactly which optional part of the graph is missing.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .auth import CookieVault
from .bot import Services, build_dispatcher
from .config import Settings
from .db import Database
from .extractors.registry import Registry, build_registry
from .logging_setup import setup_logging
from .mtproto import MtprotoSender, telethon_available
from .processing.ffmpeg import FFmpeg
from .queue import DownloadQueue
from .senders import BotApiSender, build_bot
from .service import GrabberService
from .storage import StorageManager

log = logging.getLogger("grabber.app")


async def create_services(settings: Settings, *, connect_mtproto: bool = True) -> Services:
    setup_logging(settings.data_dir, settings.extra.get("log_level", "INFO"))
    db = Database(settings.db_path)
    storage = StorageManager(settings, db)
    registry: Registry = build_registry(settings)
    queue = DownloadQueue(max_concurrent=settings.max_concurrent_downloads,
                          per_user=settings.per_user_concurrent)
    ffmpeg = FFmpeg(ffmpeg_path=settings.ffmpeg_path, ffprobe_path=settings.ffprobe_path)
    vault = CookieVault(settings, db)

    bot = build_bot(settings) if settings.bot_token else None
    sender = BotApiSender(bot) if bot is not None else None
    mtproto = MtprotoSender(settings) if settings.mtproto_configured else None
    if mtproto and connect_mtproto:
        connected = await mtproto.connect()
        if not connected:
            log.warning("MTProto session unavailable; the bot stays on the Bot API path")
            mtproto = None

    service = GrabberService(settings=settings, db=db, storage=storage, registry=registry,
                             queue=queue, ffmpeg=ffmpeg, vault=vault, bot_sender=sender,
                             mtproto=mtproto)
    services = Services(settings=settings, db=db, storage=storage, queue=queue, ffmpeg=ffmpeg,
                        vault=vault, service=service, bot=bot)
    if bot is None:
        log.warning("BOT_TOKEN غير مضبوط: يتم بناء المكوّنات للتشخيص فقط")
    elif settings.send_mode in {"auto", "local_api"}:
        # Only meaningful with a working token: it asks the local server for getMe.
        probed = await service.probe_senders()
        log.info("sender probe: %s", probed)
    log.info("wired: %d engines, ffmpeg=%s, mtproto=%s", len(registry.engines),
             ffmpeg.available, bool(mtproto))
    return services


async def maintenance_loop(services: Services, interval_seconds: int = 900) -> None:
    """Periodic disk hygiene so long-running deploys do not fill the volume."""
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            stats = await asyncio.to_thread(services.service.maintenance)
            if stats.get("dirs_removed"):
                log.info("maintenance: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - maintenance must never kill the bot
            log.warning("maintenance failed: %s", exc)


async def run_bot(settings: Settings) -> None:
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN مفقود: لا يمكن بدء البوت بدون توكن @BotFather.")
    services = await create_services(settings)
    dispatcher = build_dispatcher(services)
    task = asyncio.create_task(maintenance_loop(services))
    try:
        await dispatcher.start_polling(services.bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        task.cancel()
        await services.queue.shutdown()
        services.db.close()


def ensure_dirs(settings: Settings) -> list[Path]:
    created = []
    for path in (settings.data_dir, settings.downloads_dir, settings.cache_dir, settings.cookies_dir):
        path.mkdir(parents=True, exist_ok=True)
        created.append(path)
    return created


__all__ = ["create_services", "run_bot", "maintenance_loop", "ensure_dirs", "telethon_available"]
