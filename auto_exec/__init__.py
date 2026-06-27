"""Auto-exec: code-level planning loop and memory management.

Replaces the prompt-based scheduled task worker with proper Python
Orchestrator and MemoryManager classes.

Packages:
    orchestrator.py — Planning loop (CowAgent-inspired AgentStreamExecutor)
    memory.py       — 3-tier memory with Deep Dream distillation
    models.py       — Pydantic models for task/execution state
"""

from auto_exec.orchestrator import Orchestrator, OrchestratorConfig, ExecutionResult
from auto_exec.memory import MemoryManager

__all__ = ["Orchestrator", "OrchestratorConfig", "ExecutionResult", "MemoryManager"]
