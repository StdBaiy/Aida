"""Host execution boundary for explicitly configured Skill commands."""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from coding_agent.errors import fail
from coding_agent.execution.host import HostExecutionBackend, OutputCallback
from coding_agent.execution.policy import CommandPolicy
from coding_agent.models import CommandRequest, CommandResult
from coding_agent.skills import TrustedSkillCommand
from coding_agent.workspace.paths import PathGuard

if TYPE_CHECKING:
    from coding_agent.tracing.recorder import TraceRecorder


class TrustedSkillExecutionService:
    """Execute only pre-bound, approved Skill capabilities on the host."""

    def __init__(self, workspace_root: Path, *, max_output_bytes: int) -> None:
        self._backend = HostExecutionBackend(
            PathGuard(workspace_root.resolve(strict=True)),
            max_output_bytes=max_output_bytes,
        )
        self._policy = CommandPolicy()

    def run(
        self,
        capability: TrustedSkillCommand,
        args: list[str],
        *,
        extra_env: Mapping[str, str] | None = None,
        cancel_event: threading.Event | None = None,
        on_output: OutputCallback | None = None,
        recorder: TraceRecorder | None = None,
    ) -> CommandResult:
        """Verify the pinned executable and invoke it without a shell."""
        executable = Path(capability.argv[0])
        if not executable.is_file() or _sha256_file(executable) != capability.executable_digest:
            raise fail(
                "TRUSTED_SKILL_EXECUTABLE_CHANGED",
                f"Trusted Skill executable changed; restart and approve again: {executable}",
            )
        request = CommandRequest(
            argv=[*capability.argv, *args],
            cwd=".",
            timeout_seconds=capability.timeout_seconds,
        )
        self._policy.validate(request)
        decision_id = str(uuid.uuid4())
        result = self._backend.run(
            request,
            extra_env=extra_env,
            cancel_event=cancel_event,
            on_output=on_output,
            recorder=recorder,
        )
        cancelled = cancel_event is not None and cancel_event.is_set()
        result.provider = "trusted_host"
        result.isolation_level = "none"
        result.policy_decision_id = decision_id
        result.termination_reason = (
            "cancelled" if cancelled else "timeout" if result.timed_out else "exited"
        )
        result.error_code = (
            "TRUSTED_SKILL_CANCELLED"
            if cancelled
            else "TRUSTED_SKILL_TIMEOUT"
            if result.timed_out
            else None
        )
        return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise fail(
            "TRUSTED_SKILL_EXECUTABLE_CHANGED",
            f"Cannot verify trusted Skill executable {path}: {exc}",
        ) from exc
    return digest.hexdigest()
