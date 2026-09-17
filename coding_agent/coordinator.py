"""Joint graph checkpoint and Git snapshot coordination."""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from coding_agent.errors import CodingAgentError, fail
from coding_agent.models import utc_now
from coding_agent.repository import SqliteCheckpointRepository, new_id
from coding_agent.runtime import AgentRuntime, ApprovalCallback, TokenCallback, ToolEventCallback
from coding_agent.tracing.exporter import MetricsLangSmithExporter
from coding_agent.tracing.recorder import TraceRecorder, TraceStore
from coding_agent.workspace.git import GitSnapshotStore
from coding_agent.workspace.mutation import WorkspaceMutationGate


class TurnCancelled(CodingAgentError):
    """Raised when a turn is cancelled, carrying the promoted thread context."""

    def __init__(self, execution_thread_id: str, checkpoint_id: str) -> None:
        super().__init__("OPERATION_CANCELLED", "The Agent operation was cancelled.")
        self.execution_thread_id = execution_thread_id
        self.checkpoint_id = checkpoint_id


class TurnCoordinator:
    """Commit user-visible turns only after graph and filesystem state exist."""

    def __init__(
        self,
        *,
        session_id: str,
        repository: SqliteCheckpointRepository,
        runtime: AgentRuntime,
        snapshots: GitSnapshotStore,
        trace_store: TraceStore,
        trace_exporter: MetricsLangSmithExporter,
        model_id: str,
        trace_secrets: tuple[str, ...] = (),
        mutation_gate: WorkspaceMutationGate | None = None,
    ) -> None:
        self.session_id = session_id
        self.repository = repository
        self.runtime = runtime
        self.snapshots = snapshots
        self.trace_store = trace_store
        self.trace_exporter = trace_exporter
        self.model_id = model_id
        self.trace_secrets = trace_secrets
        self.mutation_gate = mutation_gate
        self._baseline_lock = threading.Lock()

    def ensure_baseline(self) -> None:
        """Create turn zero for a new session."""
        with self._baseline_lock:
            self._ensure_baseline_locked()

    def _ensure_baseline_locked(self) -> None:
        """Create turn zero after serializing concurrent bootstrap callers."""
        timeline = self.repository.active_timeline(self.session_id)
        turns = self.repository.turns(timeline.timeline_id)
        if turns:
            owner_id = self._context_owner_id(timeline.timeline_id)
            if self.repository.context_state(owner_id) is None:
                checkpoint_id = timeline.head_checkpoint_id or turns[-1].checkpoint_id
                self.repository.save_context_state(
                    context_owner_id=owner_id,
                    session_id=self.session_id,
                    timeline_id=timeline.timeline_id,
                    attempt_id=None,
                    snapshot=self.runtime.measure_context(
                        thread_id=timeline.thread_id,
                        checkpoint_id=checkpoint_id,
                    ),
                )
            return
        checkpoint_id = self.runtime.initialize_thread(timeline.thread_id)
        context_snapshot = self.runtime.measure_context(
            thread_id=timeline.thread_id,
            checkpoint_id=checkpoint_id,
        )
        snapshot_oid = self.snapshots.create(
            ref=self._timeline_ref(timeline.timeline_id),
            parent_oid=None,
            reason="baseline",
        )
        self.repository.add_turn(
            timeline_id=timeline.timeline_id,
            checkpoint_id=checkpoint_id,
            thread_id=timeline.thread_id,
            snapshot_oid=snapshot_oid,
            user_text="",
            assistant_text="",
            turn_number=0,
            context_owner_id=self._context_owner_id(timeline.timeline_id),
            context_snapshot=context_snapshot,
        )

    def run_turn(
        self,
        user_text: str,
        approve: ApprovalCallback,
        on_token: TokenCallback | None = None,
        on_tool_event: ToolEventCallback | None = None,
        cancelled: threading.Event | None = None,
        before_commit: Callable[[], bool] | None = None,
        *,
        message_origin: str = "user",
    ) -> str:
        """Run and jointly commit one user turn."""
        started_at = utc_now()
        timeline = self.repository.active_timeline(self.session_id)
        turns = self.repository.turns(timeline.timeline_id)
        parent_oid = turns[-1].snapshot_oid
        persisted_context = self.repository.context_state(
            self._context_owner_id(timeline.timeline_id)
        )
        if persisted_context is not None:
            self.runtime.accountant.restore(persisted_context.model_dump(mode="json"))
        execution_thread_id = new_id()
        self.runtime.fork_checkpoint(
            source_thread_id=timeline.thread_id,
            source_checkpoint_id=timeline.head_checkpoint_id or turns[-1].checkpoint_id,
            target_thread_id=execution_thread_id,
        )
        turn_id, trace_id, logical_run_id = new_id(), new_id(), new_id()
        snapshot_created = False
        recorder = TraceRecorder(
            store=self.trace_store,
            trace_id=trace_id,
            turn_id=turn_id,
            session_id=self.session_id,
            timeline_id=timeline.timeline_id,
            logical_run_id=logical_run_id,
            model_id=self.model_id,
            turn_number=turns[-1].turn_number + 1,
            secrets=self.trace_secrets,
        )
        try:
            with recorder.span(
                "agent.invoke",
                kind="agent",
                inputs={"user_message": user_text, "origin": message_origin},
            ) as invoke_span_id:
                assistant_text, checkpoint_id = self.runtime.run_turn(
                    thread_id=execution_thread_id,
                    user_text=user_text,
                    approve=approve,
                    recorder=recorder,
                    on_token=on_token,
                    on_tool_event=on_tool_event,
                    cancelled=cancelled,
                    **({"message_origin": message_origin} if message_origin != "user" else {}),
                )
                recorder.end_span(
                    invoke_span_id,
                    output={"assistant_message": assistant_text},
                )
            self._raise_if_cancelled(cancelled)
            if before_commit is not None and not before_commit():
                raise fail("OPERATION_CANCELLED", "The Agent operation was cancelled.")
            if self.mutation_gate is not None:
                self.mutation_gate.acquire(execution_thread_id, cancelled)
            with recorder.span("workspace.snapshot", kind="workspace") as snapshot_span_id:
                snapshot_oid = self.snapshots.create(
                    ref=self._timeline_ref(timeline.timeline_id),
                    parent_oid=parent_oid,
                    reason=f"turn {turns[-1].turn_number + 1}",
                )
                recorder.end_span(
                    snapshot_span_id,
                    output={"snapshot_oid": snapshot_oid},
                    attributes=self.snapshots.diff_stats(parent_oid, snapshot_oid),
                )
                snapshot_created = True
            self._raise_if_cancelled(cancelled)
            context_snapshot = self.runtime.measure_context(
                thread_id=execution_thread_id,
                checkpoint_id=checkpoint_id,
            )
            self.repository.add_turn(
                timeline_id=timeline.timeline_id,
                checkpoint_id=checkpoint_id,
                thread_id=execution_thread_id,
                snapshot_oid=snapshot_oid,
                user_text=user_text,
                assistant_text=assistant_text,
                turn_id=turn_id,
                started_at=started_at,
                context_owner_id=self._context_owner_id(timeline.timeline_id),
                context_snapshot=context_snapshot,
            )
        except BaseException as exc:
            owns_workspace = (
                self.mutation_gate is not None
                and self.mutation_gate.owns(execution_thread_id)
            )
            if owns_workspace:
                with suppress(Exception):
                    self.snapshots.restore(parent_oid)
                if snapshot_created:
                    with suppress(Exception):
                        self.snapshots.point_ref(
                            self._timeline_ref(timeline.timeline_id),
                            parent_oid,
                        )
            is_cancelled = (
                isinstance(exc, CodingAgentError)
                and exc.code == "OPERATION_CANCELLED"
            )
            if is_cancelled:
                try:
                    checkpoint_id = self.runtime.annotate_cancelled_context(
                        thread_id=execution_thread_id,
                    )
                except BaseException:
                    state = self.runtime.graph.get_state(
                        self.runtime.graph_config(execution_thread_id),
                    )
                    checkpoint_id = str(
                        state.config["configurable"]["checkpoint_id"]
                    )
                recorder.finish(status="cancelled", error=exc)
                self._export_trace(recorder)
                raise TurnCancelled(execution_thread_id, checkpoint_id) from exc
            recorder.finish(status="failed", error=exc)
            self._export_trace(recorder)
            raise
        finally:
            if self.mutation_gate is not None:
                self.mutation_gate.release(execution_thread_id)
        recorder.finish(status="completed")
        self._export_trace(recorder)
        return assistant_text

    @staticmethod
    def _raise_if_cancelled(cancelled: threading.Event | None) -> None:
        if cancelled is not None and cancelled.is_set():
            raise fail("OPERATION_CANCELLED", "The Agent operation was cancelled.")

    def history(self) -> list[dict[str, Any]]:
        """Return compact active-timeline history."""
        timeline = self.repository.active_timeline(self.session_id)
        return [
            {
                "turn": turn.turn_number,
                "created_at": turn.created_at.isoformat(),
                "request": turn.user_text[:80],
                "response": turn.assistant_text[:120],
                "snapshot": turn.snapshot_oid[:12],
            }
            for turn in self.repository.history_turns(timeline.timeline_id)
        ]

    def context_snapshot(self) -> dict[str, Any]:
        timeline = self.repository.active_timeline(self.session_id)
        state = self.repository.context_state(self._context_owner_id(timeline.timeline_id))
        if state is None:
            self.ensure_baseline()
            state = self.repository.context_state(
                self._context_owner_id(timeline.timeline_id)
            )
        if state is None:
            raise fail("CONTEXT_STATE_MISSING", "Context state could not be initialized.")
        return state.model_dump(mode="json")

    def compact_context(self, *, trigger: str, force: bool = False) -> dict[str, Any] | None:
        """Compact only the current committed timeline head."""
        timeline = self.repository.active_timeline(self.session_id)
        state = self.context_snapshot()
        if not force and (
            not self.runtime.context_compaction_enabled
            or float(state.get("usage_ratio", 0.0))
            < self.runtime.context_config.soft_limit
            / max(self.runtime.context_config.hard_limit, 1)
        ):
            return None
        turns = self.repository.turns(timeline.timeline_id)
        if not turns:
            raise fail("CONTEXT_STATE_MISSING", "The active timeline has no checkpoint.")
        base_checkpoint_id = timeline.head_checkpoint_id or turns[-1].checkpoint_id
        before_tokens = int(state.get("used_tokens", 0) or 0)
        result = self.runtime.compact_context(
            thread_id=timeline.thread_id,
            checkpoint_id=base_checkpoint_id,
            summaries=list(state.get("summaries", [])),
        )
        usage = {
            **result["usage"],
            "summaries": result["summaries"],
        }
        return self.repository.commit_context_compaction(
            context_owner_id=self._context_owner_id(timeline.timeline_id),
            session_id=self.session_id,
            timeline_id=timeline.timeline_id,
            thread_id=timeline.thread_id,
            base_checkpoint_id=base_checkpoint_id,
            result_checkpoint_id=str(result["checkpoint_id"]),
            trigger=trigger,
            summary_id=str(result["summary_id"]),
            before_tokens=before_tokens,
            snapshot=usage,
        )

    def restore(self, turn_number: int) -> None:
        """Fork graph state and workspace state from a committed turn."""
        source = self.repository.active_timeline(self.session_id)
        source_turn = self.repository.get_turn(source.timeline_id, turn_number)
        current_turns = self.repository.turns(source.timeline_id)
        current_oid = current_turns[-1].snapshot_oid
        target_thread_id = new_id()
        target_checkpoint_id = self.runtime.fork_checkpoint(
            source_thread_id=source.thread_id,
            source_checkpoint_id=source_turn.checkpoint_id,
            target_thread_id=target_thread_id,
        )
        self.snapshots.restore(source_turn.snapshot_oid)
        try:
            target_timeline_id = new_id()
            self.snapshots.point_ref(
                self._timeline_ref(target_timeline_id),
                source_turn.snapshot_oid,
            )
            self.repository.fork(
                session_id=self.session_id,
                source=source,
                source_turn=source_turn,
                thread_id=target_thread_id,
                checkpoint_id=target_checkpoint_id,
                timeline_id=target_timeline_id,
            )
            self.repository.save_context_state(
                context_owner_id=self._context_owner_id(target_timeline_id),
                session_id=self.session_id,
                timeline_id=target_timeline_id,
                attempt_id=None,
                snapshot=self.runtime.measure_context(
                    thread_id=target_thread_id,
                    checkpoint_id=target_checkpoint_id,
                ),
            )
        except Exception:
            self.snapshots.restore(current_oid)
            raise

    def trace(self, turn_number: int) -> dict[str, Any]:
        """Return the local trace summary for one active-timeline turn."""
        timeline = self.repository.active_timeline(self.session_id)
        turn = self.repository.get_turn(timeline.timeline_id, turn_number)
        summary = self.trace_store.trace_for_turn_id(turn.turn_id)
        if summary is None:
            raise ValueError(f"No trace exists for turn {turn_number}.")
        return summary

    def turn_context(self, turn_number: int) -> dict[str, Any]:
        """Return the model-visible checkpoint context committed by one turn."""
        timeline = self.repository.active_timeline(self.session_id)
        turn = self.repository.get_turn(timeline.timeline_id, turn_number)
        return {
            "turn": turn.model_dump(mode="json"),
            "context": self.runtime.inspect_checkpoint_context(turn.checkpoint_id),
        }

    def _context_owner_id(self, timeline_id: str) -> str:
        return f"main:{self.session_id}:{timeline_id}"

    def _export_trace(self, recorder: TraceRecorder) -> None:
        span_id = recorder.start_span("langsmith.export", kind="export")
        queued = self.trace_exporter.submit(recorder.trace_id) is not None
        recorder.end_span(
            span_id,
            attributes={"queued": queued, "enabled": self.trace_exporter.enabled},
        )

    def _timeline_ref(self, timeline_id: str) -> str:
        return f"refs/coding-agent/timelines/{self.session_id}/{timeline_id}"
