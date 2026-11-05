"""Download queue: bounded concurrency, per-user fairness and real cancellation.

Every job is an asyncio task guarded by two semaphores (global and per-user). Cancelling
kills the task, which kills the engine subprocess loop and releases the slot; the
service layer then deletes the job directory.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .errors import CancelledJobError, QueueFullError
from .extractors.base import ProgressHook as ProgressCallback

log = logging.getLogger("grabber.queue")


@dataclass(slots=True)
class TaskHandle:
    job_id: str
    user_id: int
    label: str
    status: str = "queued"
    progress: float = 0.0
    detail: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str | None = None
    task: asyncio.Task[Any] | None = None
    on_progress: ProgressCallback | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "user_id": self.user_id,
            "label": self.label,
            "status": self.status,
            "progress": round(self.progress, 1),
            "detail": self.detail,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 1),
            "error": self.error,
        }


class DownloadQueue:
    def __init__(self, *, max_concurrent: int = 3, per_user: int = 1) -> None:
        self.max_concurrent = max(1, max_concurrent)
        self.per_user = max(1, per_user)
        self._global = asyncio.Semaphore(self.max_concurrent)
        self._per_user: dict[int, asyncio.Semaphore] = {}
        self._tasks: dict[str, TaskHandle] = {}
        self._history: list[dict[str, Any]] = []

    # ---------------- introspection ----------------

    def _user_semaphore(self, user_id: int) -> asyncio.Semaphore:
        semaphore = self._per_user.get(user_id)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self.per_user)
            self._per_user[user_id] = semaphore
        return semaphore

    @property
    def active(self) -> list[TaskHandle]:
        return [handle for handle in self._tasks.values() if handle.task and not handle.task.done()]

    def user_active(self, user_id: int) -> list[TaskHandle]:
        return [handle for handle in self.active if handle.user_id == user_id]

    def get(self, job_id: str) -> TaskHandle | None:
        return self._tasks.get(job_id)

    def stats(self) -> dict[str, Any]:
        return {
            "max_concurrent": self.max_concurrent,
            "per_user": self.per_user,
            "active": len(self.active),
            "queued": sum(1 for handle in self.active if handle.status == "queued"),
            "finished": len(self._history),
            "succeeded": sum(1 for row in self._history if row["status"] == "done"),
            "failed": sum(1 for row in self._history if row["status"] == "failed"),
            "cancelled": sum(1 for row in self._history if row["status"] == "cancelled"),
        }

    # ---------------- submission ----------------

    def submit(self, coro_factory: Callable[[Callable[[float, str | None], Awaitable[None]]], Awaitable[Any]],
               *, job_id: str, user_id: int, label: str,
               on_progress: ProgressCallback | None = None) -> TaskHandle:
        handle = TaskHandle(job_id=job_id, user_id=user_id, label=label, on_progress=on_progress)
        if len(self.active) >= self.max_concurrent * 4:
            raise QueueFullError()

        async def runner() -> Any:
            user_sem = self._user_semaphore(user_id)
            async with user_sem:
                handle.status = "queued"
                await self._global.acquire()
                try:
                    handle.status = "running"
                    handle.started_at = time.time()

                    async def report(percent: float, text: str | None = None) -> None:
                        handle.progress = max(0.0, min(100.0, percent))
                        handle.detail = text
                        if handle.on_progress:
                            await handle.on_progress(percent, text)

                    result = await coro_factory(report)
                    handle.status = "done"
                    handle.progress = 100.0
                    return result
                except asyncio.CancelledError:
                    handle.status = "cancelled"
                    raise
                except Exception as exc:
                    handle.status = "failed"
                    handle.error = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    self._global.release()
                    handle.finished_at = time.time()
                    self._history.append(handle.snapshot())
                    del self._tasks[job_id]

        handle.task = asyncio.create_task(runner(), name=f"grab-{job_id}")
        self._tasks[job_id] = handle
        return handle

    # ---------------- cancellation ----------------

    async def cancel(self, job_id: str) -> TaskHandle | None:
        handle = self._tasks.get(job_id)
        if handle is None or handle.task is None or handle.task.done():
            return None
        handle.task.cancel()
        try:
            await handle.task
        except asyncio.CancelledError:
            pass
        except Exception:  # the runner re-raised the engine error after cancel
            pass
        handle.status = "cancelled"
        return handle

    async def cancel_for_user(self, user_id: int) -> list[str]:
        cancelled: list[str] = []
        for handle in self.user_active(user_id):
            if await self.cancel(handle.job_id):
                cancelled.append(handle.job_id)
        return cancelled

    async def shutdown(self) -> None:
        for handle in list(self.active):
            if handle.task:
                handle.task.cancel()
        await asyncio.gather(*(handle.task for handle in self.active if handle.task),
                             return_exceptions=True)

    async def wait_all(self, *, timeout: float = 120) -> None:
        tasks = [handle.task for handle in self.active if handle.task]
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout)


def is_cancelled(exc: BaseException) -> bool:
    return isinstance(exc, (asyncio.CancelledError, CancelledJobError))


__all__ = ["DownloadQueue", "TaskHandle", "is_cancelled"]
