"""Video concurrency control — ensures at most 1 video task runs at a time.

Implements P2-02: strict single-concurrency video task execution with
a FIFO wait queue and OOM-prevention memory checks for 4G ECS.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import psutil

logger = logging.getLogger(__name__)


@dataclass
class _QueuedJob:
    """Internal: a job waiting in the queue."""
    job_id: str
    coro_factory: Callable[[], Awaitable[None]]
    queued_at: float = 0.0


class VideoConcurrencyController:
    """Singleton controller ensuring strict single-concurrency video tasks."""

    MAX_CONCURRENT: int = 1
    MAX_QUEUE_SIZE: int = 10
    MEMORY_THRESHOLD_MB: int = 512

    def __init__(self) -> None:
        self._lock: asyncio.Lock = asyncio.Lock()
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=self.MAX_QUEUE_SIZE)
        self._active_count: int = 0
        self._enabled: bool = True
        self._processing_event: asyncio.Event = asyncio.Event()
        self._processing_event.set()

    @property
    def max_queue_size(self) -> int:
        return self.MAX_QUEUE_SIZE

    @max_queue_size.setter
    def max_queue_size(self, value: int) -> None:
        self.MAX_QUEUE_SIZE = value
        new_q = asyncio.Queue(maxsize=value)
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                new_q.put_nowait(item)
            except asyncio.QueueEmpty:
                break
        self._queue = new_q

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def active_count(self) -> int:
        return self._active_count

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    def _check_memory(self) -> bool:
        mem = psutil.virtual_memory()
        available_mb = mem.available / (1024 * 1024)
        return available_mb >= self.MEMORY_THRESHOLD_MB

    async def submit(self, job_id, coro_factory):
        if not self._enabled:
            asyncio.create_task(self._run_with_release(job_id, coro_factory))
            return "processing"

        if not self._check_memory():
            return "rejected"

        if self._active_count < self.MAX_CONCURRENT:
            return await self._start_immediately(job_id, coro_factory)

        return await self._enqueue(job_id, coro_factory)

    async def _start_immediately(self, job_id, coro_factory):
        self._active_count += 1
        self._processing_event.clear()
        asyncio.create_task(self._run_with_release(job_id, coro_factory))
        return "processing"

    async def _enqueue(self, job_id, coro_factory):
        if self._queue.full():
            return "rejected"

        job = _QueuedJob(job_id=job_id, coro_factory=coro_factory)
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            return "rejected"

        return "queued"

    async def _run_with_release(self, job_id, coro_factory):
        try:
            await coro_factory()
        except Exception:
            pass
        finally:
            self._active_count -= 1
            self._processing_event.set()

            if not self._queue.empty():
                try:
                    next_job = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                if next_job is not None:
                    await self._start_immediately(next_job.job_id, next_job.coro_factory)

    async def drain(self):
        while self._active_count > 0:
            await asyncio.sleep(0.1)
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def reset(self):
        self._active_count = 0
        self._enabled = True
        self._processing_event.set()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break


video_concurrency = VideoConcurrencyController()
