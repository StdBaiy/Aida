"""Provider boundary for short-lived sandbox executions."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol

from coding_agent.sandbox.models import (
    ExecutionHandle,
    ExecutionObservation,
    PreparedExecution,
    ProviderHealth,
    ResourceUsage,
    SandboxExecutionSpec,
)

OutputCallback = Callable[[str, bytes], None]
UsageCallback = Callable[[ResourceUsage], None]


class SandboxExecutionProvider(Protocol):
    """Minimum provider contract used by all command sandboxes."""

    def health(self, *, force: bool = False) -> ProviderHealth: ...

    def prepare(self, spec: SandboxExecutionSpec) -> PreparedExecution: ...

    def start(
        self,
        prepared: PreparedExecution,
        *,
        cancel_event: threading.Event,
        timeout_seconds: int,
        output_limit_bytes: int,
        on_output: OutputCallback | None,
        on_usage: UsageCallback | None,
    ) -> tuple[ExecutionHandle, str | None, ResourceUsage]: ...

    def inspect(self, handle: ExecutionHandle) -> ExecutionObservation: ...

    def cancel(self, handle: ExecutionHandle, grace_seconds: int) -> None: ...

    def remove(self, handle: ExecutionHandle) -> None: ...

    def list_managed(self) -> list[ExecutionHandle]: ...


# Compatibility alias for the original Docker-only API.
OciExecutionProvider = SandboxExecutionProvider
