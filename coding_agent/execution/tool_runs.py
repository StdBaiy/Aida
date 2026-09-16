"""Turn-scoped background tool execution and scheduler-facing inspection."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Generator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.tools import BaseTool, StructuredTool, tool

from coding_agent.errors import fail
from coding_agent.models import utc_now
from coding_agent.repository import new_id
from coding_agent.tracing.recorder import TraceRecorder
from coding_agent.tracing.redaction import redact

ToolRunStatus = Literal[
    "queued",
    "running",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
]
OutputCallback = Callable[[str, bytes], None]
ToolWork = Callable[[threading.Event, OutputCallback], Any]
ToolEventCallback = Callable[[str, dict[str, Any]], None]
_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


@dataclass(frozen=True)
class ToolTurnContext:
    """Execution ownership propagated explicitly when a graph turn starts."""

    thread_id: str
    recorder: TraceRecorder | None
    event_callback: ToolEventCallback | None


_ACTIVE_TOOL_TURN: ContextVar[ToolTurnContext | None] = ContextVar(
    "active_tool_turn",
    default=None,
)
_ACTIVE_TOOL_RUN_ID: ContextVar[str | None] = ContextVar(
    "active_tool_run_id",
    default=None,
)


@contextmanager
def activate_tool_turn(
    thread_id: str,
    recorder: TraceRecorder | None,
    event_callback: ToolEventCallback | None = None,
) -> Generator[None]:
    """Expose turn ownership to tools while keeping it out of model arguments."""
    token = _ACTIVE_TOOL_TURN.set(ToolTurnContext(thread_id, recorder, event_callback))
    try:
        yield
    finally:
        _ACTIVE_TOOL_TURN.reset(token)


@dataclass
class _OutputChunk:
    cursor: int
    stream: str
    text: str
    byte_size: int


@dataclass
class _ToolRun:
    run_id: str
    thread_id: str
    name: str
    effect: str
    execution_group_id: str | None
    recorder: TraceRecorder | None
    event_callback: ToolEventCallback | None
    span_id: str | None
    created_at: str
    started_monotonic: float
    status: ToolRunStatus = "queued"
    started_at: str | None = None
    ended_at: str | None = None
    last_output_at: str | None = None
    last_output_monotonic: float | None = None
    result: Any = None
    error: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    future: Future[None] | None = None
    chunks: list[_OutputChunk] = field(default_factory=list)
    retained_output_bytes: int = 0
    emitted_output_bytes: int = 0
    emitted_output_truncated: bool = False
    next_cursor: int = 1
    settled: bool = False


@dataclass
class _ExecutionGroup:
    group_id: str
    thread_id: str
    recorder: TraceRecorder
    span_id: str
    run_ids: set[str] = field(default_factory=set)
    active_count: int = 0
    peak_parallelism: int = 0


class ToolRunManager:
    """Execute long-running tools concurrently within one graph turn."""

    def __init__(self, *, max_workers: int = 4, max_output_bytes: int = 65_536) -> None:
        self.max_workers = max_workers
        self.max_output_bytes = max_output_bytes
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="coding-agent-tool",
        )
        self._condition = threading.Condition(threading.RLock())
        self._runs: dict[str, _ToolRun] = {}
        self._groups: dict[tuple[str, str], _ExecutionGroup] = {}
        self._sealed_threads: set[str] = set()
        self._peak_parallelism: dict[str, int] = {}

        @tool
        def inspect_tool_run(run_id: str, after_cursor: int = 0) -> dict[str, Any]:
            """Inspect one background tool's status and output since a cursor."""
            return self.inspect_current(run_id, after_cursor=after_cursor)

        @tool
        def wait_for_tools(
            run_ids: list[str],
            timeout_seconds: int = 15,
        ) -> dict[str, Any]:
            """Wait until a tool completes or the scheduler probe interval expires."""
            return self.wait_current(run_ids, timeout_seconds=timeout_seconds)

        @tool
        def cancel_tool_run(run_id: str, reason: str) -> dict[str, Any]:
            """Request cancellation of one background tool execution."""
            return self.cancel_current(run_id, reason=reason)

        self.tools: list[BaseTool] = [
            inspect_tool_run,
            wait_for_tools,
            cancel_tool_run,
        ]

    def begin_turn(self, thread_id: str) -> None:
        """Discard prior terminal runs and reject overlapping turns."""
        with self._condition:
            self._sealed_threads.discard(thread_id)
            self._peak_parallelism.pop(thread_id, None)
            active = [
                run.run_id
                for run in self._runs.values()
                if run.thread_id == thread_id and not run.settled
            ]
            if active:
                raise fail(
                    "TOOL_RUNS_ACTIVE",
                    f"Thread still owns active tool runs: {', '.join(active)}",
                )
            self._runs = {
                run_id: run
                for run_id, run in self._runs.items()
                if run.thread_id != thread_id
            }
            self._groups = {
                key: group
                for key, group in self._groups.items()
                if group.thread_id != thread_id
            }

    def wrap_tool(self, remote_tool: BaseTool, *, effect: str = "external") -> BaseTool:
        """Expose a synchronous third-party tool as a background submission."""
        if remote_tool.args_schema is None:
            raise fail(
                "TOOL_SCHEMA_REQUIRED",
                f"Background tool {remote_tool.name} requires a structured input schema.",
            )

        def invoke(**arguments: Any) -> dict[str, Any]:
            return self.start_current(
                name=remote_tool.name,
                effect=effect,
                work=lambda _cancel_event, _on_output: remote_tool.invoke(arguments),
            )

        return StructuredTool(
            name=remote_tool.name,
            description=(
                f"Start {remote_tool.description or remote_tool.name} in the background. "
                "Returns a run ID for inspect_tool_run or wait_for_tools."
            ),
            args_schema=remote_tool.args_schema,
            func=invoke,
            metadata=remote_tool.metadata,
            tags=remote_tool.tags,
            handle_tool_error=remote_tool.handle_tool_error,
            handle_validation_error=remote_tool.handle_validation_error,
        )

    def start_current(
        self,
        *,
        name: str,
        work: ToolWork,
        effect: str,
        execution_group_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit work owned by the active graph turn."""
        context = self._require_context()
        return self.start(
            thread_id=context.thread_id,
            recorder=context.recorder,
            event_callback=context.event_callback,
            name=name,
            work=work,
            effect=effect,
            execution_group_id=execution_group_id,
        )

    def start(
        self,
        *,
        thread_id: str,
        recorder: TraceRecorder | None,
        event_callback: ToolEventCallback | None,
        name: str,
        work: ToolWork,
        effect: str,
        execution_group_id: str | None = None,
    ) -> dict[str, Any]:
        """Submit one background callable and return a stable task handle."""
        run_id = new_id()
        parent_span_id = None
        with self._condition:
            if thread_id in self._sealed_threads:
                raise fail(
                    "TOOL_TURN_SEALED",
                    "The turn is finalizing and cannot start another tool.",
                )
            if self._active_count(thread_id) >= self.max_workers:
                raise fail(
                    "TOOL_CONCURRENCY_LIMIT",
                    f"At most {self.max_workers} tools may run concurrently in one turn.",
                )
            if recorder is not None and execution_group_id:
                key = (thread_id, execution_group_id)
                group = self._groups.get(key)
                if group is None:
                    group_span_id = recorder.start_span(
                        "tool.group",
                        kind="scheduler",
                        attributes={"execution_group_id": execution_group_id},
                    )
                    group = _ExecutionGroup(
                        execution_group_id,
                        thread_id,
                        recorder,
                        group_span_id,
                    )
                    self._groups[key] = group
                group.run_ids.add(run_id)
                parent_span_id = group.span_id
            span_id = (
                recorder.start_span(
                    "tool.run",
                    kind="tool_run",
                    parent_span_id=parent_span_id,
                    attributes={
                        "tool_run_id": run_id,
                        "tool_name": name,
                        "effect": effect,
                        "execution_group_id": execution_group_id,
                    },
                )
                if recorder is not None
                else None
            )
            run = _ToolRun(
                run_id=run_id,
                thread_id=thread_id,
                name=name,
                effect=effect,
                execution_group_id=execution_group_id,
                recorder=recorder,
                event_callback=event_callback,
                span_id=span_id,
                created_at=utc_now().isoformat(),
                started_monotonic=time.monotonic(),
            )
            self._runs[run_id] = run
            self._emit(
                run,
                "tool.started",
                {
                    "status": "queued",
                    "created_at": run.created_at,
                },
            )
            run.future = self._executor.submit(self._execute, run, work)
            self._condition.notify_all()
        return {
            "ok": True,
            "run_id": run_id,
            "status": "queued",
            "tool": name,
            "execution_group_id": execution_group_id,
        }

    def inspect_current(self, run_id: str, *, after_cursor: int = 0) -> dict[str, Any]:
        context = self._require_context()
        return self.inspect(context.thread_id, run_id, after_cursor=after_cursor)

    def inspect(self, thread_id: str, run_id: str, *, after_cursor: int = 0) -> dict[str, Any]:
        """Return one ownership-checked snapshot with bounded incremental output."""
        with self._condition:
            run = self._owned_run(thread_id, run_id)
            if run is None:
                return {
                    "ok": False,
                    "error_code": "TOOL_RUN_STALE",
                    "message": (
                        "This tool run belongs to a previous (cancelled or completed) "
                        "turn and is no longer inspectable."
                    ),
                }
            now = time.monotonic()
            chunks = [
                {
                    "cursor": chunk.cursor,
                    "stream": chunk.stream,
                    "text": chunk.text,
                }
                for chunk in run.chunks
                if chunk.cursor > after_cursor
            ]
            return {
                "ok": True,
                "run_id": run.run_id,
                "tool": run.name,
                "status": run.status,
                "effect": run.effect,
                "execution_group_id": run.execution_group_id,
                "elapsed_seconds": round(now - run.started_monotonic, 3),
                "idle_seconds": (
                    round(now - run.last_output_monotonic, 3)
                    if run.last_output_monotonic is not None
                    else round(now - run.started_monotonic, 3)
                ),
                "next_cursor": run.next_cursor - 1,
                "output_truncated": bool(
                    run.chunks and after_cursor < run.chunks[0].cursor - 1
                ),
                "output": chunks,
                "result": run.result if run.status in _TERMINAL_STATUSES else None,
                "error": run.error,
            }

    def wait_current(self, run_ids: list[str], *, timeout_seconds: int) -> dict[str, Any]:
        context = self._require_context()
        span_id = (
            context.recorder.start_span(
                "scheduler.wait",
                kind="scheduler",
                attributes={
                    "tool_run_ids": run_ids,
                    "timeout_seconds": timeout_seconds,
                },
            )
            if context.recorder is not None
            else None
        )
        try:
            result = self.wait(
                context.thread_id,
                run_ids,
                timeout_seconds=timeout_seconds,
            )
        except BaseException as exc:
            if context.recorder is not None and span_id is not None:
                context.recorder.end_span(span_id, status="error", error=exc)
            raise
        if context.recorder is not None and span_id is not None:
            for tool_span_id in self.span_ids(context.thread_id, run_ids):
                context.recorder.link_spans(tool_span_id, span_id, "observed_by")
            context.recorder.end_span(
                span_id,
                attributes={"wake_reason": result["wake_reason"]},
            )
        return result

    def wait(
        self,
        thread_id: str,
        run_ids: list[str],
        *,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """Wait for a terminal result or a bounded scheduler probe."""
        if not run_ids:
            raise fail("TOOL_RUN_REQUIRED", "wait_for_tools requires at least one run ID.")
        timeout = min(max(timeout_seconds, 1), 120)
        deadline = time.monotonic() + timeout
        stale: list[str] = []
        with self._condition:
            for run_id in run_ids:
                if self._owned_run(thread_id, run_id) is None:
                    stale.append(run_id)
            remaining_ids = [rid for rid in run_ids if rid not in stale]
            if remaining_ids:
                while not any(
                    self._runs[run_id].status in _TERMINAL_STATUSES
                    for run_id in remaining_ids
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(timeout=remaining)
                completed = [
                    run_id
                    for run_id in remaining_ids
                    if self._runs[run_id].status in _TERMINAL_STATUSES
                ]
            else:
                completed = []
        snapshots = []
        for run_id in run_ids:
            snapshot = self.inspect(thread_id, run_id)
            snapshots.append(
                {
                    key: value
                    for key, value in snapshot.items()
                    if key not in {"output", "result"}
                }
            )
        return {
            "ok": True,
            "wake_reason": (
                "tool_terminal" if completed
                else "tool_stale" if stale
                else "probe_timeout"
            ),
            "completed_run_ids": completed,
            "stale_run_ids": stale,
            "runs": snapshots,
        }

    def cancel_current(self, run_id: str, *, reason: str) -> dict[str, Any]:
        context = self._require_context()
        return self.cancel(context.thread_id, run_id, reason=reason)

    def cancel(self, thread_id: str, run_id: str, *, reason: str) -> dict[str, Any]:
        """Signal one task; subprocess-backed tools terminate their process group."""
        with self._condition:
            run = self._owned_run(thread_id, run_id)
            if run is None:
                return {
                    "ok": True,
                    "run_id": run_id,
                    "status": "cancelled",
                    "already_terminal": True,
                    "stale": True,
                }
            if run.status in _TERMINAL_STATUSES:
                return {
                    "ok": True,
                    "run_id": run_id,
                    "status": run.status,
                    "already_terminal": True,
                }
            run.status = "cancelling"
            run.cancel_event.set()
            self._emit(
                run,
                "tool.cancelling",
                {"status": "cancelling", "reason": reason},
            )
            if run.recorder is not None and run.span_id is not None:
                cancel_span = run.recorder.start_span(
                    "tool.cancel",
                    kind="scheduler",
                    attributes={"tool_run_id": run_id, "reason": reason},
                )
                run.recorder.end_span(cancel_span)
            self._condition.notify_all()
            return {"ok": True, "run_id": run_id, "status": "cancelling"}

    def active_run_ids(self, thread_id: str) -> list[str]:
        with self._condition:
            return [
                run.run_id
                for run in self._runs.values()
                if run.thread_id == thread_id and not run.settled
            ]

    def run_ids(self, thread_id: str) -> list[str]:
        """List all runs owned by the current turn."""
        with self._condition:
            return [
                run.run_id for run in self._runs.values() if run.thread_id == thread_id
            ]

    def wait_for_thread(
        self,
        thread_id: str,
        *,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        run_ids = self.active_run_ids(thread_id)
        if not run_ids:
            return {"wake_reason": "all_terminal", "completed_run_ids": [], "runs": []}
        return self.wait(thread_id, run_ids, timeout_seconds=timeout_seconds)

    def cancel_thread(self, thread_id: str, *, reason: str) -> None:
        for run_id in self.active_run_ids(thread_id):
            self.cancel(thread_id, run_id, reason=reason)

    def cancel_all(self, *, reason: str) -> list[str]:
        """Cancel every active run and return the affected thread IDs."""
        with self._condition:
            active = [
                (run.thread_id, run.run_id)
                for run in self._runs.values()
                if not run.settled
            ]
        for thread_id, run_id in active:
            self.cancel(thread_id, run_id, reason=reason)
        return sorted({thread_id for thread_id, _ in active})

    def seal_thread(self, thread_id: str) -> None:
        """Reject new submissions while a turn produces its final response."""
        with self._condition:
            self._sealed_threads.add(thread_id)

    def finalize_thread(self, thread_id: str) -> None:
        """Close execution-group spans after every member is terminal."""
        with self._condition:
            if self._active_count(thread_id):
                raise fail(
                    "TOOL_RUNS_ACTIVE",
                    "Cannot finalize a turn while background tools are active.",
                )
            groups = [
                group
                for group in self._groups.values()
                if group.thread_id == thread_id
            ]
            for group in groups:
                self._groups.pop((thread_id, group.group_id), None)
            runs = [run for run in self._runs.values() if run.thread_id == thread_id]
            peak_parallelism = self._peak_parallelism.get(thread_id, 0)
        for group in groups:
            members = [self._runs[run_id] for run_id in group.run_ids]
            statuses = {member.status for member in members}
            group.recorder.end_span(
                group.span_id,
                status="error" if "failed" in statuses else "ok",
                attributes={
                    "execution_group_id": group.group_id,
                    "run_count": len(members),
                    "completed_count": sum(
                        member.status == "completed" for member in members
                    ),
                    "cancelled_count": sum(
                        member.status == "cancelled" for member in members
                    ),
                    "failed_count": sum(member.status == "failed" for member in members),
                    "peak_parallelism": group.peak_parallelism,
                },
            )
        recorder = next((run.recorder for run in runs if run.recorder is not None), None)
        if recorder is not None and runs:
            summary_span = recorder.start_span(
                "tool.scheduler.summary",
                kind="scheduler",
            )
            recorder.end_span(
                summary_span,
                attributes={
                    "run_count": len(runs),
                    "completed_count": sum(run.status == "completed" for run in runs),
                    "cancelled_count": sum(run.status == "cancelled" for run in runs),
                    "failed_count": sum(run.status == "failed" for run in runs),
                    "peak_parallelism": peak_parallelism,
                },
            )

    def drain_thread(self, thread_id: str, *, timeout_seconds: int) -> bool:
        """Wait until every run reaches a terminal state."""
        deadline = time.monotonic() + max(timeout_seconds, 0)
        with self._condition:
            while self._active_count(thread_id):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def close(self) -> None:
        """Cancel outstanding tasks and release worker threads."""
        with self._condition:
            thread_ids = {run.thread_id for run in self._runs.values()}
        for thread_id in thread_ids:
            self.cancel_thread(thread_id, reason="runtime closing")
        self._executor.shutdown(wait=True, cancel_futures=False)

    def span_ids(self, thread_id: str, run_ids: list[str]) -> list[str]:
        with self._condition:
            return [
                run.span_id
                for run_id in run_ids
                if (run := self._runs.get(run_id)) is not None
                and run.thread_id == thread_id
                and run.span_id is not None
            ]

    def _execute(self, run: _ToolRun, work: ToolWork) -> None:
        with self._condition:
            if run.cancel_event.is_set():
                run.status = "cancelled"
                run.ended_at = utc_now().isoformat()
                if run.recorder is not None and run.span_id is not None:
                    run.recorder.end_span(
                        run.span_id,
                        status="cancelled",
                        attributes=self._trace_attributes(run),
                    )
                self._emit(run, "tool.cancelled", self._terminal_event(run))
                run.settled = True
                self._condition.notify_all()
                return
            run.status = "running"
            run.started_at = utc_now().isoformat()
            running_count = sum(
                candidate.thread_id == run.thread_id and candidate.status == "running"
                for candidate in self._runs.values()
            )
            self._peak_parallelism[run.thread_id] = max(
                self._peak_parallelism.get(run.thread_id, 0),
                running_count,
            )
            group = self._group_for(run)
            if group is not None:
                group.active_count += 1
                group.peak_parallelism = max(group.peak_parallelism, group.active_count)
            self._condition.notify_all()
        self._emit(
            run,
            "tool.running",
            {"status": "running", "started_at": run.started_at},
        )
        token = _ACTIVE_TOOL_RUN_ID.set(run.run_id)
        try:
            result = work(
                run.cancel_event,
                lambda stream, chunk: self._record_output(run.run_id, stream, chunk),
            )
        except BaseException as exc:
            with self._condition:
                run.status = "cancelled" if run.cancel_event.is_set() else "failed"
                run.error = str(exc)
                run.ended_at = utc_now().isoformat()
            if run.recorder is not None and run.span_id is not None:
                run.recorder.end_span(
                    run.span_id,
                    status="cancelled" if run.cancel_event.is_set() else "error",
                    error=exc,
                    attributes=self._trace_attributes(run),
                )
        else:
            failed = self._result_failed(result)
            with self._condition:
                run.result = result
                run.status = (
                    "cancelled"
                    if run.cancel_event.is_set()
                    else "failed"
                    if failed
                    else "completed"
                )
                run.ended_at = utc_now().isoformat()
            if run.recorder is not None and run.span_id is not None:
                run.recorder.end_span(
                    run.span_id,
                    status=(
                        "cancelled"
                        if run.cancel_event.is_set()
                        else "error"
                        if failed
                        else "ok"
                    ),
                    output=result,
                    attributes=self._trace_attributes(run),
                )
        finally:
            _ACTIVE_TOOL_RUN_ID.reset(token)
            with self._condition:
                group = self._group_for(run)
                if group is not None:
                    group.active_count = max(0, group.active_count - 1)
            self._emit(
                run,
                f"tool.{run.status}",
                self._terminal_event(run),
            )
            with self._condition:
                run.settled = True
                self._condition.notify_all()
    def _record_output(self, run_id: str, stream: str, chunk: bytes) -> None:
        text = chunk.decode(errors="replace")
        with self._condition:
            run = self._runs[run_id]
            output = _OutputChunk(run.next_cursor, stream, text, len(chunk))
            run.next_cursor += 1
            run.chunks.append(output)
            run.retained_output_bytes += output.byte_size
            while run.chunks and run.retained_output_bytes > self.max_output_bytes:
                removed = run.chunks.pop(0)
                run.retained_output_bytes -= removed.byte_size
            run.last_output_at = utc_now().isoformat()
            run.last_output_monotonic = time.monotonic()
            self._condition.notify_all()
            event_callback = run.event_callback
            recorder = run.recorder
            cursor = output.cursor
            remaining_event_bytes = max(0, self.max_output_bytes - run.emitted_output_bytes)
            event_chunk = chunk[:remaining_event_bytes]
            run.emitted_output_bytes += len(event_chunk)
            if len(event_chunk) < len(chunk):
                run.emitted_output_truncated = True
        if event_callback is not None and event_chunk:
            safe_text = str(
                redact(
                    event_chunk.decode(errors="replace"),
                    secrets=recorder.secrets if recorder is not None else (),
                )
            )
            self._safe_emit(
                event_callback,
                "tool.output",
                {
                    "run_id": run_id,
                    "cursor": cursor,
                    "stream": stream,
                    "text": safe_text,
                },
            )

    def _trace_attributes(self, run: _ToolRun) -> dict[str, Any]:
        return {
            "tool_run_id": run.run_id,
            "tool_name": run.name,
            "effect": run.effect,
            "execution_group_id": run.execution_group_id,
            "output_chunk_count": run.next_cursor - 1,
            "retained_output_bytes": run.retained_output_bytes,
            "cancelled": run.cancel_event.is_set(),
        }

    def _owned_run(self, thread_id: str, run_id: str) -> _ToolRun | None:
        run = self._runs.get(run_id)
        if run is None or run.thread_id != thread_id:
            return None
        return run

    def _require_run(self, thread_id: str, run_id: str) -> _ToolRun:
        run = self._owned_run(thread_id, run_id)
        if run is None:
            raise fail("TOOL_RUN_NOT_FOUND", "Tool run does not exist in this turn.")
        return run

    def _active_count(self, thread_id: str) -> int:
        return sum(
            run.thread_id == thread_id and not run.settled
            for run in self._runs.values()
        )

    def _group_for(self, run: _ToolRun) -> _ExecutionGroup | None:
        if run.execution_group_id is None:
            return None
        return self._groups.get((run.thread_id, run.execution_group_id))

    def _emit(self, run: _ToolRun, event_type: str, payload: dict[str, Any]) -> None:
        if run.event_callback is None:
            return
        self._safe_emit(
            run.event_callback,
            event_type,
            {
                "run_id": run.run_id,
                "tool": run.name,
                "effect": run.effect,
                "execution_group_id": run.execution_group_id,
                **payload,
            },
        )

    @staticmethod
    def _safe_emit(
        callback: ToolEventCallback,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        try:
            callback(event_type, payload)
        except Exception:
            return

    @staticmethod
    def _terminal_event(run: _ToolRun) -> dict[str, Any]:
        result = run.result if isinstance(run.result, dict) else {}
        preview = json.dumps(run.result, ensure_ascii=False, default=str)
        safe_preview = redact(
            preview[:2000],
            secrets=run.recorder.secrets if run.recorder is not None else (),
        )
        return {
            "status": run.status,
            "ended_at": run.ended_at,
            "exit_code": result.get("exit_code"),
            "timed_out": bool(result.get("timed_out", False)),
            "duration_ms": result.get("duration_ms"),
            "sandbox_id": result.get("sandbox_id"),
            "provider": result.get("provider"),
            "container_id": result.get("container_id"),
            "isolation_level": result.get("isolation_level"),
            "sandbox_policy_digest": result.get("sandbox_policy_digest"),
            "termination_reason": result.get("termination_reason"),
            "resource_usage": result.get("resource_usage"),
            "error": run.error,
            "result_preview": safe_preview,
            "output_truncated": run.emitted_output_truncated,
        }

    @staticmethod
    def _result_failed(result: Any) -> bool:
        if not isinstance(result, dict):
            return False
        if result.get("ok") is False or result.get("timed_out") is True:
            return True
        if result.get("termination_reason") not in {None, "exited"}:
            return True
        exit_code = result.get("exit_code")
        return exit_code is not None and exit_code != 0

    @staticmethod
    def _require_context() -> ToolTurnContext:
        context = _ACTIVE_TOOL_TURN.get()
        if context is None:
            raise fail("TOOL_CONTEXT_MISSING", "Background tools require an active graph turn.")
        return context


def active_tool_run_id() -> str | None:
    """Return the worker-owned ToolRun ID without exposing it to the model."""
    return _ACTIVE_TOOL_RUN_ID.get()
