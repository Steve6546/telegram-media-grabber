"""Job state, dedupe cache, bundle tokens and storage hygiene."""

from __future__ import annotations

import time

import pytest

from media_bot.db import Database
from media_bot.storage import StorageManager, cache_key


@pytest.fixture
def database(db: Database) -> Database:
    return db


def test_job_lifecycle_persists(database: Database) -> None:
    job = database.create_job(user_id=7, chat_id=70, url="https://x/y", engine="ytdlp",
                             platform="YouTube", variant="720")
    assert job.status == "queued"
    database.update_job(job.id, status="running", progress=42.5)
    reloaded = database.get_job(job.id)
    assert (reloaded.status, reloaded.progress) == ("running", 42.5)
    assert reloaded.is_active


def test_update_job_ignores_unknown_columns(database: Database) -> None:
    job = database.create_job(user_id=1, chat_id=1, url="u", engine="e", platform="p", variant="v")
    database.update_job(job.id, status="done", nonexistent="boom")
    assert database.get_job(job.id).status == "done"


def test_active_filter_and_user_scoping(database: Database) -> None:
    mine = database.create_job(user_id=1, chat_id=1, url="a", engine="e", platform="p", variant="v")
    theirs = database.create_job(user_id=2, chat_id=2, url="b", engine="e", platform="p", variant="v")
    database.update_job(theirs.id, status="done")
    active = database.user_jobs(1)
    assert [job.id for job in active] == [mine.id]
    assert database.user_jobs(1, active_only=False, limit=5)


def test_hourly_quota_counts_per_user(database: Database) -> None:
    for index in range(3):
        database.create_job(user_id=9, chat_id=9, url=f"u{index}", engine="e", platform="p",
                           variant="v")
    database.create_job(user_id=10, chat_id=10, url="other", engine="e", platform="p", variant="v")
    assert database.count_recent(9, window_seconds=3600) == 3
    assert database.count_recent(10, window_seconds=3600) == 1
    assert database.count_recent(11, window_seconds=3600) == 0


def test_dedupe_roundtrip_and_expiry(database: Database, storage: StorageManager,
                                    settings) -> None:
    job = database.create_job(user_id=1, chat_id=1, url="u", engine="e", platform="p", variant="v")
    path = storage.reserve(job.id, "file.mp4")
    path.write_bytes(b"payload")
    database.dedupe_store(cache_key("u", "e", "v"), job_id=job.id, file_path=str(path),
                          ttl_seconds=60)
    hit = storage.lookup_cached("u", "e", "v")
    assert hit and hit.size_bytes == 7

    database.dedupe_store(cache_key("u2", "e", "v"), job_id=job.id, file_path=str(path),
                          ttl_seconds=-1)
    assert storage.lookup_cached("u2", "e", "v") is None


def test_lookup_cached_drops_missing_files(database: Database, storage: StorageManager) -> None:
    job = database.create_job(user_id=1, chat_id=1, url="u", engine="e", platform="p", variant="v")
    ghost = storage.job_dir(job.id) / "gone.mp4"
    database.dedupe_store(cache_key("u", "e", "v"), job_id=job.id, file_path=str(ghost),
                          ttl_seconds=60)
    assert storage.lookup_cached("u", "e", "v") is None


def test_bundle_tokens_roundtrip(database: Database) -> None:
    payload = {"variants": [{"token": "v720"}], "url": "https://x", "engine": "ytdlp"}
    database.save_bundle("abc123", url="https://x", engine="ytdlp", payload=payload,
                        ttl_seconds=30)
    loaded = database.load_bundle("abc123")
    assert loaded["url"] == "https://x" and loaded["engine"] == "ytdlp"
    database.save_bundle("expired", url="u", engine="e", payload={}, ttl_seconds=-1)
    assert database.load_bundle("expired") is None


def test_prefs_defaults_and_updates(database: Database) -> None:
    assert database.get_prefs(42) == {"default_variant": "best", "notify_done": True}
    updated = database.set_pref(42, default_variant="audio-best", notify_done=False)
    assert updated["default_variant"] == "audio-best"
    assert database.get_prefs(42)["notify_done"] is False


def test_cookie_metadata_and_drop(database: Database) -> None:
    database.remember_cookies(5, domains=["instagram.com", "tiktok.com"], entries=12)
    status = database.cookie_status(5)
    assert status["entries"] == 12 and "tiktok.com" in status["domains"]
    database.drop_cookies(5)
    assert database.cookie_status(5) is None


# ---------------- storage ----------------

def test_reserve_is_unique_per_call(storage: StorageManager) -> None:
    first = storage.reserve("job-a", "Video Name.mp4")
    first.write_bytes(b"x")
    second = storage.reserve("job-a", "Video Name.mp4")
    third = storage.reserve("job-a", "Video Name.mp4")
    assert len({first, second, third}) == 3
    assert second.name == "Video Name-1.mp4"


def test_reserve_blocks_traversal(storage: StorageManager) -> None:
    path = storage.reserve("job-b", "../../../../windows/system32/config")
    assert str(storage.settings.downloads_dir) in str(path.resolve())
    assert ".." not in path.parts


def test_job_dir_isolation_prevents_the_old_overwrite_bug(storage: StorageManager) -> None:
    """Two concurrent jobs must not write the same fixed filename."""
    a = storage.job_dir("job-1") / "downloaded_video.mp4"
    b = storage.job_dir("job-2") / "downloaded_video.mp4"
    a.write_bytes(b"one")
    b.write_bytes(b"two")
    assert a.read_bytes() == b"one" and b.read_bytes() == b"two"


def test_purge_removes_only_expired(settings, db: Database, storage: StorageManager) -> None:
    fresh = storage.job_dir("fresh") / "f.mp4"
    fresh.write_bytes(b"data")
    stale_dir = storage.job_dir("stale")
    stale = stale_dir / "s.mp4"
    stale.write_bytes(b"data")
    old = time.time() - (settings.temp_ttl_minutes * 60 + 600)
    import os

    os.utime(stale, (old, old))
    stats = storage.purge_expired()
    assert stats["dirs_removed"] >= 1
    assert not stale_dir.exists()
    assert fresh.exists()


def test_used_bytes_tracks_written_files(settings, db: Database, storage: StorageManager) -> None:
    path = storage.job_dir("meter") / "m.bin"
    path.write_bytes(b"0" * 1234)
    assert storage.used_bytes() >= 1234


def test_cache_key_varies_by_variant() -> None:
    assert cache_key("u", "e", "720") != cache_key("u", "e", "1080")
    assert cache_key("u", "e", "720") == cache_key("u", "e", "720")
