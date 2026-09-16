"""Docker CLI implementation of the OCI execution provider."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, BinaryIO

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
from coding_agent.sandbox.resources import ResourceUsageAccumulator, workspace_size


class DockerCliProvider:
    """Create one hardened, short-lived Docker container per command."""

    def __init__(
        self,
        *,
        image: str,
        docker_binary: str = "docker",
        stats_interval_seconds: float = 1.0,
    ) -> None:
        self.image = image
        self.docker_binary = docker_binary
        self.stats_interval_seconds = stats_interval_seconds
        self._health: ProviderHealth | None = None
        self._prepared: dict[str, PreparedExecution] = {}
        self._budgets: dict[str, ResourceBudget] = {}
        self._lock = threading.RLock()

    def health(self, *, force: bool = False) -> ProviderHealth:
        """Check CLI, daemon, Linux-container mode, and the configured image."""
        with self._lock:
            if self._health is not None and not force:
                return self._health
        checked_at = utc_now()
        if shutil.which(self.docker_binary) is None:
            result = ProviderHealth(
                available=False,
                image=self.image,
                checked_at=checked_at,
                error_code="SANDBOX_DOCKER_CLI_MISSING",
                message="Docker CLI is not installed or not on PATH.",
            )
            return self._cache_health(result)
        try:
            version = self._json_command(
                ["version", "--format", "{{json .}}"],
                error_code="SANDBOX_PROVIDER_UNAVAILABLE",
            )
            info = self._json_command(
                ["info", "--format", "{{json .}}"],
                error_code="SANDBOX_PROVIDER_UNAVAILABLE",
            )
            context = self._text_command(
                ["context", "show"],
                error_code="SANDBOX_PROVIDER_UNAVAILABLE",
            ).strip()
            image = self._json_command(
                ["image", "inspect", self.image],
                error_code="SANDBOX_IMAGE_MISSING",
            )
            image_data = image[0] if isinstance(image, list) and image else image
            if not all(isinstance(value, dict) for value in (version, info, image_data)):
                raise fail(
                    "SANDBOX_PROVIDER_INCOMPATIBLE",
                    "Docker returned an unexpected health response.",
                )
            if str(info.get("OSType", "")).lower() != "linux":
                raise fail(
                    "SANDBOX_PROVIDER_INCOMPATIBLE",
                    "Docker daemon must run Linux containers.",
                )
            unsupported_limits = [
                name
                for name in ("MemoryLimit", "PidsLimit")
                if info.get(name) is False
            ]
            if unsupported_limits:
                raise fail(
                    "SANDBOX_PROVIDER_INCOMPATIBLE",
                    "Docker daemon lacks required limits: "
                    + ", ".join(unsupported_limits),
                )
            image_architecture = str(image_data.get("Architecture") or "")
            daemon_architecture = str(info.get("Architecture") or "")
            if (
                image_architecture
                and daemon_architecture
                and _normalize_arch(image_architecture) != _normalize_arch(daemon_architecture)
            ):
                raise fail(
                    "SANDBOX_PROVIDER_INCOMPATIBLE",
                    "Sandbox image architecture does not match the Docker daemon.",
                )
            image_config = image_data.get("Config") or {}
            if isinstance(image_config, dict) and image_config.get("Entrypoint"):
                raise fail(
                    "SANDBOX_PROVIDER_INCOMPATIBLE",
                    "Sandbox image must not define an entrypoint wrapper.",
                )
            image_id = str(image_data.get("Id", ""))
            repo_digests = [
                str(value) for value in (image_data.get("RepoDigests") or [])
            ]
            configured_digest = self.image.partition("@")[2]
            if configured_digest and not any(
                value.endswith(f"@{configured_digest}") for value in repo_digests
            ):
                raise fail(
                    "SANDBOX_IMAGE_DIGEST_MISMATCH",
                    "Configured sandbox image digest does not match the local image.",
                )
            image_digest = (
                configured_digest
                or (repo_digests[0].partition("@")[2] if repo_digests else image_id)
            )
            security_options = {
                str(value).lower() for value in info.get("SecurityOptions", [])
            }
            rootless = any("rootless" in value for value in security_options)
            result = ProviderHealth(
                available=True,
                version=str(version.get("Server", {}).get("Version") or ""),
                context=context,
                server_os=str(info.get("OperatingSystem") or ""),
                architecture=str(info.get("Architecture") or ""),
                rootless=rootless,
                image=self.image,
                image_id=image_id,
                image_digest=image_digest,
                checked_at=checked_at,
            )
        except CodingAgentError as exc:
            result = ProviderHealth(
                available=False,
                image=self.image,
                checked_at=checked_at,
                error_code=exc.code,
                message=exc.user_message,
            )
        except Exception as exc:
            result = ProviderHealth(
                available=False,
                image=self.image,
                checked_at=checked_at,
                error_code="SANDBOX_PROVIDER_INCOMPATIBLE",
                message=f"Docker health check failed: {str(exc)[:1000]}",
            )
        return self._cache_health(result)

    def prepare(self, spec: SandboxExecutionSpec) -> PreparedExecution:
        """Validate provider health and create a hardened stopped container."""
        health = self.health()
        if not health.available:
            raise fail(
                health.error_code or "SANDBOX_PROVIDER_UNAVAILABLE",
                health.message or "Docker sandbox provider is unavailable.",
            )
        if "," in str(spec.workspace_root):
            raise fail(
                "SANDBOX_PREPARE_FAILED",
                "Workspace paths containing commas are unsupported by Docker mount syntax.",
            )
        container_name = f"coding-agent-{spec.execution_id.replace('-', '')[:24]}"
        environment_file = self._write_environment(spec.environment)
        git_mask = self._create_git_mask(spec.workspace_root)
        container_cwd = self._container_cwd(spec)
        argv = [
            "create",
            "--name",
            container_name,
            "--label",
            "coding-agent.managed=true",
            "--label",
            f"coding-agent.host={spec.host_instance_id}",
            "--label",
            f"coding-agent.operation={spec.execution_id}",
            "--label",
            f"coding-agent.tool-run={spec.owner_id}",
            "--label",
            f"coding-agent.owner={spec.owner_id}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(spec.budget.pid_limit),
            "--memory",
            str(spec.budget.memory_bytes),
            "--memory-swap",
            str(spec.budget.memory_bytes),
            "--cpus",
            str(spec.budget.cpus),
            "--ulimit",
            f"nofile={spec.budget.nofile_limit}:{spec.budget.nofile_limit}",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,noexec,size={spec.budget.tmpfs_bytes}",
            "--tmpfs",
            f"/home/agent:rw,nosuid,nodev,size={spec.budget.home_tmpfs_bytes}",
            "--mount",
            f"type=bind,source={spec.workspace_root},target=/workspace",
            "--workdir",
            container_cwd,
            "--user",
            spec.user,
            "--env",
            "HOME=/home/agent",
            "--env",
            "TMPDIR=/tmp",
            "--env",
            "LANG=C.UTF-8",
            "--stop-timeout",
            str(spec.stop_grace_seconds),
        ]
        if environment_file is not None:
            argv.extend(["--env-file", str(environment_file)])
        if git_mask is not None:
            argv.extend(
                [
                    "--mount",
                    f"type=bind,source={git_mask},target=/workspace/.git,readonly",
                ]
            )
        resolved_image = health.image_id or spec.image
        if not resolved_image:
            raise fail("SANDBOX_IMAGE_MISSING", "Docker sandbox image is not configured.")
        argv.extend([resolved_image, *self._container_argv(spec)])
        try:
            container_id = self._text_command(
                argv,
                error_code="SANDBOX_PREPARE_FAILED",
                timeout=30,
            ).strip()
            if not container_id:
                raise fail(
                    "SANDBOX_PREPARE_FAILED",
                    "Docker create returned an empty container ID.",
                )
        except BaseException:
            self._remove_local_files(environment_file, git_mask)
            raise
        if environment_file is not None:
            environment_file.unlink(missing_ok=True)
            environment_file = None
        prepared = PreparedExecution(
            execution_id=spec.execution_id,
            provider="docker",
            isolation_level=IsolationLevel.CONTAINER,
            resource_id=container_id,
            resource_name=container_name,
            container_id=container_id,
            container_name=container_name,
            image=resolved_image,
            image_digest=health.image_digest or health.image_id or resolved_image,
            workspace_root=spec.workspace_root,
            stop_grace_seconds=spec.stop_grace_seconds,
            environment_file=environment_file,
            git_mask_path=git_mask,
        )
        with self._lock:
            self._prepared[container_id] = prepared
            self._budgets[container_id] = spec.budget
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
        """Attach to the container while enforcing deadline and output bounds."""
        handle = ExecutionHandle(
            execution_id=prepared.execution_id,
            provider=prepared.provider,
            resource_id=prepared.resource_id,
            resource_name=prepared.resource_name,
            container_id=prepared.container_id,
            container_name=prepared.container_name,
        )
        container_id = self._container_id(prepared.container_id)
        workspace_before = workspace_size(prepared.workspace_root)
        usage = ResourceUsageAccumulator(workspace_bytes_before=workspace_before)
        if cancel_event.is_set():
            return (
                handle,
                "cancelled",
                usage.finish(
                    workspace_bytes_after=workspace_before,
                    oom_killed=False,
                ),
            )
        process = subprocess.Popen(
            [self.docker_binary, "start", "--attach", container_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._docker_environment(),
        )
        if process.stdout is None or process.stderr is None:
            process.kill()
            raise fail("SANDBOX_START_FAILED", "Docker attach did not create output pipes.")
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
        next_stats = started
        output_bytes = 0
        closed_streams = 0
        termination: str | None = None
        cancelled = False
        try:
            while closed_streams < 2 or process.poll() is None:
                now = time.monotonic()
                if termination is None and cancel_event.is_set():
                    termination = "cancelled"
                elif termination is None and now - started >= timeout_seconds:
                    termination = "timeout"
                if termination is None and now >= next_stats:
                    sample = self._stats(container_id)
                    if sample is not None:
                        observed = usage.observe(sample)
                        if on_usage is not None:
                            on_usage(observed)
                    workspace_now = workspace_size(prepared.workspace_root)
                    if (
                        workspace_now - workspace_before
                        > self._spec_budget(container_id).workspace_growth_bytes
                    ):
                        termination = "workspace_limit"
                    next_stats = now + self.stats_interval_seconds
                if termination is not None and not cancelled:
                    cancelled = True
                    self.cancel(handle, prepared.stop_grace_seconds)
                try:
                    item = output_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item is None:
                    closed_streams += 1
                    continue
                stream, chunk = item
                output_bytes += len(chunk)
                if output_bytes > output_limit_bytes and termination is None:
                    termination = "output_limit"
                if on_output is not None:
                    remaining = max(0, output_limit_bytes - (output_bytes - len(chunk)))
                    if remaining:
                        on_output(stream, chunk[:remaining])
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            termination = termination or "provider_error"
        finally:
            for reader in readers:
                reader.join(timeout=1)
        final_usage = usage.finish(
            workspace_bytes_after=workspace_size(prepared.workspace_root),
            oom_killed=False,
        )
        return handle, termination, final_usage

    def inspect(self, handle: ExecutionHandle) -> ExecutionObservation:
        container_id = self._container_id(handle.container_id)
        data = self._json_command(
            ["inspect", container_id],
            error_code="SANDBOX_PROVIDER_UNAVAILABLE",
        )
        item = data[0] if isinstance(data, list) and data else data
        state = item.get("State", {})
        return ExecutionObservation(
            status=str(state.get("Status") or "unknown"),
            exit_code=state.get("ExitCode"),
            oom_killed=bool(state.get("OOMKilled")),
            started_at=state.get("StartedAt"),
            finished_at=state.get("FinishedAt"),
        )

    def cancel(self, handle: ExecutionHandle, grace_seconds: int) -> None:
        container_id = self._container_id(handle.container_id)
        stopped = self._run(
            ["stop", "--time", str(grace_seconds), container_id],
            timeout=grace_seconds + 5,
        )
        if stopped.returncode != 0:
            self._run(["kill", container_id], timeout=5)

    def remove(self, handle: ExecutionHandle) -> None:
        container_id = self._container_id(handle.container_id)
        result = self._run(["rm", "--force", container_id], timeout=15)
        if result.returncode != 0 and b"No such container" not in result.stderr:
            raise fail(
                "SANDBOX_CLEANUP_PENDING",
                self._bounded_error(result.stderr, "Cannot remove sandbox container."),
            )
        with self._lock:
            prepared = self._prepared.pop(container_id, None)
            self._budgets.pop(container_id, None)
        if prepared is not None:
            self._remove_local_files(
                prepared.environment_file,
                prepared.git_mask_path,
            )

    def list_managed(self) -> list[ExecutionHandle]:
        result = self._text_command(
            [
                "ps",
                "--all",
                "--filter",
                "label=coding-agent.managed=true",
                "--format",
                (
                    "{{.ID}}|{{.Names}}|{{.Label \"coding-agent.operation\"}}|"
                    "{{.Label \"coding-agent.host\"}}"
                ),
            ],
            error_code="SANDBOX_PROVIDER_UNAVAILABLE",
        )
        handles = []
        for line in result.splitlines():
            container_id, container_name, execution_id, host_instance_id = (
                line.split("|", maxsplit=3) + ["", "", ""]
            )[:4]
            if container_id:
                handles.append(
                    ExecutionHandle(
                        execution_id=execution_id or container_id,
                        provider="docker",
                        resource_id=container_id,
                        resource_name=container_name or container_id,
                        container_id=container_id,
                        container_name=container_name or container_id,
                        host_instance_id=host_instance_id or None,
                    )
                )
        return handles

    def final_usage(
        self,
        prepared: PreparedExecution,
        observation: ExecutionObservation,
        current: ResourceUsage | None,
    ) -> ResourceUsage:
        usage = current.model_copy(deep=True) if current is not None else ResourceUsage()
        usage.workspace_bytes_after = workspace_size(prepared.workspace_root)
        usage.workspace_growth_bytes = max(
            0, usage.workspace_bytes_after - usage.workspace_bytes_before
        )
        usage.oom_killed = observation.oom_killed
        return usage

    def _stats(self, container_id: str) -> dict[str, str] | None:
        result = self._run(
            ["stats", "--no-stream", "--format", "{{json .}}", container_id],
            timeout=5,
        )
        if result.returncode != 0:
            return None
        try:
            value = json.loads(result.stdout.decode())
        except (UnicodeDecodeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _spec_budget(self, container_id: str) -> ResourceBudget:
        with self._lock:
            return self._budgets[container_id]

    def _json_command(self, argv: list[str], *, error_code: str) -> Any:
        output = self._text_command(argv, error_code=error_code)
        try:
            return json.loads(output)
        except ValueError as exc:
            raise fail(error_code, "Docker returned malformed JSON.") from exc

    def _text_command(
        self,
        argv: list[str],
        *,
        error_code: str,
        timeout: int = 15,
    ) -> str:
        result = self._run(argv, timeout=timeout)
        if result.returncode != 0:
            raise fail(
                error_code,
                self._bounded_error(result.stderr, "Docker command failed."),
            )
        return result.stdout.decode(errors="replace")

    def _run(self, argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                [self.docker_binary, *argv],
                capture_output=True,
                check=False,
                timeout=timeout,
                env=self._docker_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise fail("SANDBOX_PROVIDER_UNAVAILABLE", f"Docker command failed: {exc}") from exc

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
    def _create_git_mask(workspace_root: Path) -> Path | None:
        git_path = workspace_root / ".git"
        if not git_path.exists():
            return None
        root = Path(tempfile.mkdtemp(prefix="coding-agent-git-mask-"))
        os.chmod(root, 0o700)
        if git_path.is_file():
            mask = root / "git"
            mask.touch(mode=0o400)
            return mask
        mask = root / "git"
        mask.mkdir(mode=0o500)
        return mask

    @staticmethod
    def _write_environment(environment: dict[str, str]) -> Path | None:
        if not environment:
            return None
        fd, name = tempfile.mkstemp(prefix="coding-agent-env-")
        path = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                for key, value in environment.items():
                    if "\n" in value or "\x00" in value:
                        raise fail(
                            "SANDBOX_PREPARE_FAILED",
                            f"Environment value for {key} contains unsupported characters.",
                        )
                    file.write(f"{key}={value}\n")
            os.chmod(path, 0o600)
            return path
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _remove_local_files(
        environment_file: Path | None,
        git_mask: Path | None,
    ) -> None:
        if environment_file is not None:
            environment_file.unlink(missing_ok=True)
        if git_mask is not None:
            root = git_mask.parent
            with suppress(OSError):
                if git_mask.is_dir():
                    git_mask.rmdir()
                else:
                    git_mask.unlink(missing_ok=True)
                root.rmdir()

    @staticmethod
    def _container_cwd(spec: SandboxExecutionSpec) -> str:
        cwd = (spec.workspace_root / spec.request.cwd).resolve(strict=True)
        relative = cwd.relative_to(spec.workspace_root).as_posix()
        return "/workspace" if relative == "." else f"/workspace/{relative}"

    @staticmethod
    def _container_argv(spec: SandboxExecutionSpec) -> list[str]:
        workspace = str(spec.workspace_root)
        prefix = workspace + os.sep
        values = []
        for argument in spec.request.argv:
            if argument == workspace:
                values.append("/workspace")
            elif argument.startswith(prefix):
                values.append(f"/workspace/{argument[len(prefix):]}")
            else:
                values.append(argument)
        return values

    @staticmethod
    def _bounded_error(stderr: bytes, fallback: str) -> str:
        value = stderr.decode(errors="replace").strip()
        return value[:2000] or fallback

    @staticmethod
    def _docker_environment() -> dict[str, str]:
        allowed = (
            "PATH",
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
        )
        return {key: os.environ[key] for key in allowed if key in os.environ}

    def _cache_health(self, health: ProviderHealth) -> ProviderHealth:
        with self._lock:
            self._health = health
        return health

    @staticmethod
    def _container_id(value: str | None) -> str:
        if not value:
            raise fail("SANDBOX_PROVIDER_INCOMPATIBLE", "Docker container ID is missing.")
        return value


def _normalize_arch(value: str) -> str:
    return {
        "aarch64": "arm64",
        "x86_64": "amd64",
    }.get(value.lower(), value.lower())
