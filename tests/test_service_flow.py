"""End-to-end service flow with a fake engine and a fake Telegram sender.

This is the regression net for the failures that made the old bots unreliable: dead
buttons, a size limit that ignored config, one fixed filename shared by all users, and
downloads that failed without telling anyone.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from media_bot.errors import (
    AuthRequiredError,
    FileTooLargeError,
    GrabberError,
    NoMediaFoundError,
    SenderError,
)
from media_bot.extractors.registry import Registry
from media_bot.processing.probe import probe
from media_bot.queue import DownloadQueue
from media_bot.senders import BOT_API_SEND_LIMIT
from media_bot.service import build_caption

from .conftest import FakeBotApi, ScriptedEngine, make_bundle


def make_service(settings, db, storage, vault, *, size_bytes=4096, delay=0.0, raises=None,
                 download_raises=None,
                 sender=None, queue=None, mtproto=None, max_file_mb=2000):
    settings.max_file_mb = max_file_mb
    bundle = make_bundle()
    engine = ScriptedEngine(settings, bundle=bundle, size_bytes=size_bytes, delay=delay,
                            fail_with=raises, download_fail_with=download_raises)
    from media_bot.processing.ffmpeg import FFmpeg
    from media_bot.service import GrabberService

    service = GrabberService(
        settings=settings, db=db, storage=storage, registry=Registry(settings, [engine]),
        queue=queue or DownloadQueue(max_concurrent=2, per_user=1),
        ffmpeg=FFmpeg(), vault=vault, bot_sender=sender or FakeBotApi(), mtproto=mtproto,
    )
    return service, engine, bundle


async def test_analyze_stores_a_reusable_bundle(settings, db, storage, vault) -> None:
    service, _, _ = make_service(settings, db, storage, vault)
    bundle = await service.analyze("https://example.test/video", user_id=1)
    assert bundle.token and bundle.variants
    assert service.recall(bundle.token) is bundle
    assert db.load_bundle(bundle.token)["variants"]


async def test_download_then_deliver_then_cleanup(settings, db, storage, vault) -> None:
    sender = FakeBotApi()
    service, engine, _ = make_service(settings, db, storage, vault, sender=sender)
    bundle = await service.analyze("https://example.test/video", user_id=5)
    job_id = await service.start(token=bundle.token, variant_token="v720", user_id=5, chat_id=5)
    delivery = await service.queue.get(job_id).task

    assert engine.downloads == 1
    assert delivery.outcome.bytes_sent == 4096
    assert sender.calls[0]["chat_id"] == 5
    job = db.get_job(job_id)
    assert job.status == "done" and job.size_bytes == 4096
    path = Path(job.file_path)
    assert path.exists()
    assert storage.delete_job_files(job_id) is True
    assert not path.exists()


async def test_probe_records_what_actually_arrived(settings, db, storage, vault) -> None:
    service, _, _ = make_service(settings, db, storage, vault)
    bundle = await service.analyze("https://example.test/video", user_id=6)
    job_id = await service.start(token=bundle.token, variant_token="v720", user_id=6, chat_id=6)
    await service.queue.get(job_id).task
    info = await probe(Path(db.get_job(job_id).file_path))
    assert info.size_bytes == 4096


async def test_dedupe_second_request_reuses_cache(settings, db, storage, vault) -> None:
    service, engine, _ = make_service(settings, db, storage, vault)
    bundle = await service.analyze("https://example.test/video", user_id=7)
    job = await service.start(token=bundle.token, variant_token="v720", user_id=7, chat_id=7)
    await service.queue.get(job).task
    assert engine.downloads == 1
    hit = storage.lookup_cached(bundle.url, bundle.engine, "v720")
    assert hit is not None and hit.size_bytes == 4096


async def test_expired_cache_forces_a_new_download(settings, db, storage, vault) -> None:
    settings.dedupe_ttl_minutes = 0
    service, _, _ = make_service(settings, db, storage, vault)
    bundle = await service.analyze("https://example.test/video", user_id=8)
    job = await service.start(token=bundle.token, variant_token="v720", user_id=8, chat_id=8)
    await service.queue.get(job).task
    assert storage.lookup_cached(bundle.url, bundle.engine, "v720") is None


async def test_audio_variant_is_delivered(settings, db, storage, vault) -> None:
    sender = FakeBotApi()
    service, _, _ = make_service(settings, db, storage, vault, sender=sender)
    bundle = await service.analyze("https://example.test/video", user_id=9)
    job = await service.start(token=bundle.token, variant_token="audio-best", user_id=9, chat_id=9)
    await service.queue.get(job).task
    assert sender.calls


async def test_engine_failure_is_recorded_not_swallowed(settings, db, storage, vault) -> None:
    service, _, _ = make_service(settings, db, storage, vault,
                                     download_raises=AuthRequiredError("needs cookies"))
    bundle = await service.analyze("https://example.test/video", user_id=10)
    job = await service.start(token=bundle.token, variant_token="v720", user_id=10, chat_id=10)
    with pytest.raises(AuthRequiredError):
        await service.queue.get(job).task
    assert db.get_job(job).status == "failed"


async def test_cancel_removes_temporary_files(settings, db, storage, vault) -> None:
    service, _, _ = make_service(settings, db, storage, vault, delay=5)
    bundle = await service.analyze("https://example.test/video", user_id=11)
    job = await service.start(token=bundle.token, variant_token="v720", user_id=11, chat_id=11)
    await asyncio.sleep(0.2)
    assert await service.cancel(job) is True
    assert not list(storage.job_dir(job).glob("*.mp4"))
    assert db.get_job(job).status == "cancelled"


async def test_project_size_limit_blocks_before_send(settings, db, storage, vault) -> None:
    sender = FakeBotApi()
    service, _, _ = make_service(settings, db, storage, vault,
                                     size_bytes=2 * 1024 * 1024, max_file_mb=1, sender=sender)
    bundle = await service.analyze("https://example.test/video", user_id=12)
    job = await service.start(token=bundle.token, variant_token="v720", user_id=12, chat_id=12)
    with pytest.raises(FileTooLargeError):
        await service.queue.get(job).task
    assert sender.calls == []


class FakeMtproto:
    def __init__(self) -> None:
        self.client = object()
        self.ceiling = 2000 * 1024 * 1024


class FakeLocalSender:
    ceiling = 2000 * 1024 * 1024


async def test_route_preference_for_large_files(settings, db, storage, vault) -> None:
    service, _, _ = make_service(settings, db, storage, vault)
    big = 60 * 1024 * 1024
    assert service.choose_route(big)[0] == "split"
    service.mtproto = FakeMtproto()
    assert service.choose_route(big)[0] == "mtproto"
    service.local_sender = FakeLocalSender()
    service.local_api_ready = True
    assert service.choose_route(big)[0] == "local_api"


async def test_blocked_route_when_send_mode_is_pinned(settings, db, storage, vault) -> None:
    settings.send_mode = "bot_api"
    service, _, _ = make_service(settings, db, storage, vault)
    route, reason = service.choose_route(BOT_API_SEND_LIMIT + 1)
    assert route == "blocked" and reason


async def test_sender_refusal_is_reported(settings, db, storage, vault) -> None:
    service, _, _ = make_service(settings, db, storage, vault, sender=FakeBotApi(fail=True))
    bundle = await service.analyze("https://example.test/video", user_id=13)
    job = await service.start(token=bundle.token, variant_token="v720", user_id=13, chat_id=13)
    with pytest.raises(SenderError):
        await service.queue.get(job).task


async def test_hourly_quota_blocks_the_fourth_request(settings, db, storage, vault) -> None:
    settings.max_downloads_per_user_per_hour = 3
    service, _, _ = make_service(settings, db, storage, vault)
    bundle = await service.analyze("https://example.test/video", user_id=14)
    for _ in range(3):
        job = await service.start(token=bundle.token, variant_token="v720", user_id=14, chat_id=14)
        await service.queue.get(job).task
    with pytest.raises(GrabberError):
        await service.start(token=bundle.token, variant_token="v720", user_id=14, chat_id=14)


async def test_unknown_bundle_token_is_a_clear_error(settings, db, storage, vault) -> None:
    service, _, _ = make_service(settings, db, storage, vault)
    with pytest.raises(NoMediaFoundError):
        await service.start(token="deadbeef", variant_token="v720", user_id=15, chat_id=15)


async def test_two_users_do_not_share_a_file_slot(settings, db, storage, vault) -> None:
    service, _, _ = make_service(settings, db, storage, vault, delay=0.05)
    bundle = await service.analyze("https://example.test/video", user_id=16)
    first = await service.start(token=bundle.token, variant_token="v720", user_id=16, chat_id=16)
    second = await service.start(token=bundle.token, variant_token="v720", user_id=17, chat_id=17)
    await asyncio.gather(service.queue.get(first).task, service.queue.get(second).task)
    path_a, path_b = Path(db.get_job(first).file_path), Path(db.get_job(second).file_path)
    assert path_a != path_b and path_a.exists() and path_b.exists()


async def test_concurrent_requests_are_queued(settings, db, storage, vault) -> None:
    queue = DownloadQueue(max_concurrent=1, per_user=1)
    service, engine, _ = make_service(settings, db, storage, vault, delay=0.05, queue=queue)
    bundle = await service.analyze("https://example.test/video", user_id=18)
    jobs = [await service.start(token=bundle.token, variant_token=f"v720-{i}", user_id=18,
                               chat_id=18) for i in range(3)]
    await asyncio.gather(*(service.queue.get(job).task for job in jobs))
    assert engine.downloads == 3
    assert queue.stats()["succeeded"] == 3


def test_caption_reports_the_chosen_quality() -> None:
    bundle = make_bundle()
    text = build_caption(bundle, bundle.variants[0], None)
    assert "720p HD" in text and "UnitTest" in text


def test_caption_has_no_placeholder_text() -> None:
    text = build_caption(make_bundle(title=None), None, None)
    for junk in ("TODO", "None", "قيد التطوير", "N/A", "{}"):
        assert junk not in text


def test_status_exposes_every_capability(settings, db, storage, vault) -> None:
    service, _, _ = make_service(settings, db, storage, vault)
    status = service.status()
    assert status["engines"][0]["engine"] == "scripted"
    assert status["sender"]["configured"] is True
    assert status["queue"]["max_concurrent"] == 2
    assert "cookies" in status and "split" in status
