"""Capability-scoped tools available to one subagent attempt."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from coding_agent.errors import fail

ProgressCallback = Callable[[str, dict[str, Any]], None]


class AttemptToolRuntime:
    """Execute only the tools explicitly granted to an attempt."""

    def __init__(
        self,
        *,
        workspace: Path,
        allowed_tools: tuple[str, ...],
        cancelled: threading.Event,
        on_event: ProgressCallback,
    ) -> None:
        self.workspace = workspace.resolve()
        self.allowed_tools = frozenset(allowed_tools)
        self.cancelled = cancelled
        self.on_event = on_event
        self._tools: dict[str, Callable[..., dict[str, Any]]] = {
            "mock_sleep": self._mock_sleep,
            "write_deliverable": self._write_deliverable,
        }

    def invoke(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke one granted tool and emit an auditable input/output summary."""
        if name not in self.allowed_tools or name not in self._tools:
            raise fail("SUBAGENT_TOOL_FORBIDDEN", f"Subagent is not allowed to call {name}.")
        self.on_event("tool.started", {"tool": name, "input": arguments})
        started = time.monotonic()
        result = self._tools[name](**arguments)
        self.on_event(
            "tool.completed",
            {
                "tool": name,
                "duration_ms": round((time.monotonic() - started) * 1000),
                "output": result,
            },
        )
        return result

    def _mock_sleep(self, seconds: int = 10) -> dict[str, Any]:
        if seconds < 10:
            raise fail("SUBAGENT_TOOL_INVALID", "mock_sleep requires at least 10 seconds.")
        started = time.monotonic()
        for elapsed in range(1, seconds + 1):
            if self.cancelled.wait(1):
                raise fail("SUBAGENT_CANCELLED", "Subagent execution was cancelled.")
            self.on_event(
                "tool.progress",
                {
                    "tool": "mock_sleep",
                    "elapsed_seconds": elapsed,
                    "total_seconds": seconds,
                },
            )
        duration_ms = round((time.monotonic() - started) * 1000)
        if duration_ms < seconds * 1000:
            time.sleep((seconds * 1000 - duration_ms) / 1000)
            duration_ms = round((time.monotonic() - started) * 1000)
        return {"slept_seconds": seconds, "duration_ms": duration_ms}

    def _write_deliverable(self, path: str, content: str) -> dict[str, Any]:
        target = (self.workspace / path).resolve()
        if not target.is_relative_to(self.workspace):
            raise fail("SUBAGENT_PATH_FORBIDDEN", "Deliverable must stay inside the worktree.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": path, "bytes_written": len(content.encode())}
