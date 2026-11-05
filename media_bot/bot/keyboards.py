"""Inline keyboards built only from capabilities that actually exist.

Every button here maps to a code path that runs. That is the point: the earlier
generation of these bots shipped menus whose buttons replied "قيد التطوير", which is a
worse user experience than not showing them.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ..extractors.base import Bundle, MediaKind, Variant
from ..platform_detector import coverage_report

#: callback_data is limited to 64 bytes by Telegram, so payloads stay ASCII and short
CB_VARIANT = "q"
CB_CANCEL = "c"
CB_TASKS = "t"
CB_MENU = "m"
CB_HELP = "h"
CB_ITEM = "i"
CB_AUDIO = "a"
CB_RENAME = "r"

MAX_BUTTONS_PER_ROW = 2


def _data(*parts: str) -> str:
    payload = ":".join(parts)
    if len(payload.encode("utf-8")) > 64:
        raise ValueError(f"callback_data too long for Telegram: {payload!r}")
    return payload


def main_menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📥 أرسل رابطاً وسأحلله", callback_data=_data(CB_HELP))],
        [InlineKeyboardButton(text="🗂 مهامي", callback_data=_data(CB_TASKS)),
         InlineKeyboardButton(text="⏱ إلغاء كل المهام", callback_data=_data(CB_CANCEL, "all"))],
        [InlineKeyboardButton(text="🍪 إدارة الكوكيز", callback_data=_data(CB_MENU, "cookies")),
         InlineKeyboardButton(text="ℹ️ حالة المحركات", callback_data=_data(CB_MENU, "status"))],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def variants_keyboard(bundle: Bundle) -> InlineKeyboardMarkup:
    """One button per real quality, plus audio-only when audio exists."""
    rows: list[list[InlineKeyboardButton]] = []
    video = [v for v in bundle.variants if v.kind in (MediaKind.VIDEO, MediaKind.LIVE)]
    audio = [v for v in bundle.variants if v.kind is MediaKind.AUDIO]
    still = [v for v in bundle.variants if v.kind is MediaKind.IMAGE]

    for index in range(0, len(video), MAX_BUTTONS_PER_ROW):
        rows.append([
            InlineKeyboardButton(text=_variant_text(variant),
                                 callback_data=_data(CB_VARIANT, bundle.token, variant.token))
            for variant in video[index:index + MAX_BUTTONS_PER_ROW]
        ])
    for index in range(0, len(audio), MAX_BUTTONS_PER_ROW):
        rows.append([
            InlineKeyboardButton(text=_variant_text(variant),
                                 callback_data=_data(CB_VARIANT, bundle.token, variant.token))
            for variant in audio[index:index + MAX_BUTTONS_PER_ROW]
        ])
    for index in range(0, len(still), MAX_BUTTONS_PER_ROW):
        rows.append([
            InlineKeyboardButton(text=_variant_text(variant),
                                 callback_data=_data(CB_VARIANT, bundle.token, variant.token))
            for variant in still[index:index + MAX_BUTTONS_PER_ROW]
        ])
    rows.append([InlineKeyboardButton(text="↩️ رجوع", callback_data=_data(CB_MENU, "main"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _variant_text(variant: Variant) -> str:
    parts = [variant.label]
    if variant.filesize:
        from ..extractors.base import human_size

        parts.append(human_size(variant.filesize))
    if variant.merge:
        parts.append("دمج")
    text = " · ".join(parts)
    return text[:60]


def items_keyboard(bundle: Bundle) -> InlineKeyboardMarkup:
    """Playlist / multi-item picker."""
    rows: list[list[InlineKeyboardButton]] = []
    visible = bundle.items[:25]
    for start in range(0, len(visible), 5):
        rows.append([
            InlineKeyboardButton(
                text=f"{index + 1}. {(visible[index].name or 'عنصر')[:44]}",
                callback_data=_data(CB_ITEM, bundle.token, str(index)),
            )
            for index in range(start, min(start + 5, len(visible)))
        ])
    rows.append([InlineKeyboardButton(text="↩️ رجوع", callback_data=_data(CB_MENU, "main"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def busy_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✖️ إلغاء هذه المهمة", callback_data=_data(CB_CANCEL, job_id))
    ]])


def help_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 القائمة", callback_data=_data(CB_MENU, "main"))],
    ])


def cookies_keyboard(status: dict | None) -> InlineKeyboardMarkup:
    rows = []
    if status:
        rows.append([InlineKeyboardButton(text="🗑 حذف الكوكيز", callback_data=_data(CB_MENU, "cookies_clear"))])
    rows.append([InlineKeyboardButton(text="🏠 القائمة", callback_data=_data(CB_MENU, "main"))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def analysis_text(bundle: Bundle) -> str:
    """The preview message shown with the quality buttons."""
    from ..extractors.base import human_duration, human_size

    lines = [f"🔎 {bundle.platform} · {bundle.engine}"]
    if bundle.title:
        lines.append(f"📄 {(bundle.title or '')[:120]}")
    details = []
    if bundle.duration:
        details.append(f"⏱ {human_duration(bundle.duration)}")
    if bundle.count > 1:
        details.append(f"🗂 {bundle.count} عنصر")
    if bundle.total_bytes:
        details.append(f"💾 حتى {human_size(bundle.total_bytes)}")
    if bundle.uploader:
        details.append(f"👤 {bundle.uploader[:40]}")
    if details:
        lines.append("  ·  ".join(details))
    if bundle.live:
        lines.append("🔴 بث مباشر — الجودة تتغير حسب اللحظة")
    for note in bundle.notes[:3]:
        lines.append(f"ℹ️ {note}")
    if bundle.is_multi and not bundle.variants:
        lines.append("👇 اختر عنصراً")
    else:
        lines.append("👇 اختر الجودة أو الصوت")
    return "\n".join(lines)


def coverage_text() -> str:
    report = coverage_report()
    return (
        "🧭 تغطية المحركات الآن\n"
        f"• yt-dlp: {report.get('ytdlp_extractors', 'غير متاح')} مستخرج\n"
        f"• gallery-dl: {report.get('gallery_dl_extractors', 'غير متاح')} مستخرج صور\n"
        f"• جدول التوجيه في البوت: {report.get('routing_table', 0)} منصة موصوفة\n"
        "أي رابط غير مذكور يمر لمسار generic العام."
    )


__all__ = ["main_menu", "variants_keyboard", "items_keyboard", "busy_keyboard", "help_keyboard",
           "cookies_keyboard", "analysis_text", "coverage_text", "CB_VARIANT", "CB_CANCEL",
           "CB_TASKS", "CB_MENU", "CB_HELP", "CB_ITEM", "MAX_BUTTONS_PER_ROW"]
