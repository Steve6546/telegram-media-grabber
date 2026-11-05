"""aiogram handlers: the whole user contract lives here.

Flow per requirement: link → analyse → platform + title + thumbnail → real qualities as
buttons → download with progress → deliver (or explain exactly what blocked it).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    URLInputFile,
)

from ..auth import CookieVault
from ..config import Settings
from ..db import Database
from ..errors import AuthRequiredError, GrabberError, describe
from ..extractors.base import Bundle, Want, human_size
from ..processing.ffmpeg import FFmpeg
from ..queue import DownloadQueue
from ..security import check_url
from ..service import GrabberService
from ..storage import StorageManager
from ..telegram_media import ref_from_message
from .keyboards import (
    CB_CANCEL,
    CB_HELP,
    CB_ITEM,
    CB_MENU,
    CB_TASKS,
    CB_VARIANT,
    analysis_text,
    busy_keyboard,
    cookies_keyboard,
    coverage_text,
    help_keyboard,
    items_keyboard,
    main_menu,
    variants_keyboard,
)

log = logging.getLogger("grabber.bot")

URL_RE = re.compile(r"https?://[^\s<>'\"]+", re.I)
PROGRESS_MIN_INTERVAL = 2.5
#: a document counts as a cookie upload only while the user has asked for that
_pending_cookie: dict[int, float] = {}
COOKIE_WINDOW_SECONDS = 180


def _mark_cookie_expected(user_id: int) -> None:
    _pending_cookie[user_id] = time.time()


def _cookie_expected(user_id: int) -> bool:
    stamp = _pending_cookie.get(user_id)
    if stamp is None:
        return False
    if time.time() - stamp > COOKIE_WINDOW_SECONDS:
        _pending_cookie.pop(user_id, None)
        return False
    return True


@dataclass(slots=True)
class Services:
    settings: Settings
    db: Database
    storage: StorageManager
    queue: DownloadQueue
    ffmpeg: FFmpeg
    vault: CookieVault
    service: GrabberService
    bot: Bot | None = None


def extract_url(text: str) -> str | None:
    match = URL_RE.search(text or "")
    return match.group(0) if match else None


def build_dispatcher(services: Services) -> Dispatcher:
    router = Router()
    _register(router, services)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    dispatcher.startup.register(_on_startup(services))
    dispatcher.shutdown.register(_on_shutdown(services))
    return dispatcher


def _on_startup(services: Services):
    async def startup() -> None:
        log.info("bot started: %s", (await services.bot.get_me()).username)
    return startup


def _on_shutdown(services: Services):
    async def shutdown() -> None:
        await services.queue.shutdown()
        services.db.close()
    return shutdown


def _allow(message: Message, settings: Settings) -> bool:
    if not settings.allowed_user_ids:
        return True
    return message.from_user.id in settings.allowed_user_ids


def _register(router: Router, services: Services) -> None:  # noqa: C901 - one wiring function
    settings = services.settings
    service = services.service

    async def _relay_media(message: Message) -> None:
        """Send back the Telegram media the user already has access to."""
        if not _allow(message, settings):
            return
        ref = ref_from_message(message)
        if ref is None:
            await message.answer("🤔 لم أتعرف على الوسائط في هذه الرسالة.")
            return
        status = await message.answer(f"📥 أستلم {ref.describe()} وأعيد إرساله لك...")
        try:
            delivery = await service.take_telegram_media(
                ref, user_id=message.from_user.id, chat_id=message.chat.id
            )
            await status.delete()
            if delivery.note:
                await message.answer(f"ℹ️ {delivery.note}")
        except GrabberError as exc:
            code, text, action = describe(exc)
            await status.edit_text(f"❌ [{code}] {text}\n💡 {action}")
        except Exception as exc:
            log.exception("telegram media relay failed")
            code, text, action = describe(exc)
            await status.edit_text(f"❌ [{code}] {text}\n💡 {action}")

    # ---------------- commands ----------------

    @router.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        if not _allow(message, settings):
            await message.answer("هذا البوت مقفل على قائمة مستخدمين محددة.")
            return
        name = message.from_user.first_name or ""
        await message.answer(
            f"👋 مرحباً {name}.\n\n"
            "أرسل رابط فيديو أو صورة أو ألبوم أو ملف، وسأحلله وأعطيك الجودات الموجودة فعلاً.\n\n"
            "أوامر: /help · /tasks · /cancel · /cookies · /cookies_clear · /status\n"
            "يمكنك أيضاً إعادة توجيه وسائط من محادثاتك إليّ لتستلمها هنا.",
            reply_markup=main_menu(),
        )

    @router.message(Command("help"))
    async def cmd_help(message: Message) -> None:
        await message.answer(
            "📚 كيف أعمل\n\n"
            "1. أرسل الرابط (نص عادي يكفي، لا حاجة لأمر خاص).\n"
            "2. أحلله وأعرض المنصة والعنوان والصورة المصغرة.\n"
            "3. أضع أزرار للجودات التي توجد في المصدر فعلاً، مع الحجم المتوقع.\n"
            "4. أنزّل مع نسبة تقدم، ثم أرسلك الملف.\n\n"
            "أنواع الوسائط: فيديو · صوت · صور · ألبومات · ملفات مباشرة · بث مسجل.\n"
            "المحتوى الخاص: ارفع كوكيز بحسابك بـ /cookies، وسأستخدمها لما تملك صلاحية رؤيته فقط.\n"
            "المحتوى المحمي بـ DRM لا أتعامل معه.\n\n"
            "الأوامر:\n"
            "/tasks — المهام الجارية\n/cancel — إلغاء مهمة أو كلها\n/status — حالة المحركات\n"
            "/cookies — رفع ملف كوكيز\n/cookies_clear — حذفه\n/start — القائمة\n"
            f"\n⚠️ سقف Bot API للإرسال {human_size(50 * 1024 * 1024)}؛ للملفات الأكبر "
            "نحتاج Local Bot API Server أو MTProto أو تقسيم الملف.\n\n"
            + coverage_text(),
            reply_markup=help_keyboard(),
        )

    @router.message(Command("status"))
    async def cmd_status(message: Message) -> None:
        status = service.status()
        lines = ["🧪 حالة النظام", ""]
        for engine in status["engines"]:
            lines.append(
                f"• {engine['engine']}: "
                f"{engine.get('extractors') or engine.get('resumable') or ''} "
                f"({', '.join(engine['provides'])})"
            )
        lines += [
            "",
            f"FFmpeg: {'متوفر · ' + status['processing']['version'] if status['processing']['ffmpeg'] else 'غير متوفر'}",
            f"مسار الإرسال: {status['sender']['mode']} · سقف Bot API {status['sender']['bot_api_ceiling']}",
            f"Local Bot API Server: {'مضبوط' if status['sender']['local_api'] else 'غير مضبوط'}",
            f"MTProto: {'جالس' if status['sender']['mtproto'] else 'غير مفعّل'}",
            f"التقسيم للملفات الكبيرة: {'متاح' if status['split']['possible'] else 'يحتاج FFmpeg'}",
            f"الكوكيز: {'مشفّرة' if status['cookies']['encrypted'] else 'نص صريح بصلاحية 600'}",
            f"الطابور: {status['queue']['active']} نشِط من {status['queue']['max_concurrent']}",
            f"مساحة مستخدمة: {status['storage']['bytes']}",
        ]
        await message.answer("\n".join(lines))

    @router.message(Command("tasks"))
    async def cmd_tasks(message: Message) -> None:
        handles = services.queue.user_active(message.from_user.id)
        jobs = services.db.user_jobs(message.from_user.id, active_only=False, limit=8)
        if not handles:
            lines = ["✅ لا مهام جارية."]
            if jobs:
                lines.append("\nآخر المهام:")
                for job in jobs:
                    lines.append(f"• {job.platform} · {job.variant} · {job.status}")
            await message.answer("\n".join(lines))
            return
        lines = ["🗂 المهام الجارية:"]
        for handle in handles:
            bar = _bar(handle.progress)
            lines.append(f"• {handle.label} {bar} {handle.progress:.0f}% {handle.detail or ''}")
        await message.answer("\n".join(lines), reply_markup=busy_keyboard(handles[0].job_id))

    @router.message(Command("cancel"))
    async def cmd_cancel(message: Message, command: CommandObject) -> None:
        if command.args:
            done = await service.cancel(command.args.strip())
            await message.answer("✅ أُلغيت المهمة." if done else "ℹ️ لا توجد مهمة بهذا المعرّف.")
            return
        ids = await service.cancel_mine(message.from_user.id)
        await message.answer(
            f"✅ أُلغيت {len(ids)} مهمة." if ids else "ℹ️ لا مهام جارية لإلغائها."
        )

    @router.message(Command("cookies"))
    async def cmd_cookies(message: Message) -> None:
        if not settings.allow_cookie_auth:
            await message.answer("ℹ️ إدارة الكوكيز معطّلة في هذا النشر (ALLOW_COOKIE_AUTH=false).")
            return
        status = service.vault.status(message.from_user.id)
        hint = (
            "أرسل الآن **ملف كوكيز** بصيغة Netscape (يبدأ الأسطر بـ # Netscape HTTP Cookie File) "
            "كرسالة مستند، وسأخزّنه لحسابك.\n\n"
            "كيف تحصل عليه: إضافة Get cookies.txt / cookies.txt في المتصفح، وأنت مسجّل الدخول في الموقع.\n"
            "الكوكيز تفتح لك المحتوى الذي ترى أنت — لا محتوى الآخرين. اكتب /cookies_clear للحذف.\n"
        )
        text = (hint + (f"\nالمخزّن حالياً: {status['entries']} كوكيز لنطاقات: "
                        f"{', '.join(status['domains'][:8])} · {service.vault.age_text(message.from_user.id)}")
                if status else hint)
        _mark_cookie_expected(message.from_user.id)
        await message.answer(
            text + "\n\n📎 أرسل الملف الآن (الطلب صالٍ 3 دقائق).",
            reply_markup=cookies_keyboard(status),
        )

    @router.message(Command("cookies_clear"))
    async def cmd_cookies_clear(message: Message) -> None:
        removed = service.vault.drop(message.from_user.id)
        await message.answer("✅ حُذفت الكوكيز." if removed else "ℹ️ لا يوجد شيء لحذفه.")

    # ---------------- cookie upload ----------------

    @router.message(F.document)
    async def on_document(message: Message) -> None:
        if not message.document:
            return
        user_id = message.from_user.id
        if not _cookie_expected(user_id):
            # Not a cookie drop: fall through to the media relay path.
            await _relay_media(message)
            return
        name = message.document.file_name or ""
        mime = message.document.mime_type or ""
        if not (name.lower().endswith((".txt", ".cookies")) or mime.startswith("text/")):
            await message.answer("📄 أحتاج ملف كوكيز نصي (.txt) بصيغة Netscape.")
            return
        if message.document.file_size and message.document.file_size > 512 * 1024:
            await message.answer("⚠️ الملف أكبر من 512 KB — يبدو غير مناسب.")
            return
        try:
            file = await services.bot.get_file(message.document.file_id)
            dest = settings.cache_dir / f"upload-{user_id}.txt"
            await services.bot.download(file, destination=str(dest))
            info = service.vault.store(user_id, dest.read_text(encoding="utf-8", errors="replace").encode())
            dest.unlink(missing_ok=True)
            _pending_cookie.pop(user_id, None)
            encryption = "مشفّرة بـ Fernet" if info["encrypted"] else "بصلاحية 600 بلا تشفير"
            await message.answer(
                f"✅ حُفظت {info['entries']} كوكيز ({encryption}).\n"
                f"النطاقات: {', '.join(info['domains'][:10])}\n"
                "جرّب رابط المحتوى الخاص الآن."
            )
        except GrabberError as exc:
            _pending_cookie.pop(user_id, None)
            code, text, action = describe(exc)
            await message.answer(f"❌ [{code}] هذا ليس ملف كوكيز صالح.\n{text}\n💡 {action}")
        except Exception as exc:
            log.exception("cookie store failed")
            code, text, action = describe(exc)
            await message.answer(f"❌ [{code}] {text}\n💡 {action}")

    # ---------------- links ----------------

    @router.message(F.text)
    async def on_text(message: Message) -> None:
        if not _allow(message, settings):
            await message.answer("هذا البوت مقفل على قائمة مستخدمين محددة.")
            return
        raw = extract_url(message.text or "")
        if not raw:
            await message.answer(
                "🤔 لم أجد رابطاً في رسالتك. أرسل رابطاً مثل:\n"
                "https://www.youtube.com/watch?v=...\nأو أعد توجيه وسائط من محادثتك.",
                reply_markup=main_menu(),
            )
            return
        try:
            safe = check_url(raw)
        except GrabberError as exc:
            code, text, action = describe(exc)
            await message.answer(f"❌ [{code}] {text}\n💡 {action}")
            return

        want = _guess_want(message.text or "")
        status = await message.answer("🔍 أحلّل الرابط...")
        try:
            bundle = await service.analyze(safe, user_id=message.from_user.id, want=want)
        except AuthRequiredError as exc:
            await status.edit_text(
                f"🔒 هذا المحتوى يحتاج دخولك.\n{exc}\n\n💡 ارفع كوكيز بـ /cookies ثم أعد إرسال الرابط."
            )
            return
        except GrabberError as exc:
            code, text, action = describe(exc)
            await status.edit_text(f"❌ [{code}] {text}\n💡 {action}")
            return
        except Exception as exc:
            log.exception("analysis failed for %s", safe)
            code, text, action = describe(exc)
            await status.edit_text(f"❌ [{code}] {text}\n💡 {action}")
            return

        await status.delete()
        markup = items_keyboard(bundle) if (bundle.is_multi and not bundle.variants) else variants_keyboard(bundle)
        text = analysis_text(bundle)
        await _send_analysis(message, bundle, text, markup)

    # ---------------- callbacks ----------------

    @router.callback_query(F.data == f"{CB_MENU}:main")
    async def cb_menu(callback: CallbackQuery) -> None:
        await callback.message.edit_text("🏠 أرسل رابطاً للبدء.", reply_markup=main_menu())
        await callback.answer()

    @router.callback_query(F.data == f"{CB_MENU}:status")
    async def cb_status(callback: CallbackQuery) -> None:
        await callback.message.edit_text(coverage_text() + "\n\n" + _engine_line(service))
        await callback.answer()

    @router.callback_query(F.data == f"{CB_MENU}:cookies")
    async def cb_cookies(callback: CallbackQuery) -> None:
        await callback.message.edit_text("استخدم /cookies لرفع ملف جديد، أو /cookies_clear للحذف.")
        await callback.answer()

    @router.callback_query(F.data == f"{CB_MENU}:cookies_clear")
    async def cb_cookies_clear(callback: CallbackQuery) -> None:
        removed = service.vault.drop(callback.from_user.id)
        await callback.message.edit_text("✅ حُذفت الكوكيز." if removed else "ℹ️ لا شيء لحذفه.")
        await callback.answer()

    @router.callback_query(F.data == CB_HELP)
    async def cb_help(callback: CallbackQuery) -> None:
        await cmd_help(callback.message)
        await callback.answer()

    @router.callback_query(F.data == CB_TASKS)
    async def cb_tasks(callback: CallbackQuery) -> None:
        await cmd_tasks(callback.message)
        await callback.answer()

    @router.callback_query(F.data.startswith(f"{CB_CANCEL}:"))
    async def cb_cancel(callback: CallbackQuery) -> None:
        job_id = callback.data.split(":", 1)[1]
        if job_id == "all":
            ids = await service.cancel_mine(callback.from_user.id)
            await callback.answer(f"أُلغيت {len(ids)} مهمة" if ids else "لا مهام جارية")
            return
        done = await service.cancel(job_id)
        await callback.answer("أُلغيت المهمة" if done else "المهمة انتهت بالفعل")
        if done:
            await _safe_edit(callback.message, "✖️ أُلغيت المهمة.")

    @router.callback_query(F.data.startswith(f"{CB_ITEM}:"))
    async def cb_item(callback: CallbackQuery) -> None:
        _, token, index = (callback.data.split(":", 2) + ["", "0"])[:3]
        bundle = service.recall(token)
        if not bundle or not bundle.items:
            await callback.answer("انتهت صلاحية القائمة", show_alert=True)
            return
        try:
            child = await service.analyze(bundle.items[int(index)].url, user_id=callback.from_user.id)
        except GrabberError as exc:
            code, text, action = describe(exc)
            await callback.answer(f"[{code}] {text}", show_alert=True)
            return
        await _safe_edit(callback.message, analysis_text(child), variants_keyboard(child))
        await callback.answer()

    @router.callback_query(F.data.startswith(f"{CB_VARIANT}:"))
    async def cb_variant(callback: CallbackQuery) -> None:
        parts = callback.data.split(":")
        if len(parts) != 3:
            await callback.answer("زر غير معروف", show_alert=True)
            return
        _, token, variant_token = parts
        bundle = service.recall(token)
        if bundle is None:
            await callback.answer("انتهت صلاحية هذه القائمة — أرسل الرابط من جديد", show_alert=True)
            return
        variant = service.pick_variant(bundle, variant_token)
        preview = (
            f"يبدأ التنزيل: {variant.label}" if variant
            else f"عناصر القائمة: {variant_token}"
        ) + "\n" + (
            f"الحجم المتوقع: {human_size(variant.filesize)}"
            if variant and variant.filesize else "الحجم يظهر أثناء التنزيل"
        )
        holding = await callback.message.answer(f"⏳ {preview}")
        await callback.answer()

        # The status message is created before the job id exists, so the cancel button is
        # patched in as soon as the real id arrives — otherwise it would point at nothing.
        last = {"at": 0.0}

        async def report(percent: float, text: str | None) -> None:
            now = time.monotonic()
            if now - last["at"] < PROGRESS_MIN_INTERVAL and percent < 100:
                return
            last["at"] = now
            await _safe_edit(holding, f"⏳ {percent:.0f}% · {text or ''}")

        try:
            job_id = await service.start(token=token, variant_token=variant_token,
                                        user_id=callback.from_user.id,
                                        chat_id=callback.message.chat.id, progress=report)
            await _safe_edit(holding, f"🆔 المهمة {job_id}", busy_keyboard(job_id))
        except GrabberError as exc:
            code, text, action = describe(exc)
            await _safe_edit(holding, f"❌ [{code}] {text}\n💡 {action}")
            return
        except Exception as exc:
            log.exception("download start failed")
            code, text, action = describe(exc)
            await _safe_edit(holding, f"❌ [{code}] {text}\n💡 {action}")
            return

        handle = services.queue.get(job_id)
        if handle and handle.task:
            try:
                delivery = await handle.task
            except asyncio.CancelledError:
                await _safe_edit(holding, "✖️ أُلغيت المهمة.")
                return
            except Exception as exc:
                log.info("job %s failed: %s", job_id, exc)
                code, text, action = describe(exc)
                await _safe_edit(holding, f"❌ [{code}] {text}\n💡 {action}")
                return
            try:
                await holding.delete()
            except TelegramAPIError:
                pass
            if delivery and delivery.note:
                await callback.message.answer(f"ℹ️ {delivery.note}")

    # ---------------- media sent to the bot ----------------

    @router.message(F.video | F.document | F.audio | F.voice | F.animation | F.photo | F.video_note)
    async def on_media(message: Message) -> None:
        await _relay_media(message)

    @router.message()
    async def on_other(message: Message) -> None:
        await message.answer("🤖 أرسل رابط وسائط أو إعادة توجيه لملف، أو /help.")


async def _send_analysis(message: Message, bundle: Bundle, text: str,
                         markup: InlineKeyboardMarkup) -> None:
    """Show the thumbnail when Telegram can fetch it, otherwise plain text."""
    if bundle.thumbnail:
        try:
            await message.answer_photo(URLInputFile(url=bundle.thumbnail, timeout=20),
                                       caption=text, reply_markup=markup)
            return
        except TelegramAPIError as exc:
            log.info("thumbnail fetch refused: %s", exc)
    await message.answer(text, reply_markup=markup)


async def _safe_edit(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramAPIError:
        pass


def _bar(percent: float) -> str:
    filled = int(round(percent / 10))
    return "▰" * filled + "▱" * (10 - filled)


def _engine_line(service: GrabberService) -> str:
    status = service.status()
    return "\n".join(f"• {row['engine']}: {', '.join(row['provides'])}" for row in status["engines"])


def _guess_want(text: str) -> Want:
    lowered = (text or "").lower()
    if any(word in lowered for word in ("صوت", "audio", "mp3", "أغنية", "اغنيه")):
        return Want.AUDIO
    if any(word in lowered for word in ("صورة", "صور", "ألبوم", "album", "photo", "image")):
        return Want.IMAGE
    return Want.AUTO


__all__ = ["Services", "build_dispatcher", "extract_url"]
