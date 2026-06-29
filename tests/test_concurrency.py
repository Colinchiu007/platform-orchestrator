"""Unit tests for video concurrency control (P2-02).

Tests the VideoConcurrencyController singleton directly (no HTTP layer).
Verifies:
  - Single task starts immediately
  - Concurrent tasks serialize: 1 processing + N queued
  - Queue full → reject
  - Feature gate disable → bypass
  - Sequential dequeue after completion
  - Memory check rejection
  - Controller reset / drain
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from services.concurrency_control import (
    VideoConcurrencyController,
    video_concurrency,
)


@pytest.fixture(autouse=True)
def _reset_controller():
    """Reset the singleton controller before each test."""
    video_concurrency.reset()
    yield
    video_concurrency.reset()


@pytest.mark.asyncio
async def test_single_task_starts_immediately():
    """A single task should be accepted as 'processing' immediately."""
    executed = []

    async def _task():
        executed.append(1)

    status = await video_concurrency.submit("job-1", _task)

    assert status == "processing"
    assert video_concurrency.active_count == 1

    # Wait for task to complete
    await video_concurrency.drain()
    assert len(executed) == 1
    assert video_concurrency.active_count == 0


@pytest.mark.asyncio
async def test_three_tasks_one_processing_two_queued():
    """Submit 3 tasks: 1st should be 'processing', 2nd + 3rd 'queued'."""
    order = []

    async def _make_task(name: str):
        async def _task():
            order.append(f"{name}-start")
            await asyncio.sleep(0.05)
            order.append(f"{name}-end")
        return _task

    s1 = await video_concurrency.submit("j1", await _make_task("A"))
    s2 = await video_concurrency.submit("j2", await _make_task("B"))
    s3 = await video_concurrency.submit("j3", await _make_task("C"))

    assert s1 == "processing"
    assert s2 == "queued"
    assert s3 == "queued"
    assert video_concurrency.active_count == 1
    assert video_concurrency.queue_size >= 2  # may have started dequeuing

    await video_concurrency.drain()

    # Verify serial execution: A → B → C
    assert order == ["A-start", "A-end", "B-start", "B-end", "C-start", "C-end"]


@pytest.mark.asyncio
async def test_sequential_dequeue_after_completion():
    """When task 1 completes, task 2 should automatically dequeue and start."""
    events = []

    async def _slow_task():
        events.append("running")
        await asyncio.sleep(0.1)
        events.append("done")

    await video_concurrency.submit("j1", _slow_task)
    await asyncio.sleep(0.02)  # let it start

    # Submit a second task — should queue
    s2 = await video_concurrency.submit("j2", _slow_task)
    assert s2 == "queued"

    await video_concurrency.drain()
    assert events == ["running", "done", "running", "done"]
    assert video_concurrency.active_count == 0
    assert video_concurrency.queue_size == 0


@pytest.mark.asyncio
async def test_queue_full_rejects():
    """When the wait queue is full, new submissions should be rejected."""
    controller = VideoConcurrencyController()
    controller.MAX_QUEUE_SIZE = 2

    # Start a long-running task to occupy the slot
    async def _long_task():
        await asyncio.sleep(10)

    await controller.submit("j-running", _long_task)
    await asyncio.sleep(0.01)

    # Fill the queue (set maxsize to 2 so 3rd submit is rejected)
    controller.max_queue_size = 2

    async def _noop():
        pass

    s1 = await controller.submit("j-q1", _noop)
    s2 = await controller.submit("j-q2", _noop)
    assert s1 == "queued"
    assert s2 == "queued"
    assert controller.queue_size == 2

    # Queue full — should reject
    s3 = await controller.submit("j-reject", _noop)
    assert s3 == "rejected"
    assert controller.queue_size == 2

    controller.reset()


@pytest.mark.asyncio
async def test_feature_gate_disabled_bypass():
    """When enabled=False, all tasks should bypass the guard and start immediately."""
    controller = VideoConcurrencyController()
    controller.enabled = False

    async def _slow_task():
        await asyncio.sleep(0.1)

    s1 = await controller.submit("j1", _slow_task)
    s2 = await controller.submit("j2", _slow_task)

    # Both should report "processing" (bypass mode)
    assert s1 == "processing"
    assert s2 == "processing"

    # Active count is still tracked even in bypass mode
    # (tasks increment/decrement via _run_with_release)
    await asyncio.sleep(0.05)

    controller.reset()


@pytest.mark.asyncio
async def test_memory_check_rejects_when_low():
    """When available memory is below threshold, submissions should reject."""
    controller = VideoConcurrencyController()
    controller.MEMORY_THRESHOLD_MB = 10000  # unrealistically high

    async def _task():
        pass

    status = await controller.submit("j-oom", _task)
    assert status == "rejected"

    controller.reset()


@patch("psutil.virtual_memory")
@pytest.mark.asyncio
async def test_memory_check_passes_when_high(mock_mem):
    """When available memory exceeds threshold, submissions proceed normally."""
    mock_mem.return_value.available = 4 * 1024 * 1024 * 1024  # 4GB available

    controller = VideoConcurrencyController()
    controller.MEMORY_THRESHOLD_MB = 512

    executed = []

    async def _task():
        executed.append(1)

    status = await controller.submit("j-ok", _task)
    assert status == "processing"

    await controller.drain()
    assert executed == [1]

    controller.reset()


@pytest.mark.asyncio
async def test_task_exception_releases_slot():
    """If a task raises an exception, the concurrency slot should still be released."""
    controller = VideoConcurrencyController()

    executed = []

    async def _failing_task():
        executed.append("fail")
        raise ValueError("boom")

    async def _next_task():
        executed.append("next")

    await controller.submit("j-fail", _failing_task)
    await asyncio.sleep(0.05)

    # Slot should be free — next task should start immediately
    s2 = await controller.submit("j-next", _next_task)
    assert s2 == "processing"

    await controller.drain()
    assert executed == ["fail", "next"]

    controller.reset()


@pytest.mark.asyncio
async def test_queue_status_properties():
    """Verify active_count and queue_size reflect real state."""
    controller = VideoConcurrencyController()

    async def _long():
        await asyncio.sleep(0.5)

    async def _noop():
        pass

    assert controller.active_count == 0
    assert controller.queue_size == 0

    await controller.submit("j1", _long)
    await asyncio.sleep(0.01)
    assert controller.active_count == 1
    assert controller.queue_size == 0

    await controller.submit("j2", _noop)
    assert controller.queue_size == 1

    controller.reset()


@pytest.mark.asyncio
async def test_drain_clears_queue():
    """drain() should wait for all tasks and clear the queue."""
    controller = VideoConcurrencyController()

    executed = []

    async def _task():
        executed.append(1)

    await controller.submit("j1", _task)
    await controller.submit("j2", _task)
    await controller.submit("j3", _task)

    await controller.drain()

    assert controller.active_count == 0
    assert controller.queue_size == 0
    assert executed == [1, 1, 1]

    controller.reset()


@pytest.mark.asyncio
async def test_reset_clears_all_state():
    """reset() should clear active count, queue, and re-enable the controller."""
    controller = VideoConcurrencyController()

    async def _long():
        await asyncio.sleep(1)

    controller.enabled = False
    await controller.submit("j1", _long)
    await asyncio.sleep(0.01)

    assert controller.active_count == 1

    controller.reset()
    assert controller.active_count == 0
    assert controller.queue_size == 0
    assert controller.enabled is True
