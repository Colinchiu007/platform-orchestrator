"""Pydantic models for auto-exec: Task, TaskStatus, ExecutionResult, ConversationTurn."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    """Execution status of a single task."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class Task(BaseModel):
    """A single atomic task in the plan."""

    id: int
    description: str
    status: TaskStatus = TaskStatus.PENDING
    dependencies: list[int] = Field(default_factory=list)
    result: Optional[str] = None
    error: Optional[str] = None
    risk: str = "low"
    prd_impact: bool = False
    test_required: bool = True


class ExecutionResult(BaseModel):
    """Result of a full orchestrator run."""

    total: int = 0
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0
    summary: str = ""


class ConversationTurn(BaseModel):
    """A single conversation turn for memory management."""

    role: str  # "user" | "assistant" | "system"
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now().isoformat())
    task_id: Optional[int] = None
