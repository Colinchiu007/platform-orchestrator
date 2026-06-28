"""Parallel multi-agent execution engine for auto-exec.

Enables running multiple independent tasks concurrently via worker
workspaces with JSONL-based IPC (inbox/outbox/heartbeat).

oh-my-claudecode inspired pattern:
    WorkerPool -> manages individual workers
    PhaseController -> infers execution phase from worker states
    ParallelEngine -> coordinates the full parallel run
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from auto_exec.models import Task, TaskStatus

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

WORKERS_DIR = ".plan/workers"
HEARTBEAT_FILE = "heartbeat.jsonl"
INBOX_FILE = "inbox.jsonl"
OUTBOX_FILE = "outbox.jsonl"
HEARTBEAT_INTERVAL = 15  # seconds between heartbeats
STALE_WORKER_TIMEOUT = 60  # seconds without heartbeat → stale


# ── Enums ────────────────────────────────────────────────────────────────────


class WorkerStatus(str, Enum):
    """Status of a single worker."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    STALE = "stale"
    CANCELLED = "cancelled"


class ExecutionPhase(str, Enum):
    """Overall execution phase inferred from all workers."""

    INITIALIZING = "initializing"
    DISPATCHING = "dispatching"
    WORKERS_RUNNING = "workers_running"
    MERGING = "merging"
    COMPLETE = "complete"
    FAILED = "failed"


# ── IPC Models ───────────────────────────────────────────────────────────────


class Heartbeat(BaseModel):
    """Heartbeat from a worker process."""

    worker_id: str
    status: str
    memory_usage: int = 0
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class InboxMessage(BaseModel):
    """Message sent TO a worker (task assignment, cancel, etc.)."""

    type: str  # "task_assign" | "cancel" | "ping"
    payload: dict = Field(default_factory=dict)
    msg_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])


class OutboxMessage(BaseModel):
    """Message sent FROM a worker (progress, result, error, etc.)."""

    type: str  # "result" | "progress" | "error" | "pong"
    payload: dict = Field(default_factory=dict)
    in_reply_to: Optional[str] = None
    msg_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])


# ── WorkerSpec ───────────────────────────────────────────────────────────────


class WorkerSpec(BaseModel):
    """Specification for a single worker."""

    worker_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    task: Task
    status: WorkerStatus = WorkerStatus.PENDING
    workspace: str = ""
    result: Optional[str] = None
    error: Optional[str] = None


# ── ParallelConfig ───────────────────────────────────────────────────────────


class ParallelConfig(BaseModel):
    """Configuration for parallel execution."""

    plan_dir: str = ".plan"
    max_concurrent: int = 3
    worker_timeout: int = 300  # seconds before marking a worker stale
    poll_interval: float = 0.5  # seconds between polls
    heartbeat_timeout: int = STALE_WORKER_TIMEOUT


# ── WorkerPool ───────────────────────────────────────────────────────────────


class WorkerPool:
    """Manages worker workspaces with JSONL-based IPC.

    Each worker gets a dedicated workspace directory with:
        inbox.jsonl   <- tasks/commands from coordinator
        outbox.jsonl  -> results/progress to coordinator
        heartbeat.jsonl -> periodic liveness signal

    Usage:
        pool = WorkerPool(config)
        spec = pool.create_worker(task)
        result = await pool.monitor_worker(spec.worker_id)
        pool.cleanup()
    """

    def __init__(self, config: ParallelConfig) -> None:
        self.config = config
        self._workers: dict[str, WorkerSpec] = {}
        self._base_dir = Path(config.plan_dir) / "workers"
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def create_worker(self, task: Task) -> WorkerSpec:
        """Create a new worker workspace with IPC files.

        Returns the WorkerSpec with the generated worker_id and workspace path.
        The workspace directory is created with empty inbox, outbox, and
        an initial heartbeat file.
        """
        spec = WorkerSpec(task=task)
        spec.workspace = str(self._base_dir / spec.worker_id)
        workspace = Path(spec.workspace)
        workspace.mkdir(parents=True, exist_ok=True)

        # Write initial inbox with task assignment
        assign_msg = InboxMessage(type="task_assign", payload={
            "task_id": task.id,
            "description": task.description,
            "dependencies": task.dependencies,
        })
        with open(workspace / INBOX_FILE, "w") as f:
            f.write(assign_msg.model_dump_json() + "\n")

        # Create empty outbox
        (workspace / OUTBOX_FILE).touch()

        self._workers[spec.worker_id] = spec

        # Write initial heartbeat (after adding to _workers so _write_heartbeat can find it)
        self._write_heartbeat(spec.worker_id, Heartbeat(
            worker_id=spec.worker_id, status="pending",
        ))
        logger.info("Created worker %s for task %d", spec.worker_id, task.id)
        return spec

    async def monitor_worker(self, worker_id: str) -> WorkerSpec:
        """Monitor a worker until completion or timeout.

        Polls the outbox and heartbeat files. Returns the updated WorkerSpec.
        """
        spec = self._workers.get(worker_id)
        if spec is None:
            raise ValueError(f"Unknown worker: {worker_id}")

        worker_dir = Path(spec.workspace)
        deadline = time.time() + self.config.worker_timeout

        while time.time() < deadline:
            # Check outbox for result/error
            outbox = self._read_outbox(worker_id)
            for msg in outbox:
                if msg.type == "result":
                    spec.status = WorkerStatus.COMPLETED
                    spec.result = msg.payload.get("summary", "")
                    return spec
                if msg.type == "error":
                    spec.status = WorkerStatus.FAILED
                    spec.error = msg.payload.get("error", "Unknown error")
                    return spec

            # Check if stale (no recent heartbeat)
            if self._is_stale(worker_id):
                spec.status = WorkerStatus.STALE
                spec.error = "Worker heartbeat timed out"
                return spec

            await asyncio.sleep(self.config.poll_interval)

        # Timeout
        spec.status = WorkerStatus.FAILED
        spec.error = f"Monitor timed out after {self.config.worker_timeout}s"
        return spec

    def cancel_worker(self, worker_id: str) -> None:
        """Mark a worker as cancelled."""
        spec = self._workers.get(worker_id)
        if spec is None:
            raise ValueError(f"Unknown worker: {worker_id}")
        spec.status = WorkerStatus.FAILED
        spec.error = "Cancelled by coordinator"

    def cleanup(self, worker_id: Optional[str] = None) -> None:
        """Remove a worker workspace, or all if worker_id is None."""
        if worker_id:
            spec = self._workers.pop(worker_id, None)
            if spec and Path(spec.workspace).exists():
                import shutil
                shutil.rmtree(spec.workspace, ignore_errors=True)
        else:
            for wid in list(self._workers.keys()):
                self.cleanup(wid)

    # ── Internal Methods ──────────────────────────────────────────────

    def _read_outbox(self, worker_id: str) -> list[OutboxMessage]:
        """Read all messages from a worker's outbox."""
        spec = self._workers.get(worker_id)
        if spec is None:
            return []
        outbox_path = Path(spec.workspace) / OUTBOX_FILE
        if not outbox_path.exists():
            return []

        messages: list[OutboxMessage] = []
        try:
            content = outbox_path.read_text(encoding="utf-8")
            for line in content.strip().splitlines():
                if line.strip():
                    data = json.loads(line)
                    messages.append(OutboxMessage(**data))
        except (json.JSONDecodeError, OSError):
            pass
        return messages

    def _read_heartbeats(self, worker_id: str) -> list[Heartbeat]:
        """Read all heartbeats from a worker."""
        spec = self._workers.get(worker_id)
        if spec is None:
            return []
        hb_path = Path(spec.workspace) / HEARTBEAT_FILE
        if not hb_path.exists():
            return []

        heartbeats: list[Heartbeat] = []
        try:
            content = hb_path.read_text(encoding="utf-8")
            for line in content.strip().splitlines():
                if line.strip():
                    data = json.loads(line)
                    heartbeats.append(Heartbeat(**data))
        except (json.JSONDecodeError, OSError):
            pass
        return heartbeats

    def _write_heartbeat(self, worker_id: str, hb: Heartbeat) -> None:
        """Append a heartbeat to the worker's heartbeat file."""
        spec = self._workers.get(worker_id)
        if spec is None:
            return
        hb_path = Path(spec.workspace) / HEARTBEAT_FILE
        with open(hb_path, "a") as f:
            f.write(hb.model_dump_json() + "\n")

    def _is_stale(self, worker_id: str) -> bool:
        """Check if a worker's most recent heartbeat is too old.

        Returns True if no heartbeat file exists, or the latest
        heartbeat's timestamp is older than heartbeat_timeout.
        """
        heartbeats = self._read_heartbeats(worker_id)
        if not heartbeats:
            return True

        latest = heartbeats[-1]
        try:
            hb_time = datetime.fromisoformat(latest.timestamp)
            age = (datetime.now(timezone.utc) - hb_time.replace(tzinfo=timezone.utc)).total_seconds()
            return age > self.config.heartbeat_timeout
        except (ValueError, TypeError):
            return True


# ── PhaseController ──────────────────────────────────────────────────────────


class PhaseController:
    """Infers the execution phase from the WorkerPool state.

    States:
        INITIALIZING    — No workers created yet
        DISPATCHING     — Workers created but not yet running
        WORKERS_RUNNING — At least one worker is running
        MERGING         — All workers completed/failed, merging results
        COMPLETE        — All workers completed successfully
        FAILED          — All workers failed

    Usage:
        controller = PhaseController(pool)
        phase = controller.infer_phase()
        if controller.is_complete(): ...
    """

    def __init__(self, pool: WorkerPool) -> None:
        self.pool = pool

    def infer_phase(self) -> ExecutionPhase:
        """Infer the current execution phase from worker states."""
        workers = list(self.pool._workers.values())
        if not workers:
            return ExecutionPhase.INITIALIZING

        statuses = {w.status for w in workers}

        if all(s == WorkerStatus.PENDING for s in statuses):
            return ExecutionPhase.DISPATCHING

        if any(s == WorkerStatus.RUNNING for s in statuses):
            return ExecutionPhase.WORKERS_RUNNING

        if all(s in (WorkerStatus.COMPLETED, WorkerStatus.FAILED, WorkerStatus.STALE) for s in statuses):
            return ExecutionPhase.MERGING

        if all(s == WorkerStatus.COMPLETED for s in statuses):
            return ExecutionPhase.COMPLETE

        if all(s in (WorkerStatus.FAILED, WorkerStatus.STALE) for s in statuses):
            return ExecutionPhase.FAILED

        return ExecutionPhase.WORKERS_RUNNING

    def is_complete(self) -> bool:
        """Check if all workers have completed (successfully or not)."""
        phase = self.infer_phase()
        return phase in (ExecutionPhase.MERGING, ExecutionPhase.COMPLETE, ExecutionPhase.FAILED)

    def success_rate(self) -> float:
        """Calculate the fraction of workers that completed successfully."""
        workers = list(self.pool._workers.values())
        if not workers:
            return 0.0
        completed = sum(1 for w in workers if w.status == WorkerStatus.COMPLETED)
        return completed / len(workers)


# ── ParallelEngine ───────────────────────────────────────────────────────────


class ParallelEngine:
    """Coordinates parallel execution of multiple tasks.

    Usage:
        engine = ParallelEngine()
        results = await engine.run(tasks)
        # or
        results = await engine.run_with_plan("path/to/task_plan.md")
    """

    def __init__(self, config: Optional[ParallelConfig] = None) -> None:
        self.config = config or ParallelConfig()
        self.pool = WorkerPool(self.config)

    async def run(self, tasks: list[Task]) -> list[WorkerSpec]:
        """Execute a list of tasks in parallel (max max_concurrent).

        Tasks are dispatched to workers, each worker is monitored
        concurrently. Returns the list of WorkerSpec results.
        """
        if not tasks:
            return []

        results: list[WorkerSpec] = []
        pending = list(tasks)
        running: list[asyncio.Task] = []

        while pending or running:
            # Fill up to max_concurrent
            while pending and len(running) < self.config.max_concurrent:
                task = pending.pop(0)
                spec = self.pool.create_worker(task)
                spec.status = WorkerStatus.RUNNING
                monitor_task = asyncio.create_task(
                    self.pool.monitor_worker(spec.worker_id)
                )
                running.append(monitor_task)

            if not running:
                break

            # Wait for one to finish
            done, running = await asyncio.wait(
                running, return_when=asyncio.FIRST_COMPLETED,
            )
            for done_task in done:
                result = done_task.result()
                results.append(result)

        return results

    async def run_with_plan(self, plan_file: str) -> list[WorkerSpec]:
        """Parse a task plan file and execute all independent tasks.

        Reads the markdown plan, filters for tasks with no unsatisfied
        dependencies, and runs them in parallel.
        """
        path = Path(plan_file)
        if not path.exists():
            raise FileNotFoundError(f"Plan file not found: {plan_file}")

        # Parse tasks from the plan
        from auto_exec.orchestrator import Orchestrator

        orch = Orchestrator()
        orch._task_plan_path = path
        content = path.read_text(encoding="utf-8")
        all_tasks = orch._parse_tasks_from_plan(content)

        # Filter for pending tasks with completed dependencies
        completed_ids = {t.id for t in all_tasks if t.status == TaskStatus.COMPLETED}
        independent = [
            t for t in all_tasks
            if t.status == TaskStatus.PENDING
            and all(d in completed_ids for d in (t.dependencies or []))
        ]

        if not independent:
            return []

        return await self.run(independent)
