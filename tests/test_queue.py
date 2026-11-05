"""Queue behaviour: bounded concurrency, per-user fairness, cancellation, progress."""

from __future__ import annotations

import asyncio

import pytest

from media_bot.errors import QueueFullError
from media_bot.queue import DownloadQueue


def factory(seconds: float, events: list, tag: str):
    def build(report):
        async def run():
            events.append(("start", tag))
            await asyncio.sleep(seconds)
            await report(50.0, f"half-{tag}")
            await asyncio.sleep(seconds)
            events.append(("end", tag))
            return tag
        return run()
    return build


async def test_global_concurrency_is_enforced() -> None:
    events: list = []
    queue = DownloadQueue(max_concurrent=2, per_user=10)
    handles = [queue.submit(factory(0.05, events, f"j{i}"), job_id=f"j{i}", user_id=9,
                           label=f"job {i}") for i in range(5)]
    assert len(queue.active) == 5
    results = await asyncio.gather(*(handle.task for handle in handles))
    assert sorted(results) == sorted(["j0", "j1", "j2", "j3", "j4"])
    assert queue.stats()["finished"] == 5
    assert queue.stats()["succeeded"] == 5


async def test_per_user_limit_serialises_one_user() -> None:
    events: list = []
    queue = DownloadQueue(max_concurrent=4, per_user=1)
    handles = [queue.submit(factory(0.03, events, f"u{i}"), job_id=f"u{i}", user_id=1,
                           label=f"u{i}") for i in range(3)]
    await asyncio.gather(*(handle.task for handle in handles))
    starts = [tag for kind, tag in events if kind == "start"]
    ends = [tag for kind, tag in events if kind == "end"]
    # strictly sequential: each job ends before the next starts
    assert starts == ends


async def test_progress_callback_chains_to_the_caller() -> None:
    seen: list[tuple[float, str | None]] = []
    queue = DownloadQueue(max_concurrent=1, per_user=1)

    async def report(percent: float, text: str | None) -> None:
        seen.append((percent, text))

    handle = queue.submit(factory(0.01, [], "p"), job_id="p", user_id=1, label="p",
                          on_progress=report)
    await handle.task
    assert (50.0, "half-p") in seen
    assert handle.progress == 100.0


async def test_cancel_stops_a_running_job_and_frees_the_slot() -> None:
    queue = DownloadQueue(max_concurrent=1, per_user=1)
    started = asyncio.Event()

    def build(report):
        async def run():
            started.set()
            await asyncio.sleep(30)
        return run()

    handle = queue.submit(build, job_id="slow", user_id=1, label="slow")
    await started.wait()
    assert handle.status == "running" and len(queue.active) == 1

    cancelled = await queue.cancel("slow")
    assert cancelled is not None and cancelled.status == "cancelled"
    assert queue.active == []

    # the semaphore was released, so the next job can start immediately
    done = asyncio.Event()

    def build2(report):
        async def run2():
            done.set()
            return "ok"
        return run2()

    second = queue.submit(build2, job_id="next", user_id=1, label="next")
    await asyncio.wait_for(done.wait(), timeout=2)
    await second.task


async def test_cancel_unknown_job_is_a_no_op() -> None:
    queue = DownloadQueue()
    assert await queue.cancel("nope") is None


async def test_failure_is_recorded_and_raised() -> None:
    queue = DownloadQueue(max_concurrent=1, per_user=1)

    def build(report):
        async def run():
            raise RuntimeError("engine exploded")
        return run()

    handle = queue.submit(build, job_id="bad", user_id=1, label="bad")
    with pytest.raises(RuntimeError):
        await handle.task
    assert handle.status == "failed"
    assert "engine exploded" in handle.error
    assert queue.stats()["failed"] == 1


async def test_cancel_for_user_only_touches_that_user() -> None:
    queue = DownloadQueue(max_concurrent=5, per_user=5)
    started = asyncio.Event()

    def build(report):
        async def run():
            started.set()
            await asyncio.sleep(30)
        return run()

    mine = [queue.submit(build, job_id=f"m{i}", user_id=1, label="m") for i in range(2)]
    theirs = queue.submit(build, job_id="t0", user_id=2, label="t")
    await started.wait()
    cancelled = await queue.cancel_for_user(1)
    assert sorted(cancelled) == ["m0", "m1"]
    assert queue.get("t0") is not None
    await queue.cancel("t0")
    assert len(mine) == 2 and theirs.status == "cancelled"


async def test_queue_full_guard() -> None:
    queue = DownloadQueue(max_concurrent=1, per_user=1)

    def build(report):
        async def run():
            await asyncio.sleep(5)
        return run()

    handles = [queue.submit(build, job_id=f"q{i}", user_id=1, label="q") for i in range(4)]
    with pytest.raises(QueueFullError):
        queue.submit(build, job_id="overflow", user_id=1, label="q")
    for handle in handles:
        handle.task.cancel()
    await asyncio.gather(*(h.task for h in handles), return_exceptions=True)


async def test_shutdown_clears_everything() -> None:
    queue = DownloadQueue(max_concurrent=2, per_user=2)
    started = asyncio.Event()

    def build(report):
        async def run():
            started.set()
            await asyncio.sleep(30)
        return run()

    queue.submit(build, job_id="s1", user_id=1, label="s")
    await started.wait()
    await queue.shutdown()
    assert queue.active == []
