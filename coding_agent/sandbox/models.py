"""Provider-neutral sandbox domain models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from coding_agent.models import CommandRequest


class IsolationLevel(StrEnum):
    """Isolation strength reported with every execution."""

    CONTAINER = "container"
    SEATBELT = "seatbelt"


class TerminationReason(StrEnum):
    """Stable command termination classifications."""

    EXITED = "exited"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    MEMORY_LIMIT = "memory_limit"
    PID_LIMIT = "pid_limit"
    OUTPUT_LIMIT = "output_limit"
    WORKSPACE_LIMIT = "workspace_limit"
    PROVIDER_ERROR = "provider_error"


class ProviderHealth(BaseModel):
    """Last observed sandbox provider health."""

    available: bool
    provider: str = "docker"
    isolation_level: IsolationLevel = IsolationLevel.CONTAINER
    version: str | None = None
    context: str | None = None
    server_os: str | None = None
    architecture: str | None = None
    rootless: bool = False
    image: str | None = None
    image_id: str | None = None
    image_digest: str | None = None
    policy_digest: str | None = None
    checked_at: datetime
    error_code: str | None = None
    message: str | None = None


class ResourceBudget(BaseModel):
    """Hard and monitored resource limits for one execution."""

    cpus: float = Field(default=2.0, gt=0, le=4)
    memory_bytes: int = Field(default=2 * 1024**3, ge=64 * 1024**2, le=8 * 1024**3)
    pid_limit: int = Field(default=256, ge=16, le=1024)
    nofile_limit: int = Field(default=4096, ge=256, le=16384)
    tmpfs_bytes: int = Field(default=512 * 1024**2, ge=16 * 1024**2, le=2 * 1024**3)
    home_tmpfs_bytes: int = Field(default=64 * 1024**2, ge=8 * 1024**2, le=256 * 1024**2)
    output_bytes: int = Field(default=10 * 1024**2, ge=1024, le=50 * 1024**2)
    workspace_growth_bytes: int = Field(default=1024**3, ge=1024**2, le=5 * 1024**3)


class ResourceUsage(BaseModel):
    """Trusted-side resource observations."""

    peak_memory_bytes: int = 0
    peak_pids: int = 0
    cpu_percent_peak: float = 0
    block_read_bytes: int = 0
    block_write_bytes: int = 0
    workspace_bytes_before: int = 0
    workspace_bytes_after: int = 0
    workspace_growth_bytes: int = 0
    oom_killed: bool = False
    samples: int = 0


class SandboxExecutionSpec(BaseModel):
    """Fully validated input to one sandbox provider."""

    execution_id: str
    owner_id: str
    host_instance_id: str
    workspace_root: Path
    request: CommandRequest
    image: str | None = None
    user: str = "1000:1000"
    budget: ResourceBudget
    environment: dict[str, str] = Field(default_factory=dict)
    stop_grace_seconds: int = Field(default=3, ge=1, le=30)


class PreparedExecution(BaseModel):
    """A prepared provider execution."""

    execution_id: str
    provider: str = "docker"
    isolation_level: IsolationLevel = IsolationLevel.CONTAINER
    resource_id: str
    resource_name: str
    workspace_root: Path
    image: str | None = None
    image_digest: str | None = None
    policy_digest: str | None = None
    container_id: str | None = None
    container_name: str | None = None
    stop_grace_seconds: int = 3
    environment_file: Path | None = None
    git_mask_path: Path | None = None
    temporary_home: Path | None = None
    command_argv: list[str] = Field(default_factory=list)
    command_cwd: Path | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    profile: str | None = None


class ExecutionHandle(BaseModel):
    """Stable provider handle."""

    execution_id: str
    provider: str = "docker"
    resource_id: str
    resource_name: str
    container_id: str | None = None
    container_name: str | None = None
    host_instance_id: str | None = None


class ExecutionObservation(BaseModel):
    """Terminal provider state."""

    status: str
    exit_code: int | None = None
    oom_killed: bool = False
    started_at: str | None = None
    finished_at: str | None = None


# Compatibility alias for callers that still describe the Docker implementation.
OciExecutionSpec = SandboxExecutionSpec
