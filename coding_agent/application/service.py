"""Single-workspace application host shared by Web adapters."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import sqlite3
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent.application.approvals import ApprovalBroker
from coding_agent.application.events import EventJournal
from coding_agent.config import (
    AgentConfig,
    load_config,
    load_recent_workspaces,
    save_non_secret_config,
    save_recent_workspaces,
)
from coding_agent.coordinator import TurnCancelled, TurnCoordinator
from coding_agent.errors import CodingAgentError, fail
from coding_agent.models import SessionRecord
from coding_agent.repository import SqliteCheckpointRepository
from coding_agent.runtime import AgentRuntime, approve_by_default
from coding_agent.subagents import SubagentDemoManager
from coding_agent.tracing import MetricsLangSmithExporter, TraceStore
from coding_agent.workspace import GitSnapshotStore, resolve_workspace
from coding_agent.workspace.lock import RepositoryLock
from coding_agent.workspace.mutation import WorkspaceMutationGate
from coding_agent.workspace.paths import PathGuard


@dataclass
class _SessionRunner:
    runtime: AgentRuntime
    coordinator: TurnCoordinator
    operation_lock: threading.Lock


class CodingAgentHost:
    """Own one repository and serialize all mutation operations."""

    def __init__(
        self,
        *,
        workspace_path: Path,
        model: str | None,
        base_url: str | None,
        config_path: Path | None,
        session_id: str | None,
        langsmith_enabled: bool | None,
    ) -> None:
        self.workspace_path = workspace_path
        self.model = model
        self.base_url = base_url
        self.config_path = config_path
        self.requested_session_id = session_id
        self.langsmith_enabled = langsmith_enabled
        self._executor_workers = 1
        self.executor = ThreadPoolExecutor(
            max_workers=self._executor_workers,
            thread_name_prefix="coding-agent",
        )
        self._operation_lock = threading.Lock()
        self._future: asyncio.Future[Any] | None = None
        self._operation_tokens: dict[str, threading.Event] = {}
        self._operation_futures: dict[str, asyncio.Future[Any]] = {}
        self._runners: dict[str, _SessionRunner] = {}
        self._runners_lock = threading.RLock()
        self._event_loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._event_loop = loop
        bootstrap_executor = self.executor
        try:
            await loop.run_in_executor(bootstrap_executor, self._initialize)
        except BaseException:
            bootstrap_executor.shutdown(wait=False, cancel_futures=True)
            raise
        self._replace_operation_executor(self.config.max_parallel_sessions)
        bootstrap_executor.shutdown(wait=True)
        for session in self.repository.sessions(self.workspace.root):
            self._dispatch_pending_wake(session.session_id)

    def _initialize(self) -> None:
        self.workspace = resolve_workspace(self.workspace_path)
        self.repository_lock = RepositoryLock(self.workspace.data_dir / "locks" / "repository.lock")
        self.repository_lock.__enter__()
        self.repository = SqliteCheckpointRepository(self.workspace.data_dir / "agent.db")
        stored: SessionRecord | None
        if self.requested_session_id:
            stored = self.repository.validate_workspace(self.requested_session_id, self.workspace)
        else:
            stored = self.repository.latest_session(self.workspace.root)
        self.config: AgentConfig = load_config(
            model=self.model or (stored.model if stored else None),
            base_url=self.base_url,
            config_path=self.config_path,
            langsmith_enabled=self.langsmith_enabled,
        )
        session = stored or self.repository.create_session(self.workspace, self.config.model)
        self.trace_store = TraceStore(
            self.workspace.data_dir / "agent.db",
            self.workspace.data_dir / "artifacts",
        )
        self.trace_exporter = MetricsLangSmithExporter(
            self.trace_store,
            enabled=self.config.langsmith_enabled,
            project=self.config.langsmith_project,
        )
        self.trace_exporter.retry_pending()
        self.subagents = SubagentDemoManager(
            self.workspace,
            self.config,
            on_wake=self._on_subagent_wake,
            trace_store=self.trace_store,
            trace_exporter=self.trace_exporter,
        )
        self.snapshots = GitSnapshotStore(self.workspace)
        self.journal = EventJournal(self.workspace.data_dir / "agent.db")
        self.approvals = ApprovalBroker(self.journal)
        self.mutation_gate = WorkspaceMutationGate()
        self.session_id = session.session_id
        self._set_coordinator(session.session_id)
        self.coordinator.ensure_baseline()

    def _set_coordinator(self, session_id: str) -> None:
        runner = self._runner(session_id)
        with self._runners_lock:
            self.session_id = session_id
            self.runtime = runner.runtime
            self.coordinator = runner.coordinator

    def _runner(self, session_id: str) -> _SessionRunner:
        with self._runners_lock:
            existing = self._runners.get(session_id)
            if existing is not None:
                return existing
            self.repository.validate_workspace(session_id, self.workspace)
            runtime = AgentRuntime(
                config=self.config,
                workspace_root=self.workspace.root,
                repo_root=self.workspace.repo_root,
                checkpoint_path=self.workspace.data_dir / "checkpoints.db",
                extra_tools=self.subagents.build_control_tools(session_id),
                mutation_gate=self.mutation_gate,
            )
            runner = _SessionRunner(
                runtime=runtime,
                coordinator=TurnCoordinator(
                    session_id=session_id,
                    repository=self.repository,
                    runtime=runtime,
                    snapshots=self.snapshots,
                    trace_store=self.trace_store,
                    trace_exporter=self.trace_exporter,
                    model_id=self.config.model,
                    trace_secrets=(self.config.api_key or "",),
                    mutation_gate=self.mutation_gate,
                ),
                operation_lock=threading.Lock(),
            )
            self._runners[session_id] = runner
            return runner

    async def close(self) -> None:
        self._event_loop = None
        active_futures = [
            future for future in self._operation_futures.values() if not future.done()
        ]
        if self._future is not None and not self._future.done():
            active_futures.append(self._future)
        if active_futures:
            await asyncio.gather(
                *(asyncio.shield(future) for future in active_futures),
                return_exceptions=True,
            )
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self.executor, self._close)
        self.executor.shutdown(wait=True)

    def _close(self) -> None:
        self.subagents.close()
        for runner in self._runners.values():
            runner.runtime.close()
        self._runners.clear()
        self.trace_exporter.close()
        self.trace_store.close()
        self.journal.close()
        self.repository.close()
        self.repository_lock.__exit__(None, None, None)

    def status(self) -> dict[str, Any]:
        timeline = self._read_one(
            """
            SELECT t.* FROM timelines t JOIN sessions s
            ON s.active_timeline_id = t.timeline_id WHERE s.session_id = ?
            """,
            (self.session_id,),
        )
        active_operations = self.journal.active_operations()
        selected_operation = next(
            (
                operation
                for operation in reversed(active_operations)
                if operation["session_id"] == self.session_id
            ),
            None,
        )
        return {
            "service": "ready",
            "workspace": str(self.workspace.root),
            "workspace_name": self.workspace.root.name,
            "repo_root": str(self.workspace.repo_root),
            "branch": self._git_text("branch", "--show-current") or "detached",
            "model": self.config.model,
            "session_id": self.session_id,
            "timeline_id": timeline["timeline_id"],
            "active_operation": selected_operation,
            "active_operations": active_operations,
            "active_subagent_demo": self.subagents.repository.active_run(self.session_id),
            "pending_agent_wakes": self.subagents.repository.pending_wakes(self.session_id),
            "pending_approvals": self.journal.pending_approvals(),
        }

    def start_subagent_demo(self, session_id: str) -> dict[str, Any]:
        self.repository.validate_workspace(session_id, self.workspace)
        return self.subagents.start_demo(session_id)

    def latest_subagent_demo(self, session_id: str) -> dict[str, Any] | None:
        self.repository.validate_workspace(session_id, self.workspace)
        return self.subagents.latest(session_id)

    def subagent_runs(self, session_id: str) -> list[dict[str, Any]]:
        self.repository.validate_workspace(session_id, self.workspace)
        return self.subagents.repository.runs(session_id)

    def subagent_demo(self, run_id: str) -> dict[str, Any]:
        return self.subagents.inspect(run_id)

    def cancel_subagent_task(self, task_id: str) -> dict[str, Any]:
        return self.subagents.cancel_task(task_id)

    def settings(self) -> dict[str, Any]:
        """Return browser-editable settings without exposing credentials."""
        return {
            "model": self.config.model,
            "base_url": self.config.base_url,
            "langsmith_enabled": self.config.langsmith_enabled,
            "langsmith_project": self.config.langsmith_project,
            "model_timeout_seconds": self.config.model_timeout_seconds,
            "command_timeout_seconds": self.config.command_timeout_seconds,
            "max_parallel_sessions": self.config.max_parallel_sessions,
            "sandbox_enabled": self.config.sandbox_enabled,
            "sandbox_provider": self.config.sandbox_provider,
            "sandbox_image": self.config.sandbox_image,
            "sandbox_health": self.runtime.sandbox_health.model_dump(mode="json"),
            "model_api_key_configured": bool(
                os.getenv("CODING_AGENT_API_KEY") or os.getenv("OPENAI_API_KEY")
            ),
            "langsmith_api_key_configured": bool(
                os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")
            ),
        }

    async def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        """Validate, persist, and atomically replace runtime configuration."""
        self._ensure_idle()
        try:
            loop = asyncio.get_running_loop()
            operation_executor = self.executor
            await loop.run_in_executor(operation_executor, self._update_settings, values)
            if self.config.max_parallel_sessions != self._executor_workers:
                self._replace_operation_executor(self.config.max_parallel_sessions)
                operation_executor.shutdown(wait=True)
            return self.settings()
        finally:
            self._operation_lock.release()

    def _update_settings(self, values: dict[str, Any]) -> None:
        candidate = AgentConfig.model_validate(
            {
                **self.config.model_dump(),
                **values,
                "api_key": self.config.api_key,
            }
        )
        new_runtime = AgentRuntime(
            config=candidate,
            workspace_root=self.workspace.root,
            repo_root=self.workspace.repo_root,
            checkpoint_path=self.workspace.data_dir / "checkpoints.db",
            extra_tools=self.subagents.build_control_tools(self.session_id),
            mutation_gate=self.mutation_gate,
        )
        new_exporter = MetricsLangSmithExporter(
            self.trace_store,
            enabled=candidate.langsmith_enabled,
            project=candidate.langsmith_project,
        )
        try:
            save_non_secret_config(candidate, self.config_path)
            with self.repository.transaction() as connection:
                connection.execute(
                    "UPDATE sessions SET model = ? WHERE session_id = ?",
                    (candidate.model, self.session_id),
                )
        except BaseException:
            new_exporter.close()
            new_runtime.close()
            raise

        old_runners = list(self._runners.values())
        old_exporter = self.trace_exporter
        self.config = candidate
        self.subagents.config = candidate
        self.trace_exporter = new_exporter
        runner = _SessionRunner(
            runtime=new_runtime,
            coordinator=TurnCoordinator(
                session_id=self.session_id,
                repository=self.repository,
                runtime=new_runtime,
                snapshots=self.snapshots,
                trace_store=self.trace_store,
                trace_exporter=new_exporter,
                model_id=candidate.model,
                trace_secrets=(candidate.api_key or "",),
                mutation_gate=self.mutation_gate,
            ),
            operation_lock=threading.Lock(),
        )
        with self._runners_lock:
            self._runners = {self.session_id: runner}
            self.runtime = runner.runtime
            self.coordinator = runner.coordinator
        for old_runner in old_runners:
            old_runner.runtime.close()
        old_exporter.close()

    def sessions(self, *, limit: int = 30, offset: int = 0) -> dict[str, Any]:
        rows = self.repository.session_summaries(
            self.workspace.root,
            active_session_id=self.session_id,
            limit=limit + 1,
            offset=offset,
        )
        has_more = len(rows) > limit
        items = rows[:limit]
        result = {
            "items": items,
            "next_offset": offset + limit if has_more else None,
        }
        return result

    async def create_session(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._create_session)

    def _create_session(self) -> dict[str, Any]:
        session = self.repository.create_session(self.workspace, self.config.model)
        with self._runners_lock:
            self.session_id = session.session_id
        return session.model_dump(mode="json")

    async def select_session(self, session_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._select_session, session_id)

    def _select_session(self, session_id: str) -> dict[str, Any]:
        session = self.repository.validate_workspace(session_id, self.workspace)
        self._set_coordinator(session_id)
        return session.model_dump(mode="json")

    def turns(
        self,
        session_id: str,
        *,
        limit: int = 30,
        before_turn_number: int | None = None,
    ) -> dict[str, Any]:
        session = self._read_one("SELECT * FROM sessions WHERE session_id = ?", (session_id,))
        timeline_id = str(session["active_timeline_id"])
        turns, next_before = self.repository.history_turn_page(
            timeline_id,
            limit=limit,
            before_turn_number=before_turn_number,
        )
        result = {
            "session_id": session_id,
            "timeline_id": timeline_id,
            "turns": [turn.model_dump(mode="json") for turn in turns],
            "next_before_turn_number": next_before,
        }
        return result

    def submit_turn(
        self,
        *,
        session_id: str,
        message: str,
        client_request_id: str,
        expected_timeline_id: str,
    ) -> dict[str, Any]:
        runner = self._acquire_session_operation(session_id)
        try:
            timeline = self._read_one(
                "SELECT active_timeline_id FROM sessions WHERE session_id = ?", (session_id,)
            )
            if timeline["active_timeline_id"] != expected_timeline_id:
                raise fail("TIMELINE_CHANGED", "The session was restored in another client.")
            operation_id, created = self.journal.create_operation(
                session_id=session_id,
                timeline_id=expected_timeline_id,
                kind="turn",
                client_request_id=client_request_id,
            )
            if created:
                cancelled = threading.Event()
                self._operation_tokens[operation_id] = cancelled
                self._schedule_operation(
                    operation_id,
                    self._run_turn,
                    operation_id,
                    session_id,
                    runner,
                    message,
                    cancelled,
                )
            else:
                runner.operation_lock.release()
            operation = self.journal.operation(operation_id)
            if operation is None:
                raise fail("OPERATION_NOT_FOUND", "Operation could not be loaded.")
            return {
                "operation_id": operation_id,
                "status": "queued" if created else operation["status"],
                "events_url": f"/api/v1/operations/{operation_id}/events",
            }
        except BaseException:
            if runner.operation_lock.locked():
                runner.operation_lock.release()
            raise

    def _run_turn(
        self,
        operation_id: str,
        session_id: str,
        runner: _SessionRunner,
        message: str,
        cancelled: threading.Event,
        wake_id: str | None = None,
    ) -> None:
        completed = False
        try:
            if cancelled.is_set():
                raise fail("OPERATION_CANCELLED", "The Agent operation was cancelled.")
            if not self.journal.set_status(operation_id, "running"):
                raise fail("OPERATION_CANCELLED", "The Agent operation was cancelled.")
            self.journal.append(operation_id, "turn.started", {"session_id": session_id})
            self.journal.append(
                operation_id,
                "user.message",
                {
                    "text": message,
                    "origin": "subagent_scheduler" if wake_id else "user",
                },
            )
            timeline = self.repository.active_timeline(session_id)
            if not self.repository.turns(timeline.timeline_id):
                owner = f"session-bootstrap:{session_id}"
                self.mutation_gate.acquire(owner, cancelled)
                try:
                    runner.coordinator.ensure_baseline()
                finally:
                    self.mutation_gate.release(owner)
            response = runner.coordinator.run_turn(
                message,
                lambda request: self._approve_turn_request(
                    operation_id,
                    request,
                    cancelled,
                ),
                lambda text: self._emit_token(operation_id, text),
                lambda event_type, payload: self._emit_tool_event(
                    operation_id,
                    event_type,
                    payload,
                ),
                cancelled,
                lambda: self.journal.set_status(
                    operation_id,
                    "committing",
                    expected=("running",),
                ),
            )
            if cancelled.is_set():
                raise fail("OPERATION_CANCELLED", "The Agent operation was cancelled.")
            self.journal.append(operation_id, "assistant.completed", {"text": response})
            self.journal.emit_context_usage(
                operation_id,
                runner.runtime.accountant.snapshot(),
            )
            timeline = self.repository.active_timeline(session_id)
            turn = self.repository.turns(timeline.timeline_id)[-1]
            self.journal.append(
                operation_id,
                "turn.committed",
                {
                    "turn_id": turn.turn_id,
                    "turn_number": turn.turn_number,
                    "timeline_id": timeline.timeline_id,
                    "snapshot_oid": turn.snapshot_oid,
                },
            )
            self.journal.append(operation_id, "workspace.changed", self.workspace_status())
            self.journal.append(
                operation_id,
                "operation.completed",
                {"kind": "turn", "timeline_id": timeline.timeline_id},
            )
            completed = self.journal.set_status(
                operation_id,
                "completed",
                expected=("committing",),
            )
        except TurnCancelled as tc:
            code = tc.code
            if wake_id is None:
                try:
                    self._record_cancelled_turn(
                        operation_id=operation_id,
                        session_id=session_id,
                        user_text=message,
                        thread_id=tc.execution_thread_id,
                        checkpoint_id=tc.checkpoint_id,
                    )
                except BaseException as persistence_error:
                    self.journal.append(
                        operation_id,
                        "turn.persistence_failed",
                        {"error": str(persistence_error)},
                    )
            self.journal.append(operation_id, "operation.cancelled", {})
            self.journal.set_status(operation_id, "cancelled", code)
        except BaseException as exc:
            code = exc.code if isinstance(exc, CodingAgentError) else "RUNTIME_ERROR"
            if code == "OPERATION_CANCELLED":
                if wake_id is None:
                    try:
                        self._record_cancelled_turn(
                            operation_id=operation_id,
                            session_id=session_id,
                            user_text=message,
                        )
                    except BaseException as persistence_error:
                        self.journal.append(
                            operation_id,
                            "turn.persistence_failed",
                            {"error": str(persistence_error)},
                        )
                self.journal.append(operation_id, "operation.cancelled", {})
                self.journal.set_status(operation_id, "cancelled", code)
            else:
                self.journal.append(
                    operation_id,
                    "operation.failed",
                    {"error": {"code": code, "message": str(exc)}},
                )
                self.journal.set_status(operation_id, "failed", code)
        finally:
            self._operation_tokens.pop(operation_id, None)
            if wake_id is not None:
                if completed:
                    self.subagents.repository.complete_wake(wake_id)
                else:
                    self.subagents.repository.release_wake(wake_id)
            runner.operation_lock.release()
            if self._event_loop is not None and (wake_id is None or completed):
                self._event_loop.call_soon_threadsafe(
                    self._dispatch_pending_wake,
                    session_id,
                )

    def _record_cancelled_turn(
        self,
        *,
        operation_id: str,
        session_id: str,
        user_text: str,
        thread_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> None:
        """Persist a cancelled interaction, promoting its thread so the context survives."""
        timeline = self.repository.active_timeline(session_id)
        parent = self.repository.turns(timeline.timeline_id)[-1]
        events = self.journal.events_after(operation_id, 0)
        partial_response = "".join(
            str(event["payload"].get("text", ""))
            for event in events
            if event["event_type"] == "assistant.delta"
        )
        tool_count = len(
            {
                str(event["payload"].get("run_id"))
                for event in events
                if event["event_type"] == "tool.started"
                and event["payload"].get("run_id")
            }
        )
        if tool_count:
            suffix = f"已取消，本轮终止了 {tool_count} 个后台工具。"
            partial_response = (
                f"{partial_response.rstrip()}\n\n{suffix}"
                if partial_response.strip()
                else suffix
            )
        elif not partial_response.strip():
            partial_response = "已取消，本轮未产生完整回复。"
        self.repository.add_turn(
            timeline_id=timeline.timeline_id,
            checkpoint_id=checkpoint_id or parent.checkpoint_id,
            thread_id=thread_id,
            snapshot_oid=parent.snapshot_oid,
            user_text=user_text,
            assistant_text=partial_response,
            status="cancelled",
        )

    def _emit_token(self, operation_id: str, text: str) -> None:
        self.journal.append(operation_id, "assistant.delta", {"text": text})

    def _emit_tool_event(
        self,
        operation_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        self.journal.append(operation_id, event_type, payload)

    def _approve_turn_request(
        self,
        operation_id: str,
        request: dict[str, Any],
        cancelled: threading.Event | None = None,
    ) -> bool:
        if request.get("name") == "activate_skill":
            if cancelled is None:
                return self.approvals.request(operation_id, request)
            return self.approvals.request(operation_id, request, cancelled)
        return approve_by_default(request)

    def submit_restore(
        self, *, session_id: str, turn_number: int, expected_timeline_id: str
    ) -> dict[str, Any]:
        self._ensure_idle()
        try:
            if session_id != self.session_id:
                raise fail("SESSION_NOT_ACTIVE", "Select this session before restoring it.")
            current = self._read_one(
                "SELECT active_timeline_id FROM sessions WHERE session_id = ?", (session_id,)
            )
            if current["active_timeline_id"] != expected_timeline_id:
                raise fail("TIMELINE_CHANGED", "The session timeline already changed.")
            operation_id, _ = self.journal.create_operation(
                session_id=session_id,
                timeline_id=expected_timeline_id,
                kind="restore",
                client_request_id=None,
            )
            runner = self._runner(session_id)
            self._schedule_operation(
                operation_id,
                self._run_restore,
                operation_id,
                session_id,
                runner,
                turn_number,
            )
            return {
                "operation_id": operation_id,
                "status": "queued",
                "events_url": f"/api/v1/operations/{operation_id}/events",
            }
        except BaseException:
            if self._operation_lock.locked():
                self._operation_lock.release()
            raise

    def _run_restore(
        self,
        operation_id: str,
        session_id: str,
        runner: _SessionRunner,
        turn_number: int,
    ) -> None:
        try:
            self.journal.set_status(operation_id, "running")
            self.journal.append(
                operation_id, "step.started", {"kind": "restore", "turn_number": turn_number}
            )
            runner.coordinator.restore(turn_number)
            timeline = self.repository.active_timeline(session_id)
            self.journal.append(
                operation_id,
                "step.completed",
                {"kind": "restore", "timeline_id": timeline.timeline_id},
            )
            self.journal.append(operation_id, "workspace.changed", self.workspace_status())
            self.journal.append(
                operation_id,
                "operation.completed",
                {"kind": "restore", "timeline_id": timeline.timeline_id},
            )
            self.journal.set_status(
                operation_id,
                "completed",
                expected=("running",),
            )
        except BaseException as exc:
            code = exc.code if isinstance(exc, CodingAgentError) else "RUNTIME_ERROR"
            self.journal.append(
                operation_id,
                "operation.failed",
                {"error": {"code": code, "message": str(exc)}},
            )
            self.journal.set_status(operation_id, "failed", code)
        finally:
            self._operation_lock.release()

    def _ensure_idle(self) -> None:
        if not self._operation_lock.acquire(blocking=False):
            raise fail("WORKSPACE_BUSY", "Another workspace operation is running.")
        with self._runners_lock:
            busy = any(runner.operation_lock.locked() for runner in self._runners.values())
        if busy:
            self._operation_lock.release()
            raise fail("WORKSPACE_BUSY", "Another workspace operation is running.")

    def _acquire_session_operation(self, session_id: str) -> _SessionRunner:
        if not self._operation_lock.acquire(blocking=False):
            raise fail("WORKSPACE_BUSY", "An exclusive workspace operation is running.")
        try:
            runner = self._runner(session_id)
            if not runner.operation_lock.acquire(blocking=False):
                raise fail("SESSION_BUSY", "Another operation is already running in this session.")
            return runner
        finally:
            self._operation_lock.release()

    def _schedule(self, function: Any, *args: Any) -> None:
        loop = asyncio.get_running_loop()
        self._future = asyncio.ensure_future(loop.run_in_executor(self.executor, function, *args))

    def _replace_operation_executor(self, max_workers: int) -> None:
        self._executor_workers = max_workers
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="coding-agent",
        )

    def _schedule_operation(
        self,
        operation_id: str,
        function: Any,
        *args: Any,
    ) -> None:
        loop = asyncio.get_running_loop()
        future = asyncio.ensure_future(loop.run_in_executor(self.executor, function, *args))
        self._operation_futures[operation_id] = future

        def remove_completed(_future: asyncio.Future[Any]) -> None:
            self._operation_futures.pop(operation_id, None)

        future.add_done_callback(remove_completed)

    def _on_subagent_wake(self, wake: dict[str, Any]) -> None:
        loop = self._event_loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(
                self._dispatch_pending_wake,
                str(wake["parent_session_id"]),
            )

    def _dispatch_pending_wake(self, session_id: str) -> None:
        if not self.subagents.repository.pending_wakes(session_id):
            return
        try:
            runner = self._acquire_session_operation(session_id)
        except CodingAgentError:
            return
        try:
            wake = self.subagents.repository.claim_next_wake(session_id)
            if wake is None:
                runner.operation_lock.release()
                return
            timeline = self.repository.active_timeline(session_id)
            operation_id, created = self.journal.create_operation(
                session_id=session_id,
                timeline_id=timeline.timeline_id,
                kind="subagent_wake",
                client_request_id=f"wake:{wake['wake_id']}:{wake['claimed_at']}",
            )
            if not created:
                self.subagents.repository.complete_wake(str(wake["wake_id"]))
                runner.operation_lock.release()
                return
            cancelled = threading.Event()
            self._operation_tokens[operation_id] = cancelled
            message = json.dumps(
                {
                    "origin": "subagent_scheduler",
                    "wake_id": wake["wake_id"],
                    "task_id": wake["task_id"],
                    "attempt_id": wake["attempt_id"],
                    "type": wake["wake_type"],
                    "payload": wake["payload"],
                    "instruction": (
                        "Inspect the child task now and decide whether to accept, revise, "
                        "answer, grant capabilities, or cancel it."
                    ),
                },
                ensure_ascii=False,
            )
            self._schedule_operation(
                operation_id,
                self._run_turn,
                operation_id,
                session_id,
                runner,
                message,
                cancelled,
                str(wake["wake_id"]),
            )
        except BaseException:
            if runner.operation_lock.locked():
                runner.operation_lock.release()
            raise

    def resolve_approval(self, approval_id: str, body: dict[str, str]) -> dict[str, Any]:
        return self.approvals.resolve(approval_id, **body)

    def cancel_operation(self, operation_id: str) -> dict[str, Any]:
        operation = self.journal.operation(operation_id)
        if operation is None:
            raise fail("OPERATION_NOT_FOUND", "Operation does not exist.")
        if operation["status"] in {"completed", "failed", "cancelled"}:
            return {"operation_id": operation_id, "status": operation["status"]}
        if not self.journal.request_cancel(operation_id):
            current = self.journal.operation(operation_id)
            return {
                "operation_id": operation_id,
                "status": current["status"] if current is not None else "cancel_requested",
            }
        self.journal.append(operation_id, "operation.cancellation_requested", {})
        token = self._operation_tokens.get(operation_id)
        if token is not None:
            token.set()
        with self._runners_lock:
            runner = self._runners.get(str(operation["session_id"]))
        if runner is not None:
            runner.runtime.cancel_active_tools(reason="operation cancelled by user")
        self.approvals.cancel_operation(operation_id)
        return {"operation_id": operation_id, "status": "cancel_requested"}

    def workspace_status(self) -> dict[str, Any]:
        output = self._git_text(
            "status", "--short", "--untracked-files=all", "--", self._workspace_prefix()
        )
        files = [line for line in output.splitlines() if line]
        added = deleted = 0
        stats = self._git_text("diff", "--numstat", "--", self._workspace_prefix())
        for line in stats.splitlines():
            fields = line.split("\t", 2)
            if len(fields) == 3:
                added += int(fields[0]) if fields[0].isdigit() else 0
                deleted += int(fields[1]) if fields[1].isdigit() else 0
        result = {
            "branch": self._git_text("branch", "--show-current") or "detached",
            "changed_file_count": len(files),
            "added_lines": added,
            "deleted_lines": deleted,
            "clean": not files,
        }
        return result

    def diff(self, path: str | None = None) -> dict[str, Any]:
        status = self._git_text(
            "status", "--short", "--untracked-files=all", "--", self._workspace_prefix()
        )
        stats: dict[str, tuple[int, int]] = {}
        if not path:
            numstat = self._git_text(
                "diff",
                "--numstat",
                "--",
                self._workspace_prefix(),
            )
            for value in numstat.splitlines():
                fields = value.split("\t", 2)
                if len(fields) != 3:
                    continue
                added, deleted, repo_path = fields
                relative = self._to_workspace_path(repo_path)
                if relative is not None:
                    stats[relative] = (
                        int(added) if added.isdigit() else 0,
                        int(deleted) if deleted.isdigit() else 0,
                    )
        files = []
        for line in status.splitlines():
            if len(line) < 4:
                continue
            repo_path = line[3:].split(" -> ")[-1]
            relative = self._to_workspace_path(repo_path)
            if relative is None or (path and relative != path):
                continue
            patch = self._file_diff(relative, line[:2]) if path else ""
            additions, deletions = stats.get(relative, (0, 0))
            if path:
                additions = sum(
                    1
                    for value in patch.splitlines()
                    if value.startswith("+") and not value.startswith("+++")
                )
                deletions = sum(
                    1
                    for value in patch.splitlines()
                    if value.startswith("-") and not value.startswith("---")
                )
            files.append(
                {
                    "path": relative,
                    "status": line[:2].strip() or "M",
                    "additions": additions,
                    "deletions": deletions,
                    "patch": patch[:1_048_576],
                    "truncated": len(patch) > 1_048_576,
                }
            )
        version = self._working_version(path)
        return {"files": files, "snapshot_version": version}

    def files(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in sorted(self.workspace.root.rglob("*")):
            relative = item.relative_to(self.workspace.root)
            ignored = {".git", ".venv", "node_modules", "__pycache__"}
            if any(part in ignored for part in relative.parts):
                continue
            if item.is_symlink() or not item.is_file():
                continue
            try:
                PathGuard(self.workspace.root).resolve_for_read(relative.as_posix())
            except CodingAgentError:
                continue
            result.append({"path": relative.as_posix(), "size": item.stat().st_size})
            if len(result) >= 2000:
                break
        return result

    def file_content(self, path: str) -> dict[str, Any]:
        resolved = PathGuard(self.workspace.root).resolve_for_read(path)
        if not resolved.is_file():
            raise fail("FILE_NOT_FOUND", "The requested path is not a file.")
        raw = resolved.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        binary = b"\0" in raw[:8192]
        limit = self.config.max_read_bytes
        content = "" if binary else raw[:limit].decode("utf-8", errors="replace")
        return {
            "path": path,
            "sha256": digest,
            "byte_size": len(raw),
            "media_type": mimetypes.guess_type(path)[0] or "text/plain",
            "binary": binary,
            "truncated": len(raw) > limit,
            "content": content,
        }

    def trace(self, turn_id: str) -> dict[str, Any]:
        summary = self.trace_store.trace_for_turn_id(turn_id)
        if summary is None:
            raise fail("TRACE_NOT_FOUND", "No trace exists for this turn.")
        return summary

    def artifact(self, artifact_id: str) -> tuple[Path, str]:
        row = self.trace_store.query_one(
            "SELECT relative_path, media_type FROM artifacts WHERE artifact_id = ?",
            (artifact_id,),
        )
        if row is None:
            raise fail("ARTIFACT_NOT_FOUND", "Unknown artifact.")
        return self.trace_store.artifacts.path(str(row["relative_path"])), str(row["media_type"])

    def _read_all(self, sql: str, parameters: tuple[Any, ...]) -> list[dict[str, Any]]:
        with sqlite3.connect(self.workspace.data_dir / "agent.db") as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, parameters).fetchall()]

    def _read_one(self, sql: str, parameters: tuple[Any, ...]) -> dict[str, Any]:
        row = self._read_one_optional(sql, parameters)
        if row is None:
            raise fail("NOT_FOUND", "Requested resource does not exist.")
        return row

    def _read_one_optional(self, sql: str, parameters: tuple[Any, ...]) -> dict[str, Any] | None:
        rows = self._read_all(sql, parameters)
        return rows[0] if rows else None

    def _git_text(self, *args: str) -> str:
        process = subprocess.run(
            ["git", "-C", str(self.workspace.repo_root), *args],
            capture_output=True,
            check=False,
        )
        return process.stdout.decode(errors="replace").rstrip("\r\n")

    def _workspace_prefix(self) -> str:
        return self.workspace.root.relative_to(self.workspace.repo_root).as_posix()

    def _to_workspace_path(self, repo_path: str) -> str | None:
        prefix = self._workspace_prefix()
        if prefix == ".":
            return repo_path
        marker = f"{prefix}/"
        return repo_path[len(marker) :] if repo_path.startswith(marker) else None

    def _file_diff(self, path: str, status: str) -> str:
        repo_path = (self.workspace.root / path).relative_to(self.workspace.repo_root).as_posix()
        if "?" in status:
            process = subprocess.run(
                ["git", "diff", "--no-index", "--", "/dev/null", str(self.workspace.root / path)],
                capture_output=True,
                check=False,
            )
            return process.stdout.decode(errors="replace")
        return self._git_text("diff", "--no-ext-diff", "--", repo_path)

    def _working_version(self, path: str | None = None) -> str:
        workspace_prefix = self._workspace_prefix()
        target = (
            (self.workspace.root / path).relative_to(self.workspace.repo_root).as_posix()
            if path
            else workspace_prefix
        )
        value = self._git_text(
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--",
            target,
        )
        return hashlib.sha256(value.encode()).hexdigest()


class WorkspaceManager:
    """Switch one active workspace Host while retaining a recent-directory registry."""

    _DELEGATED = {
        "artifact",
        "cancel_subagent_task",
        "cancel_operation",
        "create_session",
        "diff",
        "file_content",
        "files",
        "journal",
        "resolve_approval",
        "select_session",
        "sessions",
        "start_subagent_demo",
        "latest_subagent_demo",
        "subagent_runs",
        "subagent_demo",
        "submit_restore",
        "submit_turn",
        "trace",
        "turns",
        "workspace_status",
    }

    def __init__(
        self,
        *,
        workspace_path: Path | None,
        model: str | None,
        base_url: str | None,
        config_path: Path | None,
        session_id: str | None,
        langsmith_enabled: bool | None,
    ) -> None:
        self.workspace_path = workspace_path
        self.model = model
        self.base_url = base_url
        self.config_path = config_path
        self.session_id = session_id
        self.langsmith_enabled = langsmith_enabled
        self.active: CodingAgentHost | None = None
        self._switch_lock = asyncio.Lock()

    async def start(self) -> None:
        self.config = load_config(
            model=self.model,
            base_url=self.base_url,
            config_path=self.config_path,
            langsmith_enabled=self.langsmith_enabled,
        )
        self.recent = load_recent_workspaces(self.config_path)
        if self.workspace_path is not None:
            await self.open_workspace(str(self.workspace_path), session_id=self.session_id)

    async def close(self) -> None:
        if self.active is not None:
            await self.active.close()
            self.active = None

    def status(self) -> dict[str, Any]:
        if self.active is not None:
            return self.active.status()
        return {
            "service": "ready",
            "workspace": None,
            "workspace_name": None,
            "repo_root": None,
            "branch": None,
            "model": self.config.model,
            "session_id": None,
            "timeline_id": None,
            "active_operation": None,
            "active_operations": [],
            "active_subagent_demo": None,
            "pending_agent_wakes": [],
            "pending_approvals": [],
        }

    def workspaces(self) -> dict[str, Any]:
        active_path = str(self.active.workspace.root) if self.active is not None else None
        paths = list(self.recent)
        if active_path and active_path not in paths:
            paths.insert(0, active_path)
        return {
            "active": active_path,
            "recent": [
                {
                    "path": path,
                    "name": Path(path).name or path,
                    "active": path == active_path,
                    "available": Path(path).is_dir(),
                }
                for path in paths
            ],
        }

    async def open_workspace(
        self,
        path: str,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        async with self._switch_lock:
            if self.active is not None and self.active.journal.active_operation() is not None:
                raise fail("WORKSPACE_BUSY", "Wait for the active workspace operation to finish.")
            if (
                self.active is not None
                and self.active.subagents.repository.active_run(self.active.session_id) is not None
            ):
                raise fail("WORKSPACE_BUSY", "Wait for the subagent demo to finish.")
            requested = Path(path).expanduser()
            if self.active is not None:
                try:
                    if requested.resolve(strict=True) == self.active.workspace.root:
                        return self.active.status()
                except OSError:
                    pass
            candidate = CodingAgentHost(
                workspace_path=requested,
                model=self.config.model,
                base_url=self.config.base_url,
                config_path=self.config_path,
                session_id=session_id,
                langsmith_enabled=self.config.langsmith_enabled,
            )
            await candidate.start()
            previous = self.active
            path_value = str(candidate.workspace.root)
            next_recent = [path_value, *[item for item in self.recent if item != path_value]][:10]
            try:
                save_recent_workspaces(next_recent, self.config_path)
                if previous is not None:
                    await previous.close()
            except BaseException:
                await candidate.close()
                raise
            self.active = candidate
            self.config = candidate.config
            self.recent = next_recent
            result = candidate.status()
            return result

    def remove_recent_workspace(self, path: str) -> dict[str, Any]:
        active_path = str(self.active.workspace.root) if self.active is not None else None
        if path == active_path:
            raise fail("WORKSPACE_ACTIVE", "The active workspace cannot be removed from recents.")
        self.recent = [item for item in self.recent if item != path]
        save_recent_workspaces(self.recent, self.config_path)
        return self.workspaces()

    def settings(self) -> dict[str, Any]:
        if self.active is not None:
            return self.active.settings()
        return {
            "model": self.config.model,
            "base_url": self.config.base_url,
            "langsmith_enabled": self.config.langsmith_enabled,
            "langsmith_project": self.config.langsmith_project,
            "model_timeout_seconds": self.config.model_timeout_seconds,
            "command_timeout_seconds": self.config.command_timeout_seconds,
            "max_parallel_sessions": self.config.max_parallel_sessions,
            "sandbox_enabled": self.config.sandbox_enabled,
            "sandbox_provider": self.config.sandbox_provider,
            "sandbox_image": self.config.sandbox_image,
            "sandbox_health": None,
            "model_api_key_configured": bool(
                os.getenv("CODING_AGENT_API_KEY") or os.getenv("OPENAI_API_KEY")
            ),
            "langsmith_api_key_configured": bool(
                os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")
            ),
        }

    async def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        if self.active is not None:
            result = await self.active.update_settings(values)
            self.config = self.active.config
            return result
        self.config = AgentConfig.model_validate(
            {**self.config.model_dump(), **values, "api_key": self.config.api_key}
        )
        save_non_secret_config(self.config, self.config_path)
        return self.settings()

    def __getattr__(self, name: str) -> Any:
        if name not in self._DELEGATED:
            raise AttributeError(name)
        if self.active is None:
            raise fail("WORKSPACE_NOT_OPEN", "Open a Git workspace before using this action.")
        return getattr(self.active, name)
