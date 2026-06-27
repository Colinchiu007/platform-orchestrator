"""Tests for auto_exec: models, task parsing, execution, progress, memory."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from auto_exec.models import (
    ConversationTurn,
    ExecutionResult,
    Task,
    TaskStatus,
)
from auto_exec.orchestrator import Orchestrator, OrchestratorConfig


class TestTask:
    def test_default_status(self):
        t = Task(id=1, description="test")
        assert t.status == TaskStatus.PENDING
        assert t.dependencies == []
        assert t.risk == "low"

    def test_with_all_fields(self):
        t = Task(id=2, description="test full", status=TaskStatus.RUNNING,
                 dependencies=[1], risk="high", prd_impact=True)
        assert t.status == TaskStatus.RUNNING
        assert 1 in t.dependencies
        assert t.prd_impact is True


class TestExecutionResult:
    def test_default_values(self):
        r = ExecutionResult()
        assert r.total == 0
        assert r.completed == 0
        assert r.duration_seconds == 0.0

    def test_summary_with_counts(self):
        r = ExecutionResult(total=5, completed=3, failed=1, skipped=1)
        r.summary = "3/5 completed"
        assert r.summary == "3/5 completed"


class TestConversationTurn:
    def test_default_timestamp(self):
        turn = ConversationTurn(role="user", content="hello")
        assert turn.role == "user"
        assert turn.content == "hello"
        assert turn.task_id is None

    def test_with_task_id(self):
        turn = ConversationTurn(role="assistant", content="result", task_id=1)
        assert turn.task_id == 1


class TestTaskParsing:
    def test_parse_table_format(self, tmp_path):
        orch = Orchestrator(OrchestratorConfig(plan_dir=str(tmp_path)))
        content = (
            "| # | Task | Dep | Risk | PRD | Test | Status |\n"
            "|---|------|------|------|------|------|------|\n"
            "| 1 | First task | - | low | no | yes | pending |\n"
            "| 2 | Second task | 1 | low | no | yes | pending |\n"
        )
        tasks = orch._parse_tasks_from_plan(content)
        assert len(tasks) == 2
        assert tasks[0].id == 1
        assert tasks[0].description == "First task"
        assert tasks[1].dependencies == [1]

    def test_parse_checklist_format(self, tmp_path):
        orch = Orchestrator(OrchestratorConfig(plan_dir=str(tmp_path)))
        content = "- [ ] Task one\n- [x] Task two\n- [ ] Task three\n"
        tasks = orch._parse_tasks_from_plan(content)
        assert len(tasks) == 3
        assert tasks[1].status == TaskStatus.COMPLETED
        assert tasks[0].status == TaskStatus.PENDING

    def test_parse_completed_status(self, tmp_path):
        orch = Orchestrator(OrchestratorConfig(plan_dir=str(tmp_path)))
        content = (
            "| # | Task | Dep | Risk | PRD | Test | Status |\n"
            "|---|------|------|------|------|------|------|\n"
            "| 1 | Done task | - | low | no | yes | done |\n"
        )
        tasks = orch._parse_tasks_from_plan(content)
        assert tasks[0].status == TaskStatus.COMPLETED


class TestOrchestrator:
    def test_find_next_task_basic(self, tmp_path):
        plan_dir = tmp_path / ".plan"
        plan_dir.mkdir()
        plan_file = plan_dir / "task_plan.md"
        plan_file.write_text(
            "| # | Task | Dep | Risk | PRD | Test | Status |\n"
            "|---|------|------|------|------|------|------|\n"
            "| 1 | First | - | low | no | yes | pending |\n"
            "| 2 | Second | 1 | low | no | yes | pending |\n",
            encoding="utf-8",
        )

        orch = Orchestrator(OrchestratorConfig(plan_dir=str(plan_dir)))
        next_task = orch._find_next_task()
        assert next_task is not None
        assert next_task.id == 1

    def test_find_next_task_dependency_not_met(self, tmp_path):
        plan_dir = tmp_path / ".plan2"
        plan_dir.mkdir()
        plan_file = plan_dir / "task_plan.md"
        plan_file.write_text(
            "| # | Task | Dep | Risk | PRD | Test | Status |\n"
            "|---|------|------|------|------|------|------|\n"
            "| 1 | First | - | low | no | yes | pending |\n"
            "| 2 | Second | 1 | low | no | yes | pending |\n",
            encoding="utf-8",
        )

        orch = Orchestrator(OrchestratorConfig(plan_dir=str(plan_dir)))
        next_task = orch._find_next_task()
        assert next_task is not None
        assert next_task.id == 1

    def test_find_next_task_no_pending(self, tmp_path):
        plan_dir = tmp_path / ".plan3"
        plan_dir.mkdir()
        plan_file = plan_dir / "task_plan.md"
        plan_file.write_text(
            "| # | Task | Dep | Risk | PRD | Test | Status |\n"
            "|---|------|------|------|------|------|------|\n"
            "| 1 | Done | - | low | no | yes | done |\n",
            encoding="utf-8",
        )

        orch = Orchestrator(OrchestratorConfig(plan_dir=str(plan_dir)))
        next_task = orch._find_next_task()
        assert next_task is None


class TestExecution:
    @pytest.mark.asyncio
    async def test_execute_task_success(self, tmp_path):
        plan_dir = tmp_path / ".plan"
        plan_dir.mkdir()
        plan_file = plan_dir / "task_plan.md"
        plan_file.write_text(
            "| # | Task | Dep | Risk | PRD | Test | Status |\n"
            "|---|------|------|------|------|------|------|\n"
            "| 1 | Test task | - | low | no | yes | pending |\n",
            encoding="utf-8",
        )

        config = OrchestratorConfig(plan_dir=str(plan_dir))
        orch = Orchestrator(config)

        with patch.object(orch, "_call_llm", new_callable=AsyncMock) as mock:
            mock.return_value = "Task completed successfully"
            task = await orch.run_single(1)
            assert task.status == TaskStatus.COMPLETED
            assert task.result == "Task completed successfully"

    @pytest.mark.asyncio
    async def test_execute_task_http_retry(self, tmp_path):
        config = OrchestratorConfig(plan_dir=str(tmp_path), max_retries=2)
        orch = Orchestrator(config)
        task = Task(id=1, description="Retry test")

        with patch.object(orch, "_call_llm", new_callable=AsyncMock) as mock:
            mock.side_effect = httpx.RequestError("Timeout")
            await orch._execute_task(task)
            assert task.status == TaskStatus.FAILED
            assert "Timeout" in task.error

    @pytest.mark.asyncio
    async def test_execute_task_non_http_error(self, tmp_path):
        config = OrchestratorConfig(plan_dir=str(tmp_path))
        orch = Orchestrator(config)
        task = Task(id=1, description="Error test")

        with patch.object(orch, "_call_llm", new_callable=AsyncMock) as mock:
            mock.side_effect = ValueError("Logic error")
            with pytest.raises(ValueError):
                await orch._execute_task(task)
            assert task.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_retry_after_timeout(self, tmp_path):
        config = OrchestratorConfig(plan_dir=str(tmp_path), max_retries=3)
        orch = Orchestrator(config)
        task = Task(id=1, description="Timeout task")

        with patch.object(orch, "_call_llm", new_callable=AsyncMock) as mock:
            from httpx import TimeoutException

            mock.side_effect = TimeoutException("timeout")
            await orch._execute_task(task)
            assert task.status == TaskStatus.FAILED
            assert mock.call_count == 3


class TestBuildTaskPrompt:
    def test_build_task_prompt_basic(self, tmp_path):
        config = OrchestratorConfig(plan_dir=str(tmp_path))
        orch = Orchestrator(config)
        task = Task(id=1, description="Run tests")
        prompt = orch._build_task_prompt(task)

        assert "Run tests" in prompt
        assert "pytest" in prompt
        assert "Conventional Commits" in prompt

    def test_build_task_prompt_with_deps(self, tmp_path):
        config = OrchestratorConfig(plan_dir=str(tmp_path))
        orch = Orchestrator(config)
        task = Task(id=2, description="Implement feature", dependencies=[1])
        prompt = orch._build_task_prompt(task)

        assert "Implement feature" in prompt


class TestReadWriteProgress:
    def test_read_progress_empty(self, tmp_path):
        config = OrchestratorConfig(plan_dir=str(tmp_path))
        orch = Orchestrator(config)
        progress = orch._read_file(orch._progress_path)
        assert progress is None

    def test_update_progress(self, tmp_path):
        plan_dir = tmp_path / ".plan"
        plan_dir.mkdir()
        config = OrchestratorConfig(plan_dir=str(plan_dir))
        orch = Orchestrator(config)
        task = Task(id=1, description="Progress task")

        import asyncio
        asyncio.run(orch._update_progress(task, "running"))

        content = (plan_dir / "progress.md").read_text(encoding="utf-8")
        assert "Progress task" in content
        assert "running" in content


class TestReadFile:
    def test_read_file_exists(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("hello", encoding="utf-8")
        content = Orchestrator._read_file(f)
        assert content == "hello"

    def test_read_file_not_exists(self):
        config = OrchestratorConfig()
        orch = Orchestrator(config)
        content = orch._read_file(Path("/tmp/nonexistent_file_xyz"))
        assert content is None


class TestCallLLMMissingKey:
    @pytest.mark.asyncio
    async def test_missing_api_key(self, tmp_path):
        config = OrchestratorConfig(plan_dir=str(tmp_path), llm_api_key="")
        orch = Orchestrator(config)
        with pytest.raises(RuntimeError, match="No LLM API key configured"):
            await orch._call_llm([{"role": "user", "content": "test"}])
