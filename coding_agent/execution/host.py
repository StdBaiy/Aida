"""Host process execution."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, BinaryIO

from coding_agent.models import CommandRequest, CommandResult
from coding_agent.tracing.context import active_recorder
from coding_agent.workspace.paths import PathGuard

if TYPE_CHECKING:
    from coding_agent.tracing.recorder import TraceRecorder

OutputCallback = Callable[[str, bytes], None]


class HostExecutionBackend:
    """Run approved argv requests on the host with bounded output and time."""

    def __init__(self, guard: PathGuard, *, max_output_bytes: int = 65_536) -> None:
        self.guard = guard
        self.max_output_bytes = max_output_bytes

    def run(
        self,
        request: CommandRequest,
        *,
        extra_env: Mapping[str, str] | None = None,
        cancel_event: threading.Event | None = None,
        on_output: OutputCallback | None = None,
        recorder: TraceRecorder | None = None,
    ) -> CommandResult:
        """Execute without a shell while supporting output streaming and cancellation."""
        cwd = self.guard.validate_cwd(request.cwd)
        inherited = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"}
        }
        inherited.update(extra_env or {})
        started = time.monotonic()
        process = subprocess.Popen(
            request.argv,
            cwd=cwd,
            env=inherited,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        cancelled = False
        output_queue: queue.Queue[tuple[str, bytes] | None] = queue.Queue()
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []

        def read_stream(name: str, stream: BinaryIO) -> None:
            try:
                while chunk := os.read(stream.fileno(), 4096):
                    output_queue.put((name, chunk))
            finally:
                output_queue.put(None)

        if process.stdout is None or process.stderr is None:
            raise RuntimeError("Command pipes were not created.")
        readers = [
            threading.Thread(
                target=read_stream,
                args=("stdout", process.stdout),
                daemon=True,
                name=f"command-{process.pid}-stdout",
            ),
            threading.Thread(
                target=read_stream,
                args=("stderr", process.stderr),
                daemon=True,
                name=f"command-{process.pid}-stderr",
            ),
        ]
        for reader in readers:
            reader.start()

        deadline = started + request.timeout_seconds
        closed_streams = 0
        try:
            while closed_streams < len(readers) or process.poll() is None:
                now = time.monotonic()
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    self._terminate(process)
                elif now >= deadline:
                    timed_out = True
                    self._terminate(process)
                try:
                    item = output_queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item is None:
                    closed_streams += 1
                    continue
                stream_name, chunk = item
                if stream_name == "stdout":
                    stdout_parts.append(chunk)
                else:
                    stderr_parts.append(chunk)
                if on_output is not None:
                    on_output(stream_name, chunk)
                if timed_out or cancelled:
                    continue
            process.wait()
        finally:
            if process.poll() is None:
                self._terminate(process)
            for reader in readers:
                reader.join(timeout=1)

        stdout = b"".join(stdout_parts)
        stderr = b"".join(stderr_parts)
        if cancelled and not timed_out:
            stderr += b"\nCommand cancelled by scheduler.\n"
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout_artifact_id = stderr_artifact_id = None
        trace_recorder = recorder or active_recorder()
        if trace_recorder is not None:
            with suppress(Exception):
                stdout_artifact_id = trace_recorder.capture_text_artifact(stdout)
            with suppress(Exception):
                stderr_artifact_id = trace_recorder.capture_text_artifact(stderr)
        bounded_stdout, stdout_truncated = self._bound(stdout)
        bounded_stderr, stderr_truncated = self._bound(stderr)
        return CommandResult(
            exit_code=None if timed_out or cancelled else process.returncode,
            stdout=bounded_stdout,
            stderr=bounded_stderr,
            timed_out=timed_out,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            duration_ms=duration_ms,
            stdout_artifact_id=stdout_artifact_id,
            stderr_artifact_id=stderr_artifact_id,
        )

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        """Terminate a process group and escalate when it ignores SIGTERM."""
        if process.poll() is not None:
            return
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)

    def _bound(self, value: bytes) -> tuple[str, bool]:
        truncated = len(value) > self.max_output_bytes
        value = value[: self.max_output_bytes]
        lines = value.splitlines(keepends=True)
        if len(lines) > 2000:
            value = b"".join(lines[:2000])
            truncated = True
        return value.decode(errors="replace"), truncated
