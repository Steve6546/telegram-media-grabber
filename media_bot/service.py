"""Orchestration: analyse → choose → queue → download → process → deliver → clean.

This is the only module that knows about all the others; handlers stay thin and engines
stay unaware of Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from .auth import CookieVault
from .bigfiles import split
from .db import Database, Job
from .errors import (
    AuthRequiredError,
    CancelledJobError,
    FileTooLargeError,
    GrabberError,
    NoMediaFoundError,
    SenderError,
    describe,
)
from .extractors.base import Bundle, DownloadResult, Variant, Want, human_duration, human_size
from .extractors.registry import Registry
from .mtproto import MtprotoSender
from .processing.ffmpeg import FFmpeg
from .processing.probe import probe
from .queue import DownloadQueue
from .senders import BOT_API_SEND_LIMIT, BotApiSender, LocalApiSender, SendOutcome
from .storage import StorageManager

log = logging.getLogger("grabber.service")

ProgressCallback = Callable[[float, str | None], Awaitable[None]]
#: parts stay under the 50 MB Bot API ceiling with margin for container overhead
BOT_API_SPLIT_CHUNK_MB = 45


@dataclass(slots=True)
class Delivery:
    outcome: SendOutcome
    info: dict[str, Any]
    parts: int = 1
    note: str | None = None


class GrabberService:
    def __init__(self, *, settings, db: Database, storage: StorageManager,
                 registry: Registry, queue: DownloadQueue, ffmpeg: FFmpeg,
                 vault: CookieVault, bot_sender: BotApiSender | None,
                 mtproto: MtprotoSender | None = None) -> None:
        self.settings = settings
        self.db = db
        self.storage = storage
        self.registry = registry
        self.queue = queue
        self.ffmpeg = ffmpeg
        self.vault = vault
        self.bot_sender = bot_sender
        self.local_sender = (
            LocalApiSender(bot_sender.bot)
            if bot_sender is not None and settings.send_mode in {"auto", "local_api"}
            else None
        )
        self.mtproto = mtproto
        #: a Local Bot API Server is only a route once it actually answers getMe
        self.local_api_ready = False
        self._bundles: dict[str, Bundle] = {}

    async def probe_senders(self) -> dict[str, object]:
        """Verify the optional send paths instead of assuming configuration means health."""
        result: dict[str, object] = {"local_api": None, "mtproto": None}
        if self.local_sender is not None:
            probe = await self.local_sender.probe()
            self.local_api_ready = bool(probe.get("available"))
            result["local_api"] = probe
        if self.mtproto is not None and self.mtproto.client is None:
            result["mtproto"] = {"available": await self.mtproto.connect()}
        return result

    def _require_sender(self) -> BotApiSender:
        if self.bot_sender is None:
            raise SenderError("BOT_TOKEN غير مضبوط: لا يمكن إرسال أي شيء لتيليجرام.")
        return self.bot_sender

    # ---------------- analysis ----------------

    async def analyze(self, url: str, *, user_id: int, want: Want = Want.AUTO,
                      use_cookies: bool = True) -> Bundle:
        cookies_path = None
        tmp_dir: Path | None = None
        if use_cookies and self.settings.allow_cookie_auth:
            tmp_dir = self.settings.cache_dir / f"auth-{secrets.token_hex(4)}"
            cookies_path = self.vault.materialize(user_id, tmp_dir)
        try:
            bundle = await self.registry.extract(url, want=want, cookies_path=cookies_path)
        finally:
            if tmp_dir and tmp_dir.exists():
                import shutil

                shutil.rmtree(tmp_dir, ignore_errors=True)
        bundle.token = secrets.token_hex(6)
        self._bundles[bundle.token] = bundle
        self.db.save_bundle(bundle.token, url=bundle.url, engine=bundle.engine,
                           payload=bundle.to_dict(), ttl_seconds=self.settings.dedupe_ttl_minutes * 60)
        cached = self.storage.lookup_cached(bundle.url, bundle.engine, "best")
        if cached:
            bundle.notes.append("هذا الرابط مخزّن مؤقتاً: الاختيار نفسه سينتهي فوراً بلا تنزيل جديد")
        log.info("analyzed %s -> %s/%s, %d variants", bundle.platform, bundle.engine,
                 bundle.kind.value, len(bundle.variants))
        return bundle

    def recall(self, token: str) -> Bundle | None:
        bundle = self._bundles.get(token)
        if bundle:
            return bundle
        payload = self.db.load_bundle(token)
        if not payload:
            return None
        bundle = Bundle.from_dict(payload)
        self._bundles[token] = bundle
        return bundle

    def pick_variant(self, bundle: Bundle, token: str) -> Variant | None:
        for variant in bundle.variants:
            if variant.token == token:
                return variant
        if bundle.items and token.startswith("i"):
            index = int(token[1:] or 0)
            if 0 <= index < len(bundle.items):
                return None
        return None

    # ---------------- download ----------------

    async def start(self, *, token: str, variant_token: str, user_id: int, chat_id: int,
                    progress: ProgressCallback | None = None) -> str:
        bundle = self.recall(token)
        if bundle is None:
            raise NoMediaFoundError("انتهت صلاحية هذه القائمة، أرسل الرابط من جديد.")
        variant = self.pick_variant(bundle, variant_token)

        if self.settings.max_downloads_per_user_per_hour:
            used = self.db.count_recent(user_id, window_seconds=3600)
            if used >= self.settings.max_downloads_per_user_per_hour:
                raise GrabberError(
                    f"وصلت إلى حد {self.settings.max_downloads_per_user_per_hour} طلباً في الساعة.",
                    detail=f"user={user_id} used={used}",
                )

        job = self.db.create_job(user_id=user_id, chat_id=chat_id, url=bundle.url,
                                 engine=bundle.engine, platform=bundle.platform,
                                 variant=variant_token)

        if variant is None and bundle.items:
            # a playlist/gallery entry selection: re-analyse that single item
            index = int(variant_token[1:] or 0) if variant_token.startswith("i") else 0
            item = bundle.items[min(index, len(bundle.items) - 1)]
            return await self._start_item(job, item, user_id, chat_id, progress=progress)

        handle = self.queue.submit(
            lambda report: self._run(job.id, bundle, variant, report, user_id=user_id, chat_id=chat_id),
            job_id=job.id, user_id=user_id,
            label=f"{bundle.platform} · {variant_token}",
            on_progress=progress,
        )
        return handle.job_id

    async def _start_item(self, job: Job, item, user_id: int, chat_id: int, *,
                          progress: ProgressCallback | None) -> str:
        self.db.update_job(job.id, status="running")
        sub_token = job.id
        try:
            child = await self.analyze(item.url, user_id=user_id)
        except GrabberError as exc:
            self.db.update_job(job.id, status="failed", error_code=describe(exc)[0],
                               error_detail=str(exc)[:400])
            raise
        handle = self.queue.submit(
            lambda report: self._run(job.id, child, child.variants[0] if child.variants else None,
                                     report, user_id=user_id, chat_id=chat_id),
            job_id=sub_token, user_id=user_id, label=f"{child.platform} · item",
            on_progress=progress,
        )
        return handle.job_id

    async def _run(self, job_id: str, bundle: Bundle, variant: Variant | None,
                   report: ProgressCallback, *, user_id: int, chat_id: int) -> Delivery:
        try:
            return await self._run_inner(job_id, bundle, variant, report,
                                        user_id=user_id, chat_id=chat_id)
        except asyncio.CancelledError:
            # /cancel must leave an honest row, not a job stuck in "running".
            self.db.update_job(job_id, status="cancelled", error_code="CANCELLED")
            raise
        except BaseException as exc:
            code, _message, _action = describe(exc)
            self.db.update_job(job_id, status="failed", error_code=code,
                               error_detail=str(exc)[:400])
            raise

    async def _run_inner(self, job_id: str, bundle: Bundle, variant: Variant | None,
                         report: ProgressCallback, *, user_id: int, chat_id: int) -> Delivery:
        dest = self.storage.job_dir(job_id)
        cookies_tmp: Path | None = None
        try:
            if self.settings.allow_cookie_auth:
                cookies_dir = dest / "_auth"
                cookies_tmp = self.vault.materialize(user_id, cookies_dir)
            self.db.update_job(job_id, status="running")

            async def chain(percent: float, text: str | None = None) -> None:
                await report(percent, text)

            result = await self.registry.download(bundle, variant, dest, progress=chain,
                                                 cookies_path=cookies_tmp)
            media = await probe(result.main_file, ffprobe_path=self.ffmpeg.ffprobe_path)
            result.info = media.label if not media.error else f"ffprobe: {media.error}"
            if media.error is None and variant and variant.height and media.height:
                if media.height != variant.height:
                    result.info += f" (المطلوب {variant.height}p)"
                    log.info("delivered %sp for a %sp request", media.height, variant.height)
            if variant and variant.ext and result.main_file.suffix.lstrip(".").lower() != variant.ext:
                log.info("engine produced %s, requested %s", result.main_file.suffix, variant.ext)

            if variant is None:
                variant = bundle.variants[0] if bundle.variants else None
            size = result.bytes_total
            if size > self.settings.max_file_bytes:
                raise FileTooLargeError(
                    f"الحجم {human_size(size)} يتجاوز حد المشروع "
                    f"({human_size(self.settings.max_file_bytes)}).",
                    detail=bundle.url,
                )
            self.db.update_job(job_id, status="uploading", file_path=str(result.main_file),
                               size_bytes=size, progress=99.0)
            outcome = await self.deliver(chat_id=chat_id, result=result, bundle=bundle,
                                        variant=variant, report=report, job_id=job_id)
            self.db.update_job(job_id, status="done", progress=100.0)
            self.storage.store_cached(bundle.url, bundle.engine,
                                      variant.token if variant else "best",
                                      job_id=job_id, file_path=result.main_file)
            return outcome
        finally:
            import shutil

            if cookies_tmp and cookies_tmp.parent.exists():
                shutil.rmtree(cookies_tmp.parent, ignore_errors=True)

    # ---------------- delivery ----------------

    def choose_route(self, size: int) -> tuple[str, str | None]:
        """Return (route, explanation). Never pretend a blocked path is available."""
        mode = self.settings.send_mode
        if mode == "bot_api":
            return ("bot_api", None) if size <= BOT_API_SEND_LIMIT else ("blocked", "bot_api only")
        if size <= BOT_API_SEND_LIMIT:
            return ("bot_api", None)
        if self.local_sender is not None and self.local_api_ready and size <= self.local_sender.ceiling:
            return ("local_api", None)
        if self.mtproto and getattr(self.mtproto, "client", None) and size <= self.mtproto.ceiling:
            return ("mtproto", None)
        if can_split_for(self.settings):
            return ("split", f"الملف {human_size(size)} أكبر من سقف Bot API "
                             f"({human_size(BOT_API_SEND_LIMIT)})")
        return ("blocked", "لا مسار متاح لهذه الحجم")

    async def deliver(self, *, chat_id: int, result: DownloadResult, bundle: Bundle,
                      variant: Variant | None, report: ProgressCallback,
                      job_id: str) -> Delivery:
        paths = result.files
        total = result.bytes_total
        main = result.main_file
        caption = build_caption(bundle, variant, result)
        kind = variant.kind if variant else bundle.kind

        route, reason = self.choose_route(total if len(paths) == 1 else main.stat().st_size)
        sender = self._require_sender()
        if route == "bot_api":
            outcome = await sender.send(chat_id, paths, caption=caption, kind=kind)
            await report(100.0, f"أُرسل عبر Bot API · {human_size(outcome.bytes_sent)}")
            return Delivery(outcome=outcome, info={}, note=reason)
        if route == "local_api" and self.local_sender is not None:
            outcome = await self.local_sender.send(chat_id, paths, caption=caption, kind=kind)
            await report(100.0, f"أُرسل عبر Local Bot API Server · {human_size(outcome.bytes_sent)}")
            return Delivery(outcome=outcome, info={}, note="local bot api server")
        if route == "mtproto" and self.mtproto is not None:
            outcome = await self.mtproto.send(chat_id, paths, caption=caption, kind=kind)
            await report(100.0, f"أُرسل عبر MTProto · {human_size(outcome.bytes_sent)}")
            return Delivery(outcome=outcome, info={}, note="mtproto session")
        if route == "split" and reason:
            # Keep each part comfortably under the 50 MB Bot API ceiling.
            parts = await split(main, ffmpeg=self.ffmpeg, chunk_mb=BOT_API_SPLIT_CHUNK_MB)
            outcome = await sender.send(chat_id, parts, caption=caption,
                                        kind=kind, force_document=True)
            await report(100.0, f"{len(parts)} مقطعاً · {human_size(outcome.bytes_sent)}")
            return Delivery(outcome=outcome, info={"reason": reason}, parts=len(parts),
                            note=f"قُسم لأنه {reason}")
        raise FileTooLargeError(
            f"حجم الملف {human_size(total)} لا يمكن إرساله بالإعدادات الحالية.",
            detail=f"route={route}",
        )

    # ---------------- telegram-side media ----------------

    async def take_telegram_media(self, ref, *, user_id: int, chat_id: int,
                                 progress: ProgressCallback | None = None) -> Delivery:
        from .telegram_media import TelegramMediaService

        job = self.db.create_job(user_id=user_id, chat_id=chat_id, url=f"tg:{ref.file_id[:12]}",
                                engine="telegram", platform="Telegram", variant="orig")
        service = TelegramMediaService(self._require_sender().bot, self.settings)
        dest = self.storage.job_dir(job.id)
        self.db.update_job(job.id, status="running")

        async def report(percent: float, text: str | None = None) -> None:
            self.db.update_job(job.id, progress=percent)
            if progress:
                await progress(percent, text)

        result = await service.download(ref, dest, progress=report)
        self.db.update_job(job.id, status="uploading", file_path=str(result.main_file),
                           size_bytes=result.bytes_total)
        bundle = self.new_echo_bundle(ref, job.id)
        outcome = await self.deliver(chat_id=chat_id, result=result, bundle=bundle,
                                    variant=bundle.variants[0], report=report, job_id=job.id)
        self.db.update_job(job.id, status="done", progress=100.0)
        return outcome

    def new_echo_bundle(self, ref, job_id: str) -> Bundle:
        bundle = Bundle(engine="telegram", platform="Telegram", url=f"tg:{job_id}",
                        kind=ref.kind, token=job_id, title=ref.name,
                        duration=ref.duration, count=1)
        bundle.variants = [Variant(token="orig", label="كما هو", kind=ref.kind,
                                  format_spec="telegram", ext=Path(ref.name).suffix.lstrip(".") or "bin",
                                  filesize=ref.size)]
        return bundle

    # ---------------- lifecycle ----------------

    async def cancel(self, job_id: str) -> bool:
        handle = await self.queue.cancel(job_id)
        if handle is None:
            return False
        self.db.update_job(job_id, status="cancelled", error_code="CANCELLED")
        self.storage.delete_job_files(job_id)
        return True

    async def cancel_mine(self, user_id: int) -> list[str]:
        ids = await self.queue.cancel_for_user(user_id)
        for job_id in ids:
            self.db.update_job(job_id, status="cancelled", error_code="CANCELLED")
            self.storage.delete_job_files(job_id)
        return ids

    async def forget(self, job_id: str) -> bool:
        return self.storage.delete_job_files(job_id)

    async def maintenance(self) -> dict[str, int]:
        return self.storage.purge_expired()

    def status(self) -> dict[str, Any]:
        return {
            "queue": self.queue.stats(),
            "engines": self.registry.status(),
            "sender": {
                "mode": self.settings.send_mode,
                "bot_api_ceiling": human_size(BOT_API_SEND_LIMIT),
                "configured": self.bot_sender is not None,
                "local_api": bool(self.local_sender and self.local_api_ready),
                "mtproto": bool(self.mtproto and getattr(self.mtproto, "client", None)),
            },
            "processing": {"ffmpeg": self.ffmpeg.available, "version": self.ffmpeg.version()},
            "split": {"possible": self.ffmpeg.available},
            "cookies": {"enabled": self.settings.allow_cookie_auth,
                        "encrypted": self.vault.encrypted},
            "storage": {"bytes": human_size(self.storage.used_bytes())},
        }


def can_split_for(settings) -> bool:
    return settings.max_file_mb > 50


def build_caption(bundle: Bundle, variant: Variant | None, result: DownloadResult | None = None) -> str:
    lines: list[str] = []
    title = (bundle.title or "وسائط")[:90]
    lines.append(f"🎬 {title}")
    if bundle.platform:
        lines.append(f"المصدر: {bundle.platform}")
    if variant:
        lines.append(f"الاختيار: {variant.label}")
    if bundle.duration:
        lines.append(f"المدة: {human_duration(bundle.duration)}")
    if bundle.count > 1:
        lines.append(f"عدد الملفات: {bundle.count}")
    if result:
        size = result.bytes_total
        lines.append(f"الحجم: {human_size(size)}")
        info = result.info if hasattr(result, "info") else None
        if info:
            lines.append(f"الملف الفعلية: {info}")
        if result.merged:
            lines.append("دُمج الصوت بالفيديو تلقائياً")
    if bundle.uploader:
        lines.append(f"القناة: {bundle.uploader[:40]}")
    if bundle.notes:
        lines.append("· " + " · ".join(bundle.notes[:2]))
    return "\n".join(lines)


__all__ = ["GrabberService", "Delivery", "build_caption", "can_split_for", "CancelledJobError",
           "AuthRequiredError"]
