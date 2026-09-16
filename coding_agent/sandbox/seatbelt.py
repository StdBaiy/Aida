"""macOS Seatbelt implementation of the sandbox provider."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO

from coding_agent.errors import CodingAgentError, fail
from coding_agent.models import utc_now
from coding_agent.sandbox.models import (
    ExecutionHandle,
    ExecutionObservation,
    IsolationLevel,
    PreparedExecution,
    ProviderHealth,
    ResourceBudget,
    ResourceUsage,
    SandboxExecutionSpec,
)
from coding_agent.sandbox.provider import OutputCallback, UsageCallback
from coding_agent.sandbox.resources import workspace_size

_DEFAULT_READ_ROOTS = (
    "/System",
    "/Library",
    "/usr",
    "/bin",
    "/sbin",
    "/opt/homebrew",
    "/usr/local",
    "/private/var/db/timezone",
)
_ENVIRONMENT_KEYS = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "USER",
    "LOGNAME",
)


class SeatbeltProvider:
    """Run commands under a generated sandbox-exec profile."""

    def __init__(
        self,
        *,
        sandbox_binary: str = "/usr/bin/sandbox-exec",
        read_only_paths: list[str] | None = None,
        monitor_interval_seconds: float = 0.25,
    ) -> None:
        self.sandbox_binary = sandbox_binary
        configured_roots = read_only_paths or list(_DEFAULT_READ_ROOTS)
        runtime_roots = [sys.base_prefix, sys.prefix]
        self.read_only_paths = tuple(dict.fromkeys([*configured_roots, *runtime_roots]))
        self.monitor_interval_seconds = monitor_interval_seconds
        self._health: ProviderHealth | None = None
        self._prepared: dict[str, PreparedExecution] = {}
        self._budgets: dict[str, ResourceBudget] = {}
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._observations: dict[str, ExecutionObservation] = {}
        self._lock = threading.RLock()

    def health(self, *, force: bool = False) -> ProviderHealth:
        """Verify the platform, binary, and ability to apply a minimal profile."""
        with self._lock:
            if self._health is not None and not force:
                return self._health
        checked_at = utc_now()
        if platform.system() != "Darwin":
            return self._cache_health(
                ProviderHealth(
                    available=False,
                    provider="seatbelt",
                    isolation_level=IsolationLevel.SEATBELT,
                    checked_at=checked_at,
                    error_code="SANDBOX_PLATFORM_UNSUPPORTED",
                    message="Seatbelt is only available on macOS.",
                )
            )
        binary = shutil.which(self.sandbox_binary)
        if binary is None:
            return self._cache_health(
                ProviderHealth(
                    available=False,
                    provider="seatbelt",
                    isolation_level=IsolationLevel.SEATBELT,
                    checked_at=checked_at,
                    error_code="SANDBOX_SEATBELT_MISSING",
                    message="sandbox-exec is not installed or is not executable.",
                )
            )
        probe_profile = "\n".join(
            (
                "(version 1)",
                "(deny default)",
                '(import "system.sb")',
                "(allow process*)",
                '(allow file-read* file-test-existence (literal "/usr/bin/true"))',
            )
        )
        try:
            probe = subprocess.run(
                [binary, "-p", probe_profile, "/usr/bin/true"],
                capture_output=True,
                check=False,
                timeout=5,
            )
            if probe.returncode != 0:
                message = probe.stderr.decode(errors="replace").strip()
                raise fail(
                    "SANDBOX_SEATBELT_UNAVAILABLE",
                    message[:1000] or "Seatbelt profile probe failed.",
                )
            result = ProviderHealth(
                available=True,
                provider="seatbelt",
                isolation_level=IsolationLevel.SEATBELT,
                version=platform.mac_ver()[0],
                context="macOS Seatbelt",
                server_os="macOS",
                architecture=platform.machine(),
                checked_at=checked_at,
            )
        except CodingAgentError as exc:
            result = ProviderHealth(
                available=False,
                provider="seatbelt",
                isolation_level=IsolationLevel.SEATBELT,
                checked_at=checked_at,
                error_code=exc.code,
                message=exc.user_message,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            result = ProviderHealth(
                available=False,
                provider="seatbelt",
                isolation_level=IsolationLevel.SEATBELT,
                checked_at=checked_at,
                error_code="SANDBOX_SEATBELT_UNAVAILABLE",
                message=f"Seatbelt profile probe failed: {exc}",
            )
        return self._cache_health(result)

    def prepare(self, spec: SandboxExecutionSpec) -> PreparedExecution:
        """Create a private HOME and a workspace-scoped Seatbelt profile."""
        health = self.health()
        if not health.available:
            raise fail(
                health.error_code or "SANDBOX_PROVIDER_UNAVAILABLE",
                health.message or "Seatbelt sandbox provider is unavailable.",
            )
        cwd = (spec.workspace_root / spec.request.cwd).resolve(strict=True)
        try:
            cwd.relative_to(spec.workspace_root)
        except ValueError as exc:
            raise fail("PATH_OUTSIDE_WORKSPACE", "Command cwd escapes the workspace.") from exc
        temporary_home = Path(tempfile.mkdtemp(prefix="coding-agent-seatbelt-")).resolve()
        os.chmod(temporary_home, 0o700)
        profile = self.build_profile(spec.workspace_root, temporary_home)
        policy_digest = hashlib.sha256(profile.encode()).hexdigest()
        environment = self._environment(spec.environment, temporary_home)
        prepared = PreparedExecution(
            execution_id=spec.execution_id,
            provider="seatbelt",
            isolation_level=IsolationLevel.SEATBELT,
            resource_id=spec.execution_id,
            resource_name=f"seatbelt-{spec.execution_id[:12]}",
            workspace_root=spec.workspace_root,
            policy_digest=policy_digest,
            stop_grace_seconds=spec.stop_grace_seconds,
            temporary_home=temporary_home,
            command_argv=list(spec.request.argv),
            command_cwd=cwd,
            environment=environment,
            profile=profile,
        )
        with self._lock:
            self._prepared[spec.execution_id] = prepared
            self._budgets[spec.execution_id] = spec.budget
        return prepared

    def start(
        self,
        prepared: PreparedExecution,
        *,
        cancel_event: threading.Event,
        timeout_seconds: int,
        output_limit_bytes: int,
        on_output: OutputCallback | None,
        on_usage: UsageCallback | None,
    ) -> tuple[ExecutionHandle, str | None, ResourceUsage]:
        """Run and monitor one Seatbelt-confined process group."""
        if prepared.profile is None or prepared.command_cwd is None:
            raise fail("SANDBOX_PREPARE_FAILED", "Seatbelt execution is incomplete.")
        workspace_before = workspace_size(prepared.workspace_root)
        usage = ResourceUsage(workspace_bytes_before=workspace_before)
        if cancel_event.is_set():
            handle = self._pending_handle(prepared)
            with self._lock:
                self._observations[handle.resource_id] = ExecutionObservation(
                    status="exited",
                    exit_code=None,
                )
            return (
                handle,
                "cancelled",
                self._finish_usage(usage, prepared.workspace_root),
            )
        try:
            process = subprocess.Popen(
                [
                    self.sandbox_binary,
                    "-p",
                    prepared.profile,
                    *prepared.command_argv,
                ],
                cwd=prepared.command_cwd,
                env=prepared.environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            self.remove(self._pending_handle(prepared))
            raise fail("SANDBOX_START_FAILED", f"Cannot start Seatbelt command: {exc}") from exc
        if process.stdout is None or process.stderr is None:
            process.kill()
            raise fail("SANDBOX_START_FAILED", "Seatbelt command did not create output pipes.")
        handle = ExecutionHandle(
            execution_id=prepared.execution_id,
            provider="seatbelt",
            resource_id=str(process.pid),
            resource_name=prepared.resource_name,
        )
        with self._lock:
            self._processes[handle.resource_id] = process
        output_queue: queue.Queue[tuple[str, bytes] | None] = queue.Queue()
        readers = [
            threading.Thread(
                target=self._read_stream,
                args=("stdout", process.stdout, output_queue),
                daemon=True,
            ),
            threading.Thread(
                target=self._read_stream,
                args=("stderr", process.stderr, output_queue),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        started = time.monotonic()
        next_sample = started
        output_bytes = 0
        closed_streams = 0
        termination: str | None = None
        stopped = False
        try:
            while closed_streams < 2 or process.poll() is None:
                now = time.monotonic()
                if termination is None and cancel_event.is_set():
                    termination = "cancelled"
                elif termination is None and now - started >= timeout_seconds:
                    termination = "timeout"
                if now >= next_sample:
                    current_size = workspace_size(prepared.workspace_root)
                    usage.workspace_bytes_after = current_size
                    usage.workspace_growth_bytes = max(
                        0, current_size - workspace_before
                    )
                    usage.samples += 1
                    if on_usage is not None:
                        on_usage(usage.model_copy(deep=True))
                    if (
                        termination is None
                        and usage.workspace_growth_bytes
                        > self._budget(prepared).workspace_growth_bytes
                    ):
                        termination = "workspace_limit"
                    next_sample = now + self.monitor_interval_seconds
                if termination is not None and not stopped:
                    stopped = True
                    self.cancel(handle, prepared.stop_grace_seconds)
                try:
                    item = output_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item is None:
                    closed_streams += 1
                    continue
                stream, chunk = item
                previous_bytes = output_bytes
                output_bytes += len(chunk)
                remaining = max(0, output_limit_bytes - previous_bytes)
                if on_output is not None and remaining:
                    on_output(stream, chunk[:remaining])
                if output_bytes > output_limit_bytes and termination is None:
                    termination = "output_limit"
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._kill_process_group(process.pid, signal.SIGKILL)
            return_code = process.wait(timeout=5)
            termination = termination or "provider_error"
        finally:
            for reader in readers:
                reader.join(timeout=1)
        observation = ExecutionObservation(status="exited", exit_code=return_code)
        with self._lock:
            self._observations[handle.resource_id] = observation
        return handle, termination, self._finish_usage(usage, prepared.workspace_root)

    def inspect(self, handle: ExecutionHandle) -> ExecutionObservation:
        with self._lock:
            observation = self._observations.get(handle.resource_id)
        if observation is None:
            raise fail(
                "SANDBOX_PROVIDER_UNAVAILABLE",
                "Seatbelt process state is unavailable.",
            )
        return observation

    def cancel(self, handle: ExecutionHandle, grace_seconds: int) -> None:
        with self._lock:
            process = self._processes.get(handle.resource_id)
        if process is None or process.poll() is not None:
            return
        self._kill_process_group(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            self._kill_process_group(process.pid, signal.SIGKILL)

    def remove(self, handle: ExecutionHandle) -> None:
        with self._lock:
            self._processes.pop(handle.resource_id, None)
            self._observations.pop(handle.resource_id, None)
            prepared = self._prepared.pop(handle.execution_id, None)
            self._budgets.pop(handle.execution_id, None)
        if prepared is not None and prepared.temporary_home is not None:
            shutil.rmtree(prepared.temporary_home, ignore_errors=True)

    def list_managed(self) -> list[ExecutionHandle]:
        # Seatbelt has no durable resource registry. Live children are owned in-process.
        return []

    def build_profile(self, workspace_root: Path, temporary_home: Path) -> str:
        """Build a deterministic profile with no user-home or network access."""
        workspace = workspace_root.resolve(strict=True)
        git_path = workspace / ".git"
        readable = [workspace, temporary_home]
        readable.extend(Path(value) for value in self.read_only_paths)
        read_rules = "\n".join(
            f"    (subpath {self._quote_path(path)})" for path in readable
        )
        lines = [
            "(version 1)",
            "(deny default)",
            '(import "system.sb")',
            "(allow process*)",
            "(allow signal (target self))",
            "(allow file-read-metadata file-test-existence)",
            "(allow file-read* file-map-executable",
            read_rules,
            ")",
            "(allow file-write*",
            f"    (subpath {self._quote_path(workspace)})",
            f"    (subpath {self._quote_path(temporary_home)})",
            ")",
            "(deny network*)",
        ]
        if git_path.exists():
            quoted_git = self._quote_path(git_path)
            lines.extend(
                [
                    "(deny file-write*",
                    f"    (literal {quoted_git})",
                    f"    (subpath {quoted_git})",
                    ")",
                ]
            )
        return "\n".join(lines) + "\n"

    def _budget(self, prepared: PreparedExecution) -> ResourceBudget:
        with self._lock:
            return self._budgets[prepared.execution_id]

    @staticmethod
    def _pending_handle(prepared: PreparedExecution) -> ExecutionHandle:
        return ExecutionHandle(
            execution_id=prepared.execution_id,
            provider="seatbelt",
            resource_id=prepared.resource_id,
            resource_name=prepared.resource_name,
        )

    @staticmethod
    def _finish_usage(usage: ResourceUsage, workspace_root: Path) -> ResourceUsage:
        usage.workspace_bytes_after = workspace_size(workspace_root)
        usage.workspace_growth_bytes = max(
            0, usage.workspace_bytes_after - usage.workspace_bytes_before
        )
        return usage

    @staticmethod
    def _read_stream(
        name: str,
        stream: BinaryIO,
        output: queue.Queue[tuple[str, bytes] | None],
    ) -> None:
        try:
            while chunk := os.read(stream.fileno(), 4096):
                output.put((name, chunk))
        finally:
            output.put(None)

    @staticmethod
    def _environment(extra: dict[str, str], temporary_home: Path) -> dict[str, str]:
        environment = {
            key: os.environ[key] for key in _ENVIRONMENT_KEYS if key in os.environ
        }
        for key, value in extra.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise fail("SANDBOX_PREPARE_FAILED", f"Invalid environment name: {key}")
            if "\x00" in value:
                raise fail(
                    "SANDBOX_PREPARE_FAILED",
                    f"Environment value for {key} contains a null byte.",
                )
            environment[key] = value
        temporary = str(temporary_home)
        environment.update(
            {
                "HOME": temporary,
                "TMPDIR": temporary,
                "XDG_CACHE_HOME": f"{temporary}/cache",
                "XDG_CONFIG_HOME": f"{temporary}/config",
            }
        )
        return environment

    @staticmethod
    def _quote_path(path: Path) -> str:
        return json.dumps(str(path.resolve()), ensure_ascii=True)

    @staticmethod
    def _kill_process_group(pid: int, requested_signal: signal.Signals) -> None:
        with suppress(ProcessLookupError):
            os.killpg(pid, requested_signal)

    def _cache_health(self, health: ProviderHealth) -> ProviderHealth:
        with self._lock:
            self._health = health
        return health
