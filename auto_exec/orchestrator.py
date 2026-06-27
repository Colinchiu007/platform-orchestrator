"""Code-level auto-exec planning loop.

Replaces the prompt-based scheduled task worker with a proper Python
Orchestrator that reads .plan/task_plan.md, executes via LLM, and
updates .plan/progress.md — all within a single process.

CowAgent-inspired AgentStreamExecutor pattern:
    Identify next task → build prompt → call LLM → update progress → repeat.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx

from auto_exec.models import ExecutionResult, Task, TaskStatus

logger = logging.getLogger(__name__)

DEFAULT_PLAN_DIR = ".plan"
DEFAULT_MAX_RETRIES = 3
DEFAULT_CONTEXT_LIMIT = 64000


class OrchestratorConfig:
    """Configuration for the Orchestrator planning loop.

    Attributes:
        plan_dir: Path to the .plan/ directory.
        max_retries: Max LLM call retries per task (exponential backoff).
        context_limit: Token limit before context trimming.
        llm_model: Model name to use for LLM calls.
        llm_base_url: API base URL.
        llm_api_key: API key (from env if not set).
        guideline_dirs: Directories to scan for CLAUDE.md, .clinerules, AGENTS.md.
    """

    def __init__(
        self,
        plan_dir: str = DEFAULT_PLAN_DIR,
        max_retries: int = DEFAULT_MAX_RETRIES,
        context_limit: int = DEFAULT_CONTEXT_LIMIT,
        llm_model: str = "",
        llm_base_url: str = "",
        llm_api_key: str = "",
        guideline_dirs: Optional[list[str]] = None,
    ) -> None:
        self.plan_dir = plan_dir
        self.max_retries = max_retries
        self.context_limit = context_limit
        self.llm_model = llm_model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.llm_base_url = llm_base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.llm_api_key = llm_api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("PO_OPENAI_API_KEY", "")
        self.guideline_dirs = guideline_dirs or []


class Orchestrator:
    """Main planning loop: identify next task → execute → update progress → repeat.

    Usage:
        orch = Orchestrator()
        result = await orch.run()  # Full auto-exec loop
        # or
        task = await orch.run_single(1)  # One-off task execution
    """

    def __init__(self, config: Optional[OrchestratorConfig] = None) -> None:
        self.config = config or OrchestratorConfig()
        self._plan_dir = Path(self.config.plan_dir)
        self._task_plan_path = self._plan_dir / "task_plan.md"
        self._progress_path = self._plan_dir / "progress.md"
        self._messages: list[dict] = []

    # ── Public API ────────────────────────────────────────────────────

    async def run(self) -> ExecutionResult:
        """Execute the full planning loop.

        Reads the task plan, identifies pending tasks with satisfied
        dependencies, executes them one at a time, and updates progress.
        """
        start_time = time.time()
        result = ExecutionResult()

        while True:
            task = self._find_next_task()
            if task is None:
                break

            try:
                await self._execute_task(task)
                result.completed += 1
            except Exception as e:
                logger.error("Task %d failed: %s", task.id, e)
                task.status = TaskStatus.FAILED
                task.error = str(e)
                result.failed += 1

            result.total += 1
            await self._update_progress(task)

        result.duration_seconds = time.time() - start_time
        result.summary = (
            f"{result.completed}/{result.total} tasks completed"
            f" ({result.failed} failed, {result.skipped} skipped)"
        )
        return result

    async def run_single(self, task_id: int) -> Task:
        """Execute a single task by ID (for manual or one-off use).

        Parses the current task plan, finds the task, executes it, and
        updates progress. Returns the updated Task.
        """
        content = self._read_file(self._task_plan_path)
        if not content:
            raise FileNotFoundError(f"Task plan not found: {self._task_plan_path}")

        tasks = self._parse_tasks_from_plan(content)
        task = next((t for t in tasks if t.id == task_id), None)
        if task is None:
            raise ValueError(f"Task {task_id} not found in plan")

        await self._execute_task(task)
        await self._update_progress(task)
        return task

    # ── Task Identification ───────────────────────────────────────────

    def _find_next_task(self) -> Optional[Task]:
        """Find the next pending task whose dependencies are all completed.

        Scans the current task_plan.md, parses all tasks, then returns
        the first pending task whose dependencies are all 'completed'.
        """
        content = self._read_file(self._task_plan_path)
        if not content:
            return None

        tasks = self._parse_tasks_from_plan(content)
        completed_ids = {t.id for t in tasks if t.status == TaskStatus.COMPLETED}

        for task in tasks:
            if task.status == TaskStatus.PENDING:
                deps = task.dependencies or []
                if all(d in completed_ids for d in deps):
                    return task

        return None

    def _parse_tasks_from_plan(self, content: str) -> list[Task]:
        """Parse markdown task plan into Task objects.

        Supports two formats:
            1. Table format (| # | Task | Dep | Risk | PRD | Test | Status |)
            2. Checklist format (- [ ] Task description)
        """
        if "| # | Task" in content or "| # |" in content:
            return self._parse_table_format(content)
        return self._parse_checklist_format(content)

    def _parse_table_format(self, content: str) -> list[Task]:
        """Parse | # | Task | Dep | Risk | PRD | Test | Status | table."""
        tasks: list[Task] = []
        for line in content.splitlines():
            line = line.strip()
            if not line.startswith("|") or not line.endswith("|"):
                continue
            cells = [c.strip() for c in line.split("|")]
            cells = [c for c in cells if c]  # Remove empty first/last

            if len(cells) < 6:
                continue
            try:
                task_id = int(cells[0])
            except ValueError:
                continue

            description = cells[1]
            dep_str = cells[2] if len(cells) > 2 else "-"
            risk = cells[3] if len(cells) > 3 else "low"
            prd_raw = cells[4] if len(cells) > 4 else "no"
            status_raw = cells[6] if len(cells) > 6 else "pending"

            # Parse dependencies
            dependencies: list[int] = []
            for part in dep_str.split(","):
                part = part.strip()
                if part == "-":
                    continue
                try:
                    dependencies.append(int(part))
                except ValueError:
                    pass

            # Parse status
            status = TaskStatus.PENDING
            raw_lower = status_raw.strip().lower()
            if raw_lower in ("done", "completed", "✓", "x", "完成"):
                status = TaskStatus.COMPLETED
            elif raw_lower in ("failed", "✗"):
                status = TaskStatus.FAILED
            elif raw_lower in ("running", "in progress", "进行中"):
                status = TaskStatus.RUNNING
            elif raw_lower in ("skipped", "跳过"):
                status = TaskStatus.SKIPPED
            elif raw_lower in ("blocked", "阻塞"):
                status = TaskStatus.BLOCKED

            tasks.append(Task(
                id=task_id,
                description=description,
                status=status,
                dependencies=dependencies,
                risk=risk,
                prd_impact=prd_raw.lower() in ("yes", "true", "y"),
                test_required=True,
            ))

        return tasks

    def _parse_checklist_format(self, content: str) -> list[Task]:
        """Parse '- [ ] Task description' checklist format."""
        tasks: list[Task] = []
        task_id = 0
        for line in content.splitlines():
            line = line.strip()
            match = re.match(r"^- \[([ xX])\] (.+)$", line)
            if not match:
                continue
            task_id += 1
            checked = match.group(1).strip().lower() == "x"
            description = match.group(2).strip()
            tasks.append(Task(
                id=task_id,
                description=description,
                status=TaskStatus.COMPLETED if checked else TaskStatus.PENDING,
            ))
        return tasks

    # ── Task Execution ────────────────────────────────────────────────

    async def _execute_task(self, task: Task) -> None:
        """Execute a single task via LLM call with retry logic.

        Builds a system prompt that includes project guidelines, then
        calls the LLM. The LLM response is interpreted as the task output.
        """
        task.status = TaskStatus.RUNNING
        await self._update_progress(task)
        logger.info("Executing task %d: %s", task.id, task.description)

        prompt = self._build_task_prompt(task)
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"Execute task {task.id}: {task.description}"},
        ]

        last_error: Optional[str] = None
        for attempt in range(self.config.max_retries):
            try:
                response = await self._call_llm(messages)
                task.result = response
                task.status = TaskStatus.COMPLETED
                return
            except httpx.HTTPError as e:
                last_error = str(e)
                wait = 2 ** attempt
                logger.warning(
                    "LLM call attempt %d/%d failed (HTTP): %s. Retrying in %ds",
                    attempt + 1, self.config.max_retries, e, wait,
                )
                await asyncio.sleep(wait)
            except Exception as e:
                task.status = TaskStatus.FAILED
                task.error = str(e)
                raise

        task.status = TaskStatus.FAILED
        task.error = last_error or "Max retries exceeded"

    def _build_task_prompt(self, task: Task) -> str:
        """Build the system prompt for task execution.

        Injects project guidelines (CLAUDE.md, .clinerules, AGENTS.md)
        so the LLM has context about coding conventions and constraints.
        """
        lines = [
            "You are an autonomous code execution agent.",
            f"Execute task #{task.id}: {task.description}",
            "",
            "## Guidelines",
        ]

        # Inject guideline files
        for guideline_dir in self.config.guideline_dirs:
            for filename in ("CLAUDE.md", ".clinerules", "AGENTS.md"):
                path = Path(guideline_dir) / filename
                content = self._read_file(path)
                if content:
                    lines.append(f"\n### {filename} ({guideline_dir})")
                    lines.append(content[:2000])

        lines.extend([
            "",
            "## Requirements",
            "- Follow TDD: write/run tests before implementation",
            "- Run pytest to verify correctness",
            "- Use Conventional Commits",
            "- Update PRD if task has prd_impact=True",
        ])

        return "\n".join(lines)

    async def _call_llm(self, messages: list[dict]) -> str:
        """Call the OpenAI-compatible LLM API via httpx.

        Uses configuration from OrchestratorConfig (model, base_url, api_key).
        Returns the response content string. Raises on HTTP errors.
        """
        if not self.config.llm_api_key:
            raise RuntimeError("No LLM API key configured")

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{self.config.llm_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.config.llm_api_key}"},
                json={
                    "model": self.config.llm_model,
                    "messages": messages,
                    "max_tokens": 4096,
                    "temperature": 0.3,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    # ── Progress Tracking ─────────────────────────────────────────────

    async def _read_progress(self) -> dict[str, Any]:
        """Read the current progress.md into a dict.

        Returns:
            {
                "current_task": int or None,
                "completed": int,
                "total": int,
                "decisions": list[str],
                "blocked": list[str],
            }
        """
        result: dict[str, Any] = {
            "current_task": None,
            "completed": 0,
            "total": 0,
            "decisions": [],
            "blocked": [],
        }
        content = self._read_file(self._progress_path)
        if not content:
            return result

        for line in content.splitlines():
            line = line.strip()
            match_current = re.match(r"^- 当前任务:\s*#?(\d+)", line)
            match_completed = re.match(r"^- 已完成:\s*(\d+)/(\d+)", line)
            match_decision = re.match(r"^- (.+?) — (.+)$", line)

            if match_current:
                result["current_task"] = int(match_current.group(1))
            elif match_completed:
                result["completed"] = int(match_completed.group(1))
                result["total"] = int(match_completed.group(2))
            elif match_decision and "决策" in line:
                result["decisions"].append(line)

        return result

    async def _update_progress(self, task: Task, status: Optional[str] = None) -> None:
        """Update progress.md with the current task status.

        Writes a clean progress snapshot: current task, completion count,
        decision log, and any blocked items.
        """
        s = status or task.status.value
        content = self._read_file(self._progress_path) or ""

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        new_entry = (
            f"## 进度 (自动更新 @ {now})\n\n"
            f"- 当前任务: #{task.id}\n"
            f"- 状态: {s}\n"
            f"- 描述: {task.description}\n"
        )

        if task.result:
            new_entry += f"- 结果: {task.result[:200]}\n"
        if task.error:
            new_entry += f"- 错误: {task.error[:200]}\n"

        # Count completed
        plan_content = self._read_file(self._task_plan_path)
        if plan_content:
            all_tasks = self._parse_tasks_from_plan(plan_content)
            completed = sum(1 for t in all_tasks if t.status == TaskStatus.COMPLETED)
            new_entry += f"- 已完成: {completed}/{len(all_tasks)}\n"

        new_entry += "\n## 决策记录\n"
        if task.result:
            new_entry += f"- Task #{task.id} completed — {task.result[:100]}\n"

        # Preserve old decisions
        old_blocked_match = re.search(r"## 阻塞项\n(.+?)(?=\n##|\Z)", content, re.DOTALL)
        if old_blocked_match:
            new_entry += "\n## 阻塞项\n" + old_blocked_match.group(1).strip()

        self._progress_path.parent.mkdir(parents=True, exist_ok=True)
        self._progress_path.write_text(new_entry, encoding="utf-8")

    # ── Context Management ────────────────────────────────────────────

    def _trim_context_if_needed(self) -> None:
        """CowAgent flush-before-trim: keep system + last 4 turns.

        If message history exceeds 80% of context_limit, trim to
        system prompt + the last 4 conversation turns.
        """
        total_chars = sum(len(m.get("content", "")) for m in self._messages)
        if total_chars > self.config.context_limit * 0.8:
            system = [m for m in self._messages if m.get("role") == "system"]
            recent = [m for m in self._messages if m.get("role") != "system"]
            self._messages = system + recent[-4:]
            logger.info("Context trimmed: %d chars → %d turns kept", total_chars, len(self._messages))

    # ── Utilities ─────────────────────────────────────────────────────

    @staticmethod
    def _read_file(path: Path) -> Optional[str]:
        """Read a file, returning None if it doesn't exist."""
        try:
            if path.exists():
                return path.read_text(encoding="utf-8")
        except (OSError, IOError):
            pass
        return None
