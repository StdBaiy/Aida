"""Domain models and extension contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class TimelineStatus(StrEnum):
    """Timeline lifecycle."""

    ACTIVE = "active"
    READ_ONLY = "read_only"


class CommandRequest(BaseModel):
    """A structured process invocation."""

    argv: list[str] = Field(min_length=1, max_length=64)
    cwd: str = "."
    timeout_seconds: int = Field(default=120, ge=1, le=1800)


class CommandResult(BaseModel):
    """Bounded process result."""

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int
    stdout_artifact_id: str | None = None
    stderr_artifact_id: str | None = None
    sandbox_id: str | None = None
    provider: str | None = None
    container_id: str | None = None
    image_digest: str | None = None
    isolation_level: str | None = None
    termination_reason: str | None = None
    resource_usage: dict[str, Any] | None = None
    policy_decision_id: str | None = None
    sandbox_policy_digest: str | None = None
    error_code: str | None = None


class SessionRecord(BaseModel):
    """Persisted CLI session."""

    session_id: str
    repo_root: str
    workspace_root: str
    model: str
    active_timeline_id: str
    created_at: datetime


class TimelineRecord(BaseModel):
    """A restorable branch of agent and file state."""

    timeline_id: str
    session_id: str
    thread_id: str
    status: TimelineStatus
    head_checkpoint_id: str | None = None
    forked_from_timeline_id: str | None = None
    forked_from_turn_number: int | None = None
    created_at: datetime


class TurnRecord(BaseModel):
    """A user-visible turn and the checkpoint it leaves active."""

    turn_id: str
    timeline_id: str
    turn_number: int
    checkpoint_id: str
    snapshot_oid: str
    user_text: str
    assistant_text: str
    status: str = "completed"
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None


class AgentContextState(BaseModel):
    """Latest durable context-window state for one Agent owner."""

    context_owner_id: str
    session_id: str
    timeline_id: str | None = None
    attempt_id: str | None = None
    used_tokens: int = 0
    max_tokens: int
    usage_ratio: float = 0.0
    message_count: int = 0
    compression_count: int = 0
    last_strategy: str | None = None
    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0
    cumulative_prompt_tokens: int = 0
    cumulative_completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    summaries: list[dict[str, str]] = Field(default_factory=list)
    last_compaction_id: str | None = None
    updated_at: datetime


@dataclass(frozen=True)
class Workspace:
    """Resolved Git and workspace paths."""

    repo_root: Path
    root: Path
    git_dir: Path
    data_dir: Path


class ExecutionBackend(Protocol):
    """Replaceable command execution boundary."""

    def run(self, request: CommandRequest) -> CommandResult:
        """Execute a previously approved request."""
        ...


class ApprovalProvider(Protocol):
    """User approval boundary."""

    def approve(self, request: CommandRequest) -> bool:
        """Return whether one exact command request may execute."""
        ...


class SnapshotStore(Protocol):
    """Workspace snapshot boundary."""

    def create(self, *, ref: str, parent_oid: str | None, reason: str) -> str:
        """Create a Git object snapshot without touching the real index."""
        ...

    def restore(self, target_oid: str) -> None:
        """Restore only the configured workspace from a snapshot."""
        ...


class CheckpointRepository(Protocol):
    """Session metadata persistence boundary."""

    def create_session(self, workspace: Workspace, model: str) -> SessionRecord:
        """Create a session and active timeline."""
        ...

    def get_session(self, session_id: str) -> SessionRecord:
        """Load one session."""
        ...

    def active_timeline(self, session_id: str) -> TimelineRecord:
        """Load the active timeline."""
        ...

    def turns(self, timeline_id: str) -> list[TurnRecord]:
        """List committed turns."""
        ...


class ArtifactStore(Protocol):
    """Content-addressed local artifact boundary."""

    def put(self, content: bytes) -> tuple[str, str]:
        """Persist bytes and return an artifact ID and relative path."""
        ...


class TraceExporter(Protocol):
    """Remote metrics export boundary."""

    def export(self, trace_id: str) -> bool:
        """Export one local trace as privacy-safe metrics."""
        ...

    def build_payload(self, trace_id: str) -> dict[str, Any]:
        """Build the fixed remote metrics payload."""
        ...
