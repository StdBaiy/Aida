import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from coding_agent.errors import CodingAgentError
from coding_agent.models import CommandRequest, utc_now
from coding_agent.sandbox import (
    DockerCliProvider,
    OciExecutionService,
    ProviderHealth,
    ResourceBudget,
    SandboxExecutionProvider,
    SandboxExecutionRepository,
    SeatbeltProvider,
)
from coding_agent.sandbox.models import (
    ExecutionHandle,
    ExecutionObservation,
    IsolationLevel,
    OciExecutionSpec,
    PreparedExecution,
    ResourceUsage,
)


class FakeProvider:
    def __init__(
        self,
        workspace: Path,
        *,
        available: bool = True,
        termination: str | None = None,
    ) -> None:
        self.workspace = workspace
        self.available = available
        self.termination = termination
        self.removed: list[str] = []
        self.specs: list[OciExecutionSpec] = []

    def health(self, *, force: bool = False) -> ProviderHealth:
        del force
        return ProviderHealth(
            available=self.available,
            image="sandbox@sha256:test",
            image_digest="sha256:test",
            checked_at=utc_now(),
            error_code=None if self.available else "SANDBOX_PROVIDER_UNAVAILABLE",
            message=None if self.available else "daemon unavailable",
        )

    def prepare(self, spec: OciExecutionSpec) -> PreparedExecution:
        self.specs.append(spec)
        return PreparedExecution(
            execution_id=spec.execution_id,
            resource_id="container-id",
            resource_name="container-name",
            container_id="container-id",
            container_name="container-name",
            image=spec.image,
            image_digest="sha256:test",
            workspace_root=self.workspace,
        )

    def start(
        self,
        prepared: PreparedExecution,
        *,
        cancel_event: threading.Event,
        timeout_seconds: int,
        output_limit_bytes: int,
        on_output: Any,
        on_usage: Any,
    ) -> tuple[ExecutionHandle, str | None, ResourceUsage]:
        del cancel_event, timeout_seconds, output_limit_bytes
        usage = ResourceUsage(peak_memory_bytes=1024, samples=1)
        on_usage(usage)
        on_output("stdout", b"ok\n")
        return (
            ExecutionHandle(
                execution_id=prepared.execution_id,
                resource_id=prepared.resource_id,
                resource_name=prepared.resource_name,
                container_id=prepared.container_id,
                container_name=prepared.container_name,
            ),
            self.termination,
            usage,
        )

    def inspect(self, handle: ExecutionHandle) -> ExecutionObservation:
        del handle
        return ExecutionObservation(status="exited", exit_code=0)

    def cancel(self, handle: ExecutionHandle, grace_seconds: int) -> None:
        del handle, grace_seconds

    def remove(self, handle: ExecutionHandle) -> None:
        self.removed.append(handle.resource_id)

    def list_managed(self) -> list[ExecutionHandle]:
        return []


def _service(
    tmp_path: Path,
    provider: SandboxExecutionProvider,
    *,
    enabled: bool = True,
    output_bytes: int = 10 * 1024**2,
) -> OciExecutionService:
    return OciExecutionService(
        workspace_root=tmp_path,
        provider=provider,
        repository=SandboxExecutionRepository(tmp_path / "sandbox.db"),
        budget=ResourceBudget(
            memory_bytes=128 * 1024**2,
            output_bytes=output_bytes,
        ),
        enabled=enabled,
        session_parallelism=1,
        global_parallelism=2,
        stop_grace_seconds=1,
        sandbox_user="1000:1000",
        max_result_output_bytes=4096,
    )


def test_oci_execution_returns_provenance_and_removes_container(tmp_path: Path) -> None:
    provider = FakeProvider(tmp_path)
    service = _service(tmp_path, provider)

    result = service.run(CommandRequest(argv=["python3", "-V"]))

    assert result.exit_code == 0
    assert result.stdout == "ok\n"
    assert result.provider == "docker"
    assert result.container_id == "container-id"
    assert result.image_digest == "sha256:test"
    assert result.isolation_level == "container"
    assert result.termination_reason == "exited"
    assert result.resource_usage is not None
    assert result.resource_usage["peak_memory_bytes"] == 1024
    assert provider.removed == ["container-id"]
    [row] = service.repository.connection.execute(
        "SELECT state, cleanup_pending FROM sandbox_executions"
    ).fetchall()
    assert tuple(row) == ("completed", 0)
    service.close()


def test_sandbox_gate_fails_closed_without_provider(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeProvider(tmp_path, available=False))

    with pytest.raises(CodingAgentError) as exc_info:
        service.run(CommandRequest(argv=["pytest"]))

    assert exc_info.value.code == "SANDBOX_PROVIDER_UNAVAILABLE"
    service.close()


def test_sandbox_gate_never_falls_back_when_disabled(tmp_path: Path) -> None:
    service = _service(tmp_path, FakeProvider(tmp_path), enabled=False)

    with pytest.raises(CodingAgentError) as exc_info:
        service.run(CommandRequest(argv=["printf", "unsafe"]))

    assert exc_info.value.code == "SANDBOX_DISABLED"
    service.close()


def test_resource_termination_has_stable_error_code_and_cleanup(tmp_path: Path) -> None:
    provider = FakeProvider(tmp_path, termination="output_limit")
    service = _service(tmp_path, provider)

    result = service.run(CommandRequest(argv=["noisy-command"]))

    assert result.exit_code is None
    assert result.termination_reason == "output_limit"
    assert result.error_code == "SANDBOX_OUTPUT_LIMIT"
    assert provider.removed == ["container-id"]
    service.close()


def test_docker_create_uses_hardening_and_translates_workspace_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".git").mkdir()
    provider = DockerCliProvider(image="sandbox@sha256:test")
    health = ProviderHealth(
        available=True,
        image="sandbox@sha256:test",
        image_digest="sha256:test",
        checked_at=utc_now(),
    )
    monkeypatch.setattr(provider, "health", lambda **_kwargs: health)
    captured: list[str] = []

    def fake_text(argv: list[str], *, error_code: str, timeout: int = 15) -> str:
        del error_code, timeout
        captured.extend(argv)
        return "container-id\n"

    monkeypatch.setattr(provider, "_text_command", fake_text)
    spec = OciExecutionSpec(
        execution_id="execution-id",
        owner_id="tool-run-id",
        host_instance_id="host-id",
        workspace_root=tmp_path,
        request=CommandRequest(argv=["python3", str(tmp_path / "task.py")]),
        image=health.image,
        budget=ResourceBudget(memory_bytes=128 * 1024**2),
        stop_grace_seconds=2,
    )

    prepared = provider.prepare(spec)

    command = " ".join(captured)
    assert "--network none" in command
    assert "--read-only" in command
    assert "--cap-drop ALL" in command
    assert "--security-opt no-new-privileges" in command
    assert "coding-agent.tool-run=tool-run-id" in command
    assert "/workspace/task.py" in captured
    assert str(tmp_path / "task.py") not in captured
    assert prepared.git_mask_path is not None
    DockerCliProvider._remove_local_files(None, prepared.git_mask_path)


def test_seatbelt_profile_confines_writes_and_network(tmp_path: Path) -> None:
    workspace = tmp_path / 'workspace "quoted"'
    workspace.mkdir()
    (workspace / ".git").mkdir()
    temporary_home = tmp_path / "home"
    temporary_home.mkdir()
    provider = SeatbeltProvider(read_only_paths=["/System", "/usr"])

    profile = provider.build_profile(workspace, temporary_home)

    assert "(deny default)" in profile
    assert "(deny network*)" in profile
    assert f"(subpath {json.dumps(str(workspace))})" in profile
    assert f"(literal {json.dumps(str(workspace / '.git'))})" in profile
    assert str(temporary_home) in profile
    assert "/usr" in profile


def test_seatbelt_health_fails_closed_when_profile_cannot_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = SeatbeltProvider()
    monkeypatch.setattr("coding_agent.sandbox.seatbelt.platform.system", lambda: "Darwin")
    monkeypatch.setattr(
        "coding_agent.sandbox.seatbelt.shutil.which",
        lambda _binary: "/usr/bin/sandbox-exec",
    )

    def failed_probe(*_args: Any, **_kwargs: Any) -> Any:
        return type(
            "Probe",
            (),
            {"returncode": 71, "stderr": b"sandbox_apply: Operation not permitted"},
        )()

    monkeypatch.setattr("coding_agent.sandbox.seatbelt.subprocess.run", failed_probe)

    health = provider.health()

    assert not health.available
    assert health.provider == "seatbelt"
    assert health.error_code == "SANDBOX_SEATBELT_UNAVAILABLE"
    assert "Operation not permitted" in (health.message or "")


def test_seatbelt_process_monitor_enforces_timeout_and_output_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = tmp_path / "sandbox-exec-stub"
    launcher.write_text("#!/bin/sh\nshift 2\nexec \"$@\"\n", encoding="utf-8")
    launcher.chmod(0o700)
    provider = SeatbeltProvider(
        sandbox_binary=str(launcher),
        monitor_interval_seconds=0.01,
    )
    health = ProviderHealth(
        available=True,
        provider="seatbelt",
        isolation_level=IsolationLevel.SEATBELT,
        checked_at=utc_now(),
    )
    monkeypatch.setattr(provider, "health", lambda **_kwargs: health)
    slow_script = tmp_path / "slow.py"
    slow_script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    noisy_script = tmp_path / "noisy.py"
    noisy_script.write_text("print('x' * 4096)\n", encoding="utf-8")
    service = _service(tmp_path, provider, output_bytes=1024)

    timed_out = service.run(
        CommandRequest(argv=[sys.executable, str(slow_script)], timeout_seconds=1)
    )
    noisy = service.run(CommandRequest(argv=[sys.executable, str(noisy_script)]))
    cancellation = threading.Event()
    cancelled_results: list[Any] = []
    worker = threading.Thread(
        target=lambda: cancelled_results.append(
            service.run(
                CommandRequest(
                    argv=[sys.executable, str(slow_script)],
                    timeout_seconds=10,
                ),
                cancel_event=cancellation,
            )
        )
    )
    worker.start()
    time.sleep(0.1)
    cancellation.set()
    worker.join(timeout=3)

    assert timed_out.termination_reason == "timeout"
    assert timed_out.error_code == "SANDBOX_EXEC_TIMEOUT"
    assert noisy.termination_reason == "output_limit"
    assert noisy.error_code == "SANDBOX_OUTPUT_LIMIT"
    assert len(noisy.stdout.encode()) <= 1024
    assert not worker.is_alive()
    assert cancelled_results[0].termination_reason == "cancelled"
    assert cancelled_results[0].error_code == "SANDBOX_CANCELLED"
    service.close()


def test_seatbelt_real_workspace_boundary(tmp_path: Path) -> None:
    provider = SeatbeltProvider()
    health = provider.health(force=True)
    if not health.available:
        pytest.skip(health.message or "Seatbelt unavailable")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    script = workspace / "boundary.py"
    script.write_text(
        "\n".join(
            (
                "from pathlib import Path",
                "import socket",
                "Path('inside.txt').write_text('ok')",
                "try:",
                f"    Path({str(outside)!r}).write_text('denied')",
                "except PermissionError:",
                "    pass",
                "else:",
                "    raise SystemExit(9)",
                "try:",
                "    socket.create_connection(('127.0.0.1', 9), timeout=0.1)",
                "except PermissionError:",
                "    pass",
                "except OSError:",
                "    raise SystemExit(10)",
                "else:",
                "    raise SystemExit(11)",
            )
        ),
        encoding="utf-8",
    )
    service = _service(workspace, provider)

    result = service.run(
        CommandRequest(argv=[sys.executable, str(script)])
    )

    assert result.exit_code == 0
    assert (workspace / "inside.txt").read_text() == "ok"
    assert not outside.exists()
    assert result.provider == "seatbelt"
    assert result.isolation_level == "seatbelt"
    assert result.container_id is None
    assert result.sandbox_policy_digest
    service.close()
