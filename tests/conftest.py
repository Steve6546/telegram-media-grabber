"""Shared fixtures: an isolated data dir and fake engines/senders, no network."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from media_bot.auth import CookieVault
from media_bot.config import Settings
from media_bot.db import Database
from media_bot.extractors.base import (
    Bundle,
    DownloadResult,
    Extractor,
    MediaKind,
    Variant,
    Want,
)
from media_bot.extractors.registry import Registry
from media_bot.processing.ffmpeg import FFmpeg
from media_bot.queue import DownloadQueue
from media_bot.senders import SendOutcome
from media_bot.service import GrabberService
from media_bot.storage import StorageManager


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    cfg = Settings(data_dir=tmp_path / "data", bot_token="", cookie_encryption_key="")
    cfg.max_concurrent_downloads = 2
    cfg.per_user_concurrent = 1
    cfg.max_file_mb = 2000
    cfg.validate()
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return cfg


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.db_path)
    yield database
    database.close()


@pytest.fixture
def storage(settings: Settings, db: Database) -> StorageManager:
    return StorageManager(settings, db)


@pytest.fixture
def vault(settings: Settings, db: Database) -> CookieVault:
    return CookieVault(settings, db)


class FakeBotApi:
    """Stands in for aiogram's sender: records what would have been sent."""

    name = "bot_api"
    ceiling = 50 * 1024 * 1024
    uses_paths = False

    def __init__(self, *, ceiling: int | None = None, fail: bool = False) -> None:
        self.calls: list[dict] = []
        self.bot = object()
        if ceiling is not None:
            self.ceiling = ceiling
        self.fail = fail

    async def send(self, chat_id, paths, *, caption, kind, force_document=False,
                   duration=None, thumb=None, disable_notification=False) -> SendOutcome:
        sizes = [Path(path).stat().st_size for path in paths]
        if any(size > self.ceiling for size in sizes) or self.fail:
            from media_bot.errors import SenderError

            raise SenderError("sender refused")
        self.calls.append({"chat_id": chat_id, "paths": [str(p) for p in paths],
                           "caption": caption, "kind": kind.value,
                           "force_document": force_document})
        return SendOutcome(message_ids=[1], sender=self.name, bytes_sent=sum(sizes))


class ScriptedEngine(Extractor):
    """Returns a fixed bundle and writes a synthetic file on download."""

    engine_id = "scripted"
    provides = frozenset({MediaKind.VIDEO})
    priority = 1

    def __init__(self, settings, *, bundle: Bundle, size_bytes: int = 4096,
                 delay: float = 0.0, fail_with: Exception | None = None,
                 download_fail_with: Exception | None = None) -> None:
        super().__init__(settings)
        self.bundle = bundle
        self.size_bytes = size_bytes
        self.delay = delay
        self.fail_with = fail_with
        self.download_fail_with = download_fail_with
        self.downloads = 0
        self.extracts = 0

    def supports(self, host: str, url: str, *, want: Want = Want.AUTO) -> bool:
        return True

    async def extract(self, url, *, host="", cookies_path=None, want=Want.AUTO) -> Bundle:
        self.extracts += 1
        if self.fail_with:
            raise self.fail_with
        return self.bundle

    async def download(self, bundle, variant, dest_dir, *, progress=None, cookies_path=None):
        self.downloads += 1
        if self.download_fail_with:
            raise self.download_fail_with
        if self.fail_with:
            raise self.fail_with
        if self.delay:
            await asyncio.sleep(self.delay)
        if progress:
            await progress(50.0, "نصف")
        target = Path(dest_dir) / "result.mp4"
        target.write_bytes(b"\0" * self.size_bytes)
        if progress:
            await progress(100.0, "تم")
        return DownloadResult(files=[target], bytes_total=self.size_bytes, engine=self.engine_id)


def make_bundle(**overrides) -> Bundle:
    base = dict(
        engine="scripted",
        platform="UnitTest",
        url="https://example.test/video",
        kind=MediaKind.VIDEO,
        token="tok123",
        title="اختبار",
        duration=12,
        count=1,
    )
    base.update(overrides)
    bundle = Bundle(**base)
    bundle.variants = overrides.get("variants") or [
        Variant(token="v720", label="720p HD", kind=MediaKind.VIDEO, format_spec="136+140",
                ext="mp4", height=720, filesize=4096, merge=True),
        Variant(token="audio-best", label="صوت فقط", kind=MediaKind.AUDIO, format_spec="140",
                ext="m4a", filesize=1024),
    ]
    return bundle


@pytest.fixture
def service_factory(settings: Settings, db: Database, storage: StorageManager,
                    vault: CookieVault):
    def build(engine: ScriptedEngine, *, sender: FakeBotApi | None = None,
              mtproto=None, queue: DownloadQueue | None = None) -> GrabberService:
        registry = Registry(settings, [engine])
        return GrabberService(
            settings=settings, db=db, storage=storage, registry=registry,
            queue=queue or DownloadQueue(max_concurrent=2, per_user=1),
            ffmpeg=FFmpeg(), vault=vault, bot_sender=sender or FakeBotApi(), mtproto=mtproto,
        )

    return build


@pytest.fixture
def freeze_time(monkeypatch):
    """Pin time.time() for TTL assertions."""
    clock = {"now": 1_700_000_000.0}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    return clock
