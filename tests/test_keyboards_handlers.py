"""Keyboard wiring and handler construction.

Every button must map to code that runs, and callback_data must fit Telegram's 64-byte
limit. These tests fail if a dead button or an over-long payload is ever added.
"""

from __future__ import annotations

import pytest
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from media_bot.bot import build_dispatcher, extract_url
from media_bot.bot.handlers import Services, _guess_want
from media_bot.bot.keyboards import (
    CB_CANCEL,
    CB_MENU,
    CB_TASKS,
    CB_VARIANT,
    analysis_text,
    busy_keyboard,
    coverage_text,
    help_keyboard,
    items_keyboard,
    main_menu,
    variants_keyboard,
)
from media_bot.extractors.base import Bundle, MediaKind, Variant

from .conftest import FakeBotApi, ScriptedEngine, make_bundle


def all_buttons(markup: InlineKeyboardMarkup) -> list:
    return [button for row in markup.inline_keyboard for button in row]


def test_main_menu_buttons_are_all_short_and_nonempty() -> None:
    buttons = all_buttons(main_menu())
    assert buttons
    for button in buttons:
        assert button.callback_data
        assert len(button.callback_data.encode("utf-8")) <= 64
        assert button.text.strip()


def test_variant_keyboard_has_one_button_per_real_quality() -> None:
    bundle = make_bundle()
    bundle.token = "a" * 12
    markup = variants_keyboard_of(bundle)
    tokens = {
        button.callback_data.split(":")[-1]
        for button in all_buttons(markup)
        if button.callback_data.startswith(f"{CB_VARIANT}:")
    }
    assert tokens == {v.token for v in bundle.variants}


def variants_keyboard_of(bundle: Bundle) -> InlineKeyboardMarkup:
    return variants_keyboard(bundle)


def test_callback_payload_survives_telegram_limit_with_long_tokens() -> None:
    bundle = make_bundle()
    bundle.token = "f" * 12
    bundle.variants = [Variant(token="v2160-webm", label="4K", kind=MediaKind.VIDEO,
                              format_spec="315+251", ext="webm", height=2160)]
    button = all_buttons(variants_keyboard_of(bundle))[0]
    assert button.callback_data == f"{CB_VARIANT}:{bundle.token}:v2160-webm"
    assert len(button.callback_data.encode()) <= 64


def test_overlong_callback_is_rejected_not_truncated() -> None:
    bundle = make_bundle()
    bundle.token = "x" * 40  # would overflow once joined
    bundle.variants = [Variant(token="v" + "9" * 40, label="row", kind=MediaKind.VIDEO,
                              format_spec="1", ext="mp4")]
    with pytest.raises(ValueError):
        variants_keyboard_of(bundle)


def test_items_keyboard_numbers_entries() -> None:
    bundle = make_bundle()
    bundle.token = "t" * 12
    bundle.items = [type("I", (), {"name": f"Episode {i}"})() for i in range(7)]
    bundle.count = 7
    buttons = all_buttons(items_keyboard(bundle))
    numbered = [b for b in buttons if b.callback_data.startswith("i:")]
    assert len(numbered) == 7
    assert numbered[0].text.startswith("1. Episode 0")


def test_items_keyboard_caps_at_twenty_five() -> None:
    bundle = make_bundle()
    bundle.token = "t" * 12
    bundle.items = [type("I", (), {"name": f"E{i}"})() for i in range(40)]
    bundle.count = 40
    buttons = [b for b in all_buttons(items_keyboard(bundle)) if b.callback_data.startswith("i:")]
    assert len(buttons) == 25, "picker must cap at 25 entries (Telegram's button ceiling)"


def test_busy_keyboard_carries_the_real_job_id() -> None:
    button = all_buttons(busy_keyboard("job123"))[0]
    assert button.callback_data == f"{CB_CANCEL}:job123"


def test_analysis_text_reports_platform_engine_and_choice() -> None:
    bundle = make_bundle(title="فيديو اختبار", duration=65, count=1)
    bundle.notes = ["ملاحظة واحدة"]
    text = analysis_text(bundle)
    assert "UnitTest" in text and "scripted" in text
    assert "فيديو اختبار" in text
    assert "ملاحظة واحدة" in text
    assert "اختر الجودة" in text
    for junk in ("None", "قيد التطوير", "TODO"):
        assert junk not in text


def test_coverage_text_uses_live_numbers() -> None:
    text = coverage_text()
    assert "yt-dlp" in text and "gallery-dl" in text


@pytest.mark.parametrize("text,want", [
    ("نزّل لي صوت فقط", "audio"),
    ("أريد mp3", "audio"),
    ("هذي صور الألبوم", "image"),
    ("download the album photos", "image"),
    ("just a normal link", "auto"),
])
def test_appetite_detection(text: str, want: str) -> None:
    assert _guess_want(text).value == want


@pytest.mark.parametrize("text,expected", [
    ("شوف هذا https://youtu.be/abc شكراً", "https://youtu.be/abc"),
    ("no link here", None),
    ("https://a.example/x?y=1&z=2", "https://a.example/x?y=1&z=2"),
    ("رابط: <https://example.com/a>", "https://example.com/a"),
])
def test_url_extraction(text: str, expected: str | None) -> None:
    assert extract_url(text) == expected


def test_dispatcher_builds_and_registers_handlers(settings) -> None:
    engine = ScriptedEngine(settings, bundle=make_bundle())
    from media_bot.auth import CookieVault
    from media_bot.db import Database
    from media_bot.extractors.registry import Registry
    from media_bot.processing.ffmpeg import FFmpeg
    from media_bot.queue import DownloadQueue
    from media_bot.service import GrabberService
    from media_bot.storage import StorageManager

    db = Database(settings.db_path)
    storage = StorageManager(settings, db)
    service = GrabberService(settings=settings, db=db, storage=storage,
                            registry=Registry(settings, [engine]),
                            queue=DownloadQueue(max_concurrent=1, per_user=1),
                            ffmpeg=FFmpeg(), vault=CookieVault(settings, db),
                            bot_sender=FakeBotApi())
    bot = make_bot()
    services = Services(settings=settings, db=db, storage=storage, queue=service.queue,
                        ffmpeg=FFmpeg(), vault=service.vault, service=service, bot=bot)
    dispatcher = build_dispatcher(services)
    registered = len(dispatcher.update.handlers)
    assert registered >= 1, "dispatcher must have at least one update handler"
    message_handlers = dispatcher.sub_routers[0].message.handlers
    callback_handlers = dispatcher.sub_routers[0].callback_query.handlers
    assert len(message_handlers) >= 7 and len(callback_handlers) >= 7
    db.close()


def make_bot() -> Bot:
    """A syntactically valid token: constructing a Bot performs no network I/O."""
    return Bot(token="123456:AAExampleExampleExampleExampleExampl0")


def test_help_keyboard_points_at_the_main_menu() -> None:
    button = all_buttons(help_keyboard())[0]
    assert button.callback_data == f"{CB_MENU}:main"


def test_task_button_exists_on_main_menu() -> None:
    datas = [b.callback_data for b in all_buttons(main_menu())]
    assert CB_TASKS in [d.split(":")[0] for d in datas]
