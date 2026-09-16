"""Mandatory policy gate and orchestration for sandboxed command execution."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from coding_agent.errors import fail
from coding_agent.execution.policy import CommandPolicy
from coding_agent.models import CommandRequest, CommandResult
from coding_agent.sandbox.models import (
    ExecutionHandle,
    ResourceBudget,
    ResourceUsage,
    SandboxExecutionSpec,
    TerminationReason,
)
from coding_agent.sandbox.provider import OutputCallback, SandboxExecutionProvider
from coding_agent.sandbox.reconciler import SandboxReconciler
from coding_agent.sandbox.repository import SandboxExecutionRepository
from coding_agent.sandbox.resources import ResourceAdmission
from coding_agent.tracing.context import active_recorder
from coding_agent.workspace.paths import PathGuard

if TYPE_CHECKING:
    from coding_agent.tracing.recorder import TraceRecorder

_HOST_INSTANCE_ID = str(uuid.uuid4())


class SandboxExecutionGate:
    """Non-bypassable preflight for every model-controlled command."""

    def __init__(
        self,
        *,
        guard: PathGuard,
        provider: SandboxExecutionProvider,
        enabled: bool,
    ) -> None:
        self.guard = guard
        self.provider = provider
        self.enabled = enabled
        self.policy = CommandPolicy()

    def validate(self, request: CommandRequest) -> str:
        """Validate command policy, cwd confinement, and provider readiness."""
        if not self.enabled:
            raise fail(
                "SANDBOX_DISABLED",
                "Command execution is disabled because the sandbox is not enabled.",
            )
        self.policy.validate(request)
        self.guard.validate_cwd(request.cwd)
        health = self.provider.health()
        if not health.available:
            raise fail(
                health.error_code or "SANDBOX_PROVIDER_UNAVAILABLE",
                health.message or "Sandbox provider is unavailable.",
            )
        return str(uuid.uuid4())


class SandboxExecutionService:
    """Execute approved argv through one configured sandbox provider."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        provider: SandboxExecutionProvider,
        repository: SandboxExecutionRepository,
        budget: ResourceBudget,
        enabled: bool,
        session_parallelism: int,
        global_parallelism: int,
        stop_grace_seconds: int,
        sandbox_user: str,
        max_result_output_bytes: int,
    ) -> None:
        self.workspace_root = workspace_root.resolve(strict=True)
        self.provider = provider
        self.repository = repository
        self.budget = budget
        self.stop_grace_seconds = stop_grace_seconds
        self.sandbox_user = sandbox_user
        self.max_result_output_bytes = max_result_output_bytes
        self.gate = SandboxExecutionGate(
            guard=PathGuard(self.workspace_root),
            provider=provider,
            enabled=enabled,
        )
        self.admission = ResourceAdmission(
            session_limit=session_parallelism,
            global_limit=global_parallelism,
        )

    def run(
        self,
        request: CommandRequest,
        *,
        extra_env: Mapping[str, str] | None = None,
        cancel_event: threading.Event | None = None,
        on_output: OutputCallback | None = None,
        recorder: TraceRecorder | None = None,
        owner_id: str = "runtime",
    ) -> CommandResult:
        """Run one command after all mandatory checks, without a Host fallback."""
        from coding_agent.execution.tool_runs import active_tool_run_id

        cancellation = cancel_event or threading.Event()
        owner_id = active_tool_run_id() or owner_id
        decision_id = self.gate.validate(request)
        health = self.provider.health()
        execution_id = str(uuid.uuid4())
        self.repository.create(
            execution_id,
            owner_id,
            _HOST_INSTANCE_ID,
            provider=health.provider,
        )
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        usage = ResourceUsage()
        prepared = None
        handle = None
        termination: str | None = None
        terminal_state = "failed"
        started = time.monotonic()

        def capture(stream: str, chunk: bytes) -> None:
            parts = stdout_parts if stream == "stdout" else stderr_parts
            parts.append(chunk)
            if on_output is not None:
                on_output(stream, chunk)

        try:
            with self.admission.acquire(cancellation):
                self.repository.update(execution_id, "preparing")
                spec = SandboxExecutionSpec(
                    execution_id=execution_id,
                    owner_id=owner_id,
                    host_instance_id=_HOST_INSTANCE_ID,
                    workspace_root=self.workspace_root,
                    request=request,
                    image=health.image,
                    user=self.sandbox_user,
                    budget=self.budget,
                    environment=dict(extra_env or {}),
                    stop_grace_seconds=self.stop_grace_seconds,
                )
                prepared = self.provider.prepare(spec)
                handle = ExecutionHandle(
                    execution_id=prepared.execution_id,
                    provider=prepared.provider,
                    resource_id=prepared.resource_id,
                    resource_name=prepared.resource_name,
                    container_id=prepared.container_id,
                    container_name=prepared.container_name,
                )
                self.repository.update(
                    execution_id,
                    "running",
                    resource_id=prepared.resource_id,
                    container_id=prepared.container_id,
                    image_digest=prepared.image_digest,
                    policy_digest=prepared.policy_digest,
                )
                handle, termination, usage = self.provider.start(
                    prepared,
                    cancel_event=cancellation,
                    timeout_seconds=request.timeout_seconds,
                    output_limit_bytes=self.budget.output_bytes,
                    on_output=capture,
                    on_usage=lambda observed: self.repository.update(
                        execution_id,
                        "running",
                        usage=observed.model_dump(),
                    ),
                )
                self.repository.update(
                    execution_id,
                    "running",
                    resource_id=handle.resource_id,
                    container_id=handle.container_id,
                )
                self.repository.update(execution_id, "collecting")
                observation = self.provider.inspect(handle)
                usage.oom_killed = observation.oom_killed
                if cancellation.is_set() and termination is None:
                    termination = "cancelled"
                elif observation.oom_killed:
                    termination = "memory_limit"
                elif (
                    termination is None
                    and observation.exit_code not in {None, 0}
                    and usage.peak_pids >= self.budget.pid_limit
                ):
                    termination = "pid_limit"
                elif termination is None and observation.status not in {"exited", "dead"}:
                    termination = "provider_error"
                reason = self._termination_reason(termination)
                full_stdout = b"".join(stdout_parts)
                full_stderr = b"".join(stderr_parts)
                if reason == TerminationReason.CANCELLED:
                    full_stderr += b"\nCommand cancelled by scheduler.\n"
                stdout = full_stdout[: self.max_result_output_bytes]
                stderr = full_stderr[: self.max_result_output_bytes]
                result = CommandResult(
                    exit_code=(
                        observation.exit_code
                        if reason == TerminationReason.EXITED
                        else None
                    ),
                    stdout=stdout.decode(errors="replace"),
                    stderr=stderr.decode(errors="replace"),
                    timed_out=reason == TerminationReason.TIMEOUT,
                    stdout_truncated=len(full_stdout) > len(stdout),
                    stderr_truncated=len(full_stderr) > len(stderr),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    sandbox_id=execution_id,
                    provider=prepared.provider,
                    container_id=prepared.container_id,
                    image_digest=prepared.image_digest,
                    isolation_level=prepared.isolation_level,
                    termination_reason=reason,
                    resource_usage=usage.model_dump(),
                    policy_decision_id=decision_id,
                    sandbox_policy_digest=prepared.policy_digest,
                    error_code=self._error_code(reason),
                )
                trace_recorder = recorder or active_recorder()
                if trace_recorder is not None:
                    with suppress(Exception):
                        result.stdout_artifact_id = trace_recorder.capture_text_artifact(
                            full_stdout
                        )
                    with suppress(Exception):
                        result.stderr_artifact_id = trace_recorder.capture_text_artifact(
                            full_stderr
                        )
                terminal_state = (
                    "cancelled"
                    if reason == TerminationReason.CANCELLED
                    else "failed"
                    if reason != TerminationReason.EXITED
                    else "completed"
                )
                self.repository.update(
                    execution_id,
                    terminal_state,
                    termination_reason=reason,
                    usage=usage.model_dump(),
                )
                return result
        except BaseException:
            terminal_state = "cancelled" if cancellation.is_set() else "failed"
            self.repository.update(
                execution_id,
                terminal_state,
                termination_reason=(
                    TerminationReason.CANCELLED
                    if cancellation.is_set()
                    else TerminationReason.PROVIDER_ERROR
                ),
                usage=usage.model_dump(),
            )
            raise
        finally:
            if handle is not None:
                self.repository.update(execution_id, "removing")
                try:
                    self.provider.remove(handle)
                except BaseException:
                    self.repository.update(
                        execution_id,
                        terminal_state,
                        cleanup_pending=True,
                    )
                else:
                    self.repository.update(
                        execution_id,
                        terminal_state,
                        cleanup_pending=False,
                    )

    def close(self) -> None:
        self.repository.close()

    def record_health(self) -> None:
        """Persist the startup provider preflight for audit and UI recovery."""
        self.repository.save_health(self.provider.health().model_dump(mode="json"))

    def reconcile(self) -> int:
        """Remove provider resources left by previous Host process instances."""
        return SandboxReconciler(
            self.provider,
            self.repository,
            host_instance_id=_HOST_INSTANCE_ID,
        ).reconcile()

    @staticmethod
    def _termination_reason(value: str | None) -> TerminationReason:
        return {
            None: TerminationReason.EXITED,
            "exited": TerminationReason.EXITED,
            "cancelled": TerminationReason.CANCELLED,
            "timeout": TerminationReason.TIMEOUT,
            "memory_limit": TerminationReason.MEMORY_LIMIT,
            "pid_limit": TerminationReason.PID_LIMIT,
            "output_limit": TerminationReason.OUTPUT_LIMIT,
            "workspace_limit": TerminationReason.WORKSPACE_LIMIT,
        }.get(value, TerminationReason.PROVIDER_ERROR)

    @staticmethod
    def _error_code(reason: TerminationReason) -> str | None:
        return {
            TerminationReason.CANCELLED: "SANDBOX_CANCELLED",
            TerminationReason.TIMEOUT: "SANDBOX_EXEC_TIMEOUT",
            TerminationReason.MEMORY_LIMIT: "SANDBOX_MEMORY_LIMIT",
            TerminationReason.PID_LIMIT: "SANDBOX_PID_LIMIT",
            TerminationReason.OUTPUT_LIMIT: "SANDBOX_OUTPUT_LIMIT",
            TerminationReason.WORKSPACE_LIMIT: "SANDBOX_WORKSPACE_GROWTH_LIMIT",
            TerminationReason.PROVIDER_ERROR: "SANDBOX_PROVIDER_UNAVAILABLE",
        }.get(reason)


# Compatibility alias for the original Docker-only API.
OciExecutionService = SandboxExecutionService
