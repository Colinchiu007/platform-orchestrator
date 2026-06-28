"""3-tier memory management for auto-exec.

Tier 1: In-memory short-term context buffer (recent conversation turns)
Tier 2: Daily log files under .plan/daily/YYYY-MM-DD.md
Tier 3: Deep Dream LLM distillation → memory/auto-distilled-*.md
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from auto_exec.models import ConversationTurn

logger = logging.getLogger(__name__)


class MemoryManager:
    """3-tier memory: buffer → daily logs → LLM-distilled summaries.

    Usage:
        mm = MemoryManager(plan_dir=".plan")
        await mm.append_turn(turn)
        await mm.flush_to_daily()
        summary = await mm.distill()
    """

    def __init__(self, plan_dir: str = ".plan", buffer_size: int = 20) -> None:
        self._plan_dir = Path(plan_dir)
        self._daily_dir = self._plan_dir / "daily"
        self._daily_dir.mkdir(parents=True, exist_ok=True)
        self._buffer: list[ConversationTurn] = []
        self._buffer_size = buffer_size

    # ── Tier 1: Buffer ────────────────────────────────────────────────

    async def append_turn(self, turn: ConversationTurn) -> None:
        """Append a turn to the in-memory buffer.

        Auto-flushes to daily log when buffer exceeds buffer_size.
        """
        self._buffer.append(turn)
        if len(self._buffer) >= self._buffer_size:
            await self.flush_to_daily()

    async def get_recent_context(self, n: int = 10) -> list[ConversationTurn]:
        """Get the most recent N turns from the buffer + today's daily log."""
        # Start with buffered turns
        result = list(self._buffer)

        # Add today's daily log turns
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_path = self._daily_dir / f"{today}.md"
        if daily_path.exists():
            content = daily_path.read_text(encoding="utf-8")
            # Parse turns from the markdown content
            for line in content.splitlines():
                line = line.strip()
                if line.startswith("- **") and ":** " in line:
                    parts = line.split(":** ", 1)
                    if len(parts) == 2:
                        role_part = parts[0].lstrip("- **")
                        result.append(
                            ConversationTurn(
                                role=role_part,
                                content=parts[1],
                            )
                        )

        return result[-n:]

    # ── Tier 2: Daily Logs ────────────────────────────────────────────

    async def flush_to_daily(self) -> None:
        """Flush the in-memory buffer to today's daily log file."""
        if not self._buffer:
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_path = self._daily_dir / f"{today}.md"

        lines: list[str] = []
        if daily_path.exists():
            lines.append(daily_path.read_text(encoding="utf-8").rstrip())

        for turn in self._buffer:
            ts = turn.timestamp.isoformat() if hasattr(turn.timestamp, "isoformat") else str(turn.timestamp)
            prefix = f"- **{turn.role}:** "
            if turn.task_id is not None:
                prefix = f"- **[task-{turn.task_id}] {turn.role}:** "
            lines.append(f"{prefix}{turn.content[:500]}  ({ts})")

        daily_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._buffer.clear()
        logger.info("Flushed %d turns to %s", self._buffer_size, daily_path)

    # ── Tier 3: Deep Dream Distillation ───────────────────────────────

    async def distill(self, force: bool = False) -> Optional[str]:
        """Run Deep Dream distillation over the last 7 daily files.

        Returns the path to the distilled summary file, or None if
        there's nothing new to distill (checks for existing distilled
        file with matching date).
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_path = Path("memory") / f"auto-distilled-{today}.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        if out_path.exists() and not force:
            logger.info("Distilled summary already exists for %s", today)
            return str(out_path)

        # Collect last 7 daily files
        daily_files = sorted(self._daily_dir.glob("*.md"))[-7:]
        if not daily_files:
            logger.info("No daily files to distill")
            return None

        combined = ""
        for fp in daily_files:
            combined += f"\n## {fp.stem}\n{fp.read_text(encoding='utf-8')[:2000]}\n"

        summary = await self._deep_dream_summarize(combined)
        out_path.write_text(summary, encoding="utf-8")
        logger.info("Distilled summary written to %s", out_path)
        return str(out_path)

    async def _deep_dream_summarize(self, content: str) -> str:
        """LLM call: extract decisions, patterns, insights from daily logs.

        Falls back to a regex-based extractor if LLM is unavailable.
        """
        # Try LLM first
        summary = await self._llm_summarize(content)
        if summary:
            return summary

        # Fallback: regex-based keyword extraction
        return self._regex_summarize(content)

    async def _llm_summarize(self, content: str) -> Optional[str]:
        """Call LLM to distill the content (silent on failure)."""
        try:
            api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("PO_OPENAI_API_KEY")
            base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
            model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

            if not api_key:
                return None

            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": [
                            {
                                "role": "system",
                                "content": (
                                    "You are a Deep Dream distillation engine. "
                                    "Extract key decisions, patterns, insights, and unresolved questions "
                                    "from the following daily execution logs. "
                                    "Output a concise markdown summary."
                                ),
                            },
                            {"role": "user", "content": content},
                        ],
                        "max_tokens": 2000,
                        "temperature": 0.3,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"]
        except Exception:
            logger.warning("LLM distillation failed, using regex fallback")
            return None

    def _regex_summarize(self, content: str) -> str:
        """Fallback: extract key lines from daily logs using heuristics."""
        lines = content.splitlines()
        decisions: list[str] = []
        patterns: list[str] = []
        todos: list[str] = []

        decision_keywords = ["decided", "chose", "selected", "agreed", "use", "using", "implement"]
        todo_keywords = ["todo", "fixme", "next", "pending", "blocked"]

        for line in lines:
            lower = line.lower()
            if any(kw in lower for kw in decision_keywords) and len(line) > 20:
                decisions.append(line.strip("- "))
            if any(kw in lower for kw in todo_keywords) and len(line) > 20:
                todos.append(line.strip("- "))

        sections = []
        if decisions:
            sections.append("## Decisions\n" + "\n".join(f"- {d}" for d in decisions[-10:]))
        if patterns:
            sections.append("## Patterns\n" + "\n".join(f"- {p}" for p in patterns[-5:]))
        if todos:
            sections.append("## Next Steps\n" + "\n".join(f"- {t}" for t in todos[-5:]))
        if not sections:
            sections.append("_No significant decisions or patterns detected._")

        return "\n\n".join(sections)

    # ── Housekeeping ──────────────────────────────────────────────────

    async def cleanup(self, max_days: int = 30) -> int:
        """Remove daily logs older than max_days. Returns count removed."""
        now = datetime.now(timezone.utc).timestamp()
        removed = 0
        for fp in self._daily_dir.glob("*.md"):
            try:
                file_date = datetime.strptime(fp.stem, "%Y-%m-%d").timestamp()
                if now - file_date > max_days * 86400:
                    fp.unlink()
                    removed += 1
            except ValueError:
                continue
        if removed:
            logger.info("Cleaned up %d old daily logs", removed)
        return removed
