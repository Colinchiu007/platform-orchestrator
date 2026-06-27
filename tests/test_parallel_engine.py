"""Tests for auto_exec.parallel: WorkerPool, PhaseController, ParallelEngine."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from auto_exec.models import Task, TaskStatus
from auto_exec.parallel import (
    WorkerPool, PhaseController, ParallelEngine,
    ParallelConfig, WorkerSpec, WorkerStatus,
    InboxMessage, OutboxMessage, Heartbeat,
    ExecutionPhase, OUTBOX_FILE, INBOX_FILE, HEARTBEAT_FILE,
)


class TestWorkerSpec:
    def test_defaults(self):
        spec = WorkerSpec(task=Task(id=1, description="test"))
        assert spec.status == WorkerStatus.PENDING
        assert spec.worker_id != ""
        assert spec.result is None
        assert spec.error is None

    def test_with_status(self):
        spec = WorkerSpec(task=Task(id=1, description="test"), status=WorkerStatus.RUNNING)
        assert spec.status == WorkerStatus.RUNNING


class TestInboxOutboxMessages:
    def test_inbox_creation(self):
        msg = InboxMessage(type="task_assign", payload={"task_id": 1})
        assert msg.type == "task_assign"
        assert msg.payload["task_id"] == 1
        assert msg.msg_id != ""

    def test_outbox_creation(self):
        msg = OutboxMessage(type="result", payload={"summary": "done"})
        assert msg.type == "result"
        assert msg.payload["summary"] == "done"

    def test_outbox_with_reply(self):
        msg = OutboxMessage(type="progress", payload={}, in_reply_to="abc123")
        assert msg.in_reply_to == "abc123"

    def test_heartbeat(self):
        hb = Heartbeat(worker_id="w1", status="running")
        assert hb.worker_id == "w1"
        assert hb.memory_usage == 0


class TestWorkerPool:
    @pytest.fixture
    def pool(self, tmp_path):
        config = ParallelConfig(plan_dir=str(tmp_path / ".plan"))
        return WorkerPool(config)

    def test_create_worker(self, pool):
        task = Task(id=1, description="Parallel task")
        spec = pool.create_worker(task)
        assert spec.worker_id in pool._workers
        assert pool._workers[spec.worker_id].task.id == 1
        worker_dir = Path(spec.workspace)
        assert worker_dir.exists()
        assert (worker_dir / INBOX_FILE).exists()
        assert (worker_dir / HEARTBEAT_FILE).exists()
        inbox_content = (worker_dir / INBOX_FILE).read_text()
        assert "task_assign" in inbox_content
        assert "Parallel task" in inbox_content

    def test_create_multiple_workers(self, pool):
        t1 = Task(id=1, description="Task A")
        t2 = Task(id=2, description="Task B")
        w1 = pool.create_worker(t1)
        w2 = pool.create_worker(t2)
        assert w1.worker_id != w2.worker_id
        assert len(pool._workers) == 2

    def test_read_outbox_empty(self, pool):
        task = Task(id=1, description="test")
        spec = pool.create_worker(task)
        outbox = pool._read_outbox(spec.worker_id)
        assert outbox == []

    def test_write_and_read_outbox(self, pool):
        task = Task(id=1, description="test")
        spec = pool.create_worker(task)
        worker_dir = Path(spec.workspace)
        msg = OutboxMessage(type="result", payload={"summary": "task done"})
        with open(worker_dir / OUTBOX_FILE, "w") as f:
            f.write(msg.model_dump_json() + "\n")
        outbox = pool._read_outbox(spec.worker_id)
        assert len(outbox) == 1
        assert outbox[0].type == "result"
        assert outbox[0].payload["summary"] == "task done"

    def test_stale_worker_no_heartbeat(self, pool):
        task = Task(id=1, description="test")
        spec = pool.create_worker(task)
        assert not pool._is_stale(spec.worker_id)
        hb_file = Path(spec.workspace) / HEARTBEAT_FILE
        hb_file.unlink()
        assert pool._is_stale(spec.worker_id)

    def test_stale_worker_fresh_heartbeat(self, pool):
        task = Task(id=1, description="test")
        spec = pool.create_worker(task)
        worker_dir = Path(spec.workspace)
        pool._write_heartbeat(spec.worker_id, Heartbeat(
            worker_id=spec.worker_id, status="running",
        ))
        assert not pool._is_stale(spec.worker_id)

    def test_cancel_worker(self, pool):
        task = Task(id=1, description="test")
        spec = pool.create_worker(task)
        pool.cancel_worker(spec.worker_id)
        assert pool._workers[spec.worker_id].status == WorkerStatus.FAILED
        assert "Cancelled" in pool._workers[spec.worker_id].error

    def test_cleanup_single(self, pool):
        t1 = Task(id=1, description="test")
        t2 = Task(id=2, description="test2")
        w1 = pool.create_worker(t1)
        w2 = pool.create_worker(t2)
        pool.cleanup(w1.worker_id)
        assert w1.worker_id not in pool._workers
        assert w2.worker_id in pool._workers
        assert not Path(w1.workspace).exists()

    def test_cleanup_all(self, pool):
        pool.create_worker(Task(id=1, description="A"))
        pool.create_worker(Task(id=2, description="B"))
        pool.cleanup()
        assert len(pool._workers) == 0


class TestPhaseController:
    def test_initial_phase(self):
        pool = WorkerPool(ParallelConfig(plan_dir="/tmp/test-phase"))
        controller = PhaseController(pool)
        assert controller.infer_phase() == ExecutionPhase.INITIALIZING
        assert not controller.is_complete()
        assert controller.success_rate() == 0.0

    def test_dispatching_phase(self, tmp_path):
        config = ParallelConfig(plan_dir=str(tmp_path / ".plan-w"))
        pool = WorkerPool(config)
        pool.create_worker(Task(id=1, description="A"))
        pool.create_worker(Task(id=2, description="B"))
        controller = PhaseController(pool)
        assert controller.infer_phase() == ExecutionPhase.DISPATCHING

    def test_workers_running_phase(self, tmp_path):
        config = ParallelConfig(plan_dir=str(tmp_path / ".plan-w2"))
        pool = WorkerPool(config)
        w = pool.create_worker(Task(id=1, description="A"))
        w.status = WorkerStatus.RUNNING
        controller = PhaseController(pool)
        assert controller.infer_phase() == ExecutionPhase.WORKERS_RUNNING

    def test_merging_phase_all_completed(self, tmp_path):
        config = ParallelConfig(plan_dir=str(tmp_path / ".plan-w3"))
        pool = WorkerPool(config)
        w1 = pool.create_worker(Task(id=1, description="A"))
        w2 = pool.create_worker(Task(id=2, description="B"))
        w1.status = WorkerStatus.COMPLETED
        w2.status = WorkerStatus.COMPLETED
        controller = PhaseController(pool)
        assert controller.infer_phase() == ExecutionPhase.MERGING
        assert controller.is_complete()
        assert controller.success_rate() == 1.0

    def test_merging_with_failures(self, tmp_path):
        config = ParallelConfig(plan_dir=str(tmp_path / ".plan-w4"))
        pool = WorkerPool(config)
        w1 = pool.create_worker(Task(id=1, description="A"))
        w2 = pool.create_worker(Task(id=2, description="B"))
        w1.status = WorkerStatus.COMPLETED
        w2.status = WorkerStatus.FAILED
        controller = PhaseController(pool)
        assert controller.infer_phase() == ExecutionPhase.MERGING
        assert controller.success_rate() == 0.5


class TestParallelEngine:
    @pytest.mark.asyncio
    async def test_run_empty_tasks(self):
        engine = ParallelEngine()
        results = await engine.run([])
        assert results == []

    @pytest.mark.asyncio
    async def test_run_single_task(self, tmp_path):
        config = ParallelConfig(plan_dir=str(tmp_path / ".plan"), worker_timeout=3)
        engine = ParallelEngine(config)
        tasks = [Task(id=1, description="Independent task")]
        results = await engine.run(tasks)
        assert len(results) == 1
        assert results[0].status in (WorkerStatus.FAILED, WorkerStatus.STALE)
        engine.pool.cleanup()

    @pytest.mark.asyncio
    async def test_run_with_plan_nonexistent(self):
        engine = ParallelEngine()
        with pytest.raises(FileNotFoundError):
            await engine.run_with_plan("/tmp/nonexistent-plan.md")

    @pytest.mark.asyncio
    async def test_run_with_plan_no_independent(self, tmp_path):
        """All pending tasks have circular/unsatisfied dependencies."""
        plan_path = Path(tmp_path / "task_plan.md")
        plan_path.write_text(
            "| # | Task | Dep | Risk | PRD | Test | Status |\n"
            "|---|------|------|------|------|------|------|\n"
            "| 1 | Dep task | 2 | low | no | yes | pending |\n"
            "| 2 | Another dep | 1 | low | no | yes | pending |\n"
            "| 3 | Last task | - | low | no | yes | completed |\n"
        )
        config = ParallelConfig(plan_dir=str(tmp_path / ".plan-p"), worker_timeout=3)
        engine = ParallelEngine(config)
        results = await engine.run_with_plan(str(plan_path))
        assert results == []

    @pytest.mark.asyncio
    async def test_worker_pool_monitor_timeout(self, tmp_path):
        config = ParallelConfig(
            plan_dir=str(tmp_path / ".plan-t"),
            worker_timeout=2,
            poll_interval=0.2,
        )
        pool = WorkerPool(config)
        task = Task(id=1, description="Quick task")
        spec = pool.create_worker(task)
        spec.status = WorkerStatus.RUNNING
        result = await pool.monitor_worker(spec.worker_id)
        assert result.status in (WorkerStatus.FAILED, WorkerStatus.STALE)
        pool.cleanup()

    @pytest.mark.asyncio
    async def test_worker_pool_monitor_success(self, tmp_path):
        config = ParallelConfig(
            plan_dir=str(tmp_path / ".plan-ok"),
            worker_timeout=5,
            poll_interval=0.1,
        )
        pool = WorkerPool(config)
        task = Task(id=1, description="Success task")
        spec = pool.create_worker(task)
        spec.status = WorkerStatus.RUNNING
        worker_dir = Path(spec.workspace)
        msg = OutboxMessage(type="result", payload={"summary": "all good"})
        with open(worker_dir / OUTBOX_FILE, "w") as f:
            f.write(msg.model_dump_json() + "\n")
        result = await pool.monitor_worker(spec.worker_id)
        assert result.status == WorkerStatus.COMPLETED
        assert result.result == "all good"
        pool.cleanup()

    @pytest.mark.asyncio
    async def test_worker_pool_monitor_error(self, tmp_path):
        config = ParallelConfig(
            plan_dir=str(tmp_path / ".plan-err"),
            worker_timeout=5,
            poll_interval=0.1,
        )
        pool = WorkerPool(config)
        task = Task(id=1, description="Failing task")
        spec = pool.create_worker(task)
        spec.status = WorkerStatus.RUNNING
        worker_dir = Path(spec.workspace)
        msg = OutboxMessage(type="error", payload={"error": "something broke"})
        with open(worker_dir / OUTBOX_FILE, "w") as f:
            f.write(msg.model_dump_json() + "\n")
        result = await pool.monitor_worker(spec.worker_id)
        assert result.status == WorkerStatus.FAILED
