"""Startup capability report.

The bot's behaviour genuinely depends on what the host provides — whether FFmpeg exists
decides if merging, converting and splitting work; whether a Local Bot API Server is
reachable decides the send ceiling. So the report states the actual detected numbers and
lists what is degraded, instead of printing a happy-path banner.
"""

from __future__ import annotations

import sys
from typing import Any

from .config import Settings
from .extractors.base import human_size
from .senders import describe_limits

BOT_API_LIMIT_BYTES = 50 * 1024 * 1024


async def report(settings: Settings, *, check_token: bool = False) -> int:
    from .app import create_services, ensure_dirs
    from .platform_detector import coverage_report

    ensure_dirs(settings)
    services = await create_services(settings, connect_mtproto=False)
    status = services.service.status()
    coverage = coverage_report()

    lines: list[str] = ["telegram-media-grabber — تقرير البيئة", ""]
    lines.append(f"• Python            : {sys.version.split()[0]}")
    lines.append(f"• مجلد البيانات     : {settings.data_dir}")
    lines.append(f"• BOT_TOKEN         : {'مضبوط' if settings.bot_token else 'مفقود'}")

    lines.append("")
    lines.append("المحركات:")
    for engine in status["engines"]:
        extra = engine.get("extractors")
        aria = engine.get("aria2c")
        detail = f"{extra} مستخرج" if extra else ("قابل للاستئناف" if engine.get("resumable") else "")
        if aria is not None:
            detail += f" · aria2c: {'نعم' if aria else 'لا'}"
        lines.append(f"  - {engine['engine']:<11s} [{', '.join(engine['provides'])}] {detail}")
    lines.append(f"  • تغطية gallery-dl : {coverage.get('gallery_dl_extractors', 'غير متاح')} مستخرج صور")
    lines.append(f"  • جدول التوجيه     : {coverage.get('routing_table', 0)} منصة")

    lines.append("")
    lines.append("القدرات:")
    lines.append(f"  • FFmpeg           : {'متوفر — ' + status['processing']['version'] if status['processing']['ffmpeg'] else 'غير متوفر ⚠️ الدمج والتحويل والتقسيم معطّلان'}")
    lines.append(f"  • الكوكيز          : {'مشفّرة بـ Fernet' if status['cookies']['encrypted'] else 'نص صريح بصلاحية 600 ⚠️'}")
    lines.append(f"  • الطابور          : {status['queue']['max_concurrent']} متزامن، {settings.per_user_concurrent} لكل مستخدم")
    lines.append(f"  • حد المشروع       : {human_size(settings.max_file_bytes)} للملف")
    lines.append(f"  • سقف Bot API      : {human_size(BOT_API_LIMIT_BYTES)} إرسال · 20 MB تنزيل")
    lines.append(f"  • Local API Server : {'مضبوط على ' + settings.local_api_url if status['sender']['local_api'] else 'غير مضبوط'}")
    lines.append(f"  • MTProto          : {'جالس' if status['sender']['mtproto'] else 'غير مفعّل'}")
    lines.append(f"  • التقسيم           : {'متاح' if status['split']['possible'] else 'يحتاج FFmpeg'}")

    lines.append("")
    lines.append("مسارات الإرسال المحتملة:")
    for label, ceiling, note in describe_limits():
        lines.append(f"  - {label} ({human_size(ceiling)}): {note}")

    warnings = collect_warnings(settings, status)
    services.db.close()

    if warnings:
        lines.append("")
        lines.append("⚠️ ملاحظات تستحق الانتباه:")
        lines.extend(f"  - {warning}" for warning in warnings)
        print("\n".join(lines))
        return 1

    lines.append("")
    lines.append("✅ البيئة جاهزة للتشغيل.")
    print("\n".join(lines))
    return 0


def collect_warnings(settings: Settings, status: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if not settings.bot_token:
        warnings.append("BOT_TOKEN مفقود — التشغيل يحتاج توكن @BotFather.")
    if not status["processing"]["ffmpeg"]:
        warnings.append("FFmpeg مفقود — دمج الصوت بالفيديو وتحويل الصيغ وتقسيم الملفات الكبيرة معطّلة.")
    if not status["cookies"]["encrypted"] and settings.allow_cookie_auth:
        warnings.append("COOKIE_ENC_KEY مفقود — ملفات الكوكيز ستُحفظ نصاً بصلاحية 600 بدل التشفير.")
    if not settings.allowed_user_ids:
        warnings.append("ALLOWED_USER_IDS فارغ — أي شخص يستطيع استخدام البوت بلا قيود.")
    if settings.send_mode == "bot_api" and settings.max_file_mb * 1024 * 1024 > BOT_API_LIMIT_BYTES:
        warnings.append(
            "SEND_MODE=bot_api مع max_file_mb أكبر من 50: الملفات الأكبر ستُقسَّم أو تُرفض."
        )
    if not any(engine.get("extractors") for engine in status["engines"]):
        warnings.append("لم يُكتشف أي مستخرج في yt-dlp — قد تكون الحزمة غير مثبّتة بشكل صحيح.")
    return warnings


__all__ = ["report", "collect_warnings"]
