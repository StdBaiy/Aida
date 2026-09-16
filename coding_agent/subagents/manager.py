"""Two-worker asynchronous subagent MVP and deterministic demo workflow."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from coding_agent.config import AgentConfig
from coding_agent.errors import CodingAgentError, fail
from coding_agent.models import Workspace, utc_now
from coding_agent.prompting import child_contract, phase_context
from coding_agent.repository import new_id
from coding_agent.subagents.repository import SubagentRepository
from coding_agent.subagents.results import build_result_envelope
from coding_agent.subagents.tools import AttemptToolRuntime
from coding_agent.subagents.workspace import SubagentWorkspaceManager
from coding_agent.tracing import MetricsLangSmithExporter, TraceRecorder, TraceStore
from coding_agent.tracing.context import active_recorder
from coding_agent.workspace.mutation import acquire_workspace_mutation

_ALLOWED_TOOLS = ("mock_sleep", "write_deliverable")
_CHILD_TOOLS = frozenset(
    {
        "list_files",
        "read_file",
        "glob_files",
        "search_code",
        "apply_patch",
        "workspace_status",
        "show_diff",
        "run_command",
        "inspect_tool_run",
        "wait_for_tools",
        "cancel_tool_run",
    }
)
_TERMINAL_TASKS = {"merged", "failed", "cancelled"}


class SubagentDemoManager:
    """Run at most two isolated subagents for one session."""

    def __init__(
        self,
        workspace: Workspace,
        config: AgentConfig | None = None,
        on_wake: Callable[[dict[str, Any]], None] | None = None,
        trace_store: TraceStore | None = None,
        trace_exporter: MetricsLangSmithExporter | None = None,
    ) -> None:
        self.workspace = workspace
        self.config = config
        self.repository = SubagentRepository(workspace.data_dir / "agent.db")
        self.workspaces = SubagentWorkspaceManager(workspace)
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="subagent")
        self._cancelled = threading.Event()
        self._futures: set[Future[None]] = set()
        self._task_futures: dict[str, Future[None]] = {}
        self._attempt_cancellations: dict[str, threading.Event] = {}
        self._lock = threading.RLock()
        self._session_id: str | None = None
        self._contracts: dict[str, dict[str, Any]] = {}
        self._on_wake = on_wake
        self._trace_store = trace_store
        self._trace_exporter = trace_exporter
        self._recover_integrations()
        self._restore_contracts()

    def _restore_contracts(self) -> None:
        """Rebuild enough task context to review or resume persisted child work."""
        for task in self.repository.resumable_tasks():
            grants = list(json.loads(str(task["allowed_tools_json"])))
            self._contracts[str(task["task_id"])] = self._normalize_contract(
                {
                    "name": task["name"],
                    "objective": task["objective"],
                    "scope": json.loads(str(task["scope_json"])),
                    "acceptance": json.loads(str(task["acceptance_json"])),
                    "allowed_tools": grants,
                    "workspace_mode": task["workspace_mode"],
                }
            )

    def _recover_integrations(self) -> None:
        """Reconcile filesystem writes left between apply and SQLite commit."""
        for task in self.repository.integrating_tasks():
            task_id = str(task["task_id"])
            run_id = str(task["run_id"])
            attempt_id = str(task["active_attempt_id"])
            base_commit = str(task["base_commit"])
            result_commit = task.get("result_commit")
            if not result_commit:
                self._fail_task(
                    run_id,
                    task_id,
                    fail(
                        "SUBAGENT_INTEGRATION_RECOVERY_FAILED",
                        "Interrupted integration has no result commit.",
                    ),
                )
                self._finish_run_if_ready(run_id)
                continue
            state = self.workspaces.patch_state(base_commit, str(result_commit))
            if state == "applied":
                if not self.repository.complete_integration(
                    task_id,
                    attempt_id,
                    str(result_commit),
                ):
                    continue
                self._event(
                    run_id,
                    task_id,
                    attempt_id,
                    "integration.completed",
                    {
                        "integration_commit": result_commit,
                        "target": "parent workspace",
                        "recovered": True,
                    },
                )
                self._finish_run_if_ready(run_id)
            elif state == "not_applied":
                if self.repository.reset_integration(task_id, attempt_id):
                    self._event(
                        run_id,
                        task_id,
                        attempt_id,
                        "result.committed",
                        {
                            "result_commit": result_commit,
                            "changed_paths": [],
                            "recovered": True,
                        },
                    )
            else:
                self._fail_task(
                    run_id,
                    task_id,
                    fail(
                        "SUBAGENT_INTEGRATION_RECOVERY_FAILED",
                        "Interrupted integration conflicts with the parent workspace.",
                    ),
                )
                self._finish_run_if_ready(run_id)

    def _task_for_session(
        self,
        task_id: str,
        expected_session_id: str | None,
    ) -> dict[str, Any]:
        try:
            task = self.repository.task(task_id)
        except KeyError as exc:
            raise fail("SUBAGENT_TASK_NOT_FOUND", "Unknown child task.") from exc
        if (
            expected_session_id is not None
            and str(task["session_id"]) != expected_session_id
        ):
            raise fail("SUBAGENT_TASK_NOT_FOUND", "Unknown child task.")
        return task

    def bind_session(self, session_id: str) -> None:
        """Bind parent-facing tools to the currently active session."""
        self._session_id = session_id

    def build_control_tools(self, session_id: str | None = None) -> list[BaseTool]:
        """Build the capability surface exposed only to the main Agent."""
        owner_session_id = session_id or self._session_id

        @tool
        def create_agent_tasks(
            tasks: list[dict[str, Any]],
        ) -> dict[str, Any]:
            """Start one or two children; worktrees are allocated according to workspace_mode."""
            try:
                return self.create_agent_tasks(tasks, session_id=session_id)
            except Exception as exc:
                return self._tool_error(exc)

        @tool
        def inspect_agent_task(task_id: str, after_event_id: int = 0) -> dict[str, Any]:
            """Inspect one child Agent and return incremental progress and result evidence."""
            try:
                task = self._task_for_session(task_id, owner_session_id)
                run = self.repository.run(str(task["run_id"]))
                item = next(value for value in run["tasks"] if value["task_id"] == task_id)
                item["events"] = [
                    event for event in item["events"] if event["event_id"] > after_event_id
                ]
                return {"ok": True, "run_id": run["run_id"], "task": item}
            except Exception as exc:
                return self._tool_error(exc)

        @tool
        def wait_agent_tasks(task_ids: list[str], timeout_seconds: int = 10) -> dict[str, Any]:
            """Wait until any child is reviewable or terminal, then return all statuses."""
            try:
                deadline = time.monotonic() + min(max(timeout_seconds, 1), 15)
                initial = [
                    self._task_for_session(task_id, owner_session_id)
                    for task_id in task_ids
                ]
                active_ids = {
                    str(task["task_id"])
                    for task in initial
                    if task["status"] in {"queued", "running", "cancelling"}
                }
                while time.monotonic() < deadline:
                    tasks = [
                        self._task_for_session(task_id, owner_session_id)
                        for task_id in task_ids
                    ]
                    if any(
                        str(task["task_id"]) in active_ids
                        and task["status"] not in {"queued", "running", "cancelling"}
                        for task in tasks
                    ):
                        break
                    if not active_ids:
                        break
                    time.sleep(0.25)
                return {
                    "ok": True,
                    "tasks": [
                        {
                            "task_id": task["task_id"],
                            "status": task["status"],
                            "active_attempt_id": task["active_attempt_id"],
                        }
                        for task in (
                            self._task_for_session(task_id, owner_session_id)
                            for task_id in task_ids
                        )
                    ],
                }
            except Exception as exc:
                return self._tool_error(exc)

        @tool
        def request_agent_revision(task_id: str, feedback: str) -> dict[str, Any]:
            """Start a new Attempt from a completed child result with review feedback."""
            try:
                return self.request_revision(
                    task_id,
                    feedback,
                    expected_session_id=owner_session_id,
                )
            except Exception as exc:
                return self._tool_error(exc)

        @tool
        def accept_agent_result(task_id: str) -> dict[str, Any]:
            """Accept a completed child result and apply it to the parent workspace."""
            try:
                acquire_workspace_mutation()
                return self.accept_result(task_id, expected_session_id=owner_session_id)
            except Exception as exc:
                return self._tool_error(exc)

        @tool
        def cancel_agent_task(task_id: str) -> dict[str, Any]:
            """Cancel one queued, running, or review-pending child Agent task."""
            try:
                return self.cancel_task(task_id, expected_session_id=owner_session_id)
            except Exception as exc:
                return self._tool_error(exc)

        @tool
        def respond_agent_request(task_id: str, response: str) -> dict[str, Any]:
            """Resume a child Agent that is waiting for information from its parent."""
            try:
                return self.respond_to_request(
                    task_id,
                    response,
                    expected_session_id=owner_session_id,
                )
            except Exception as exc:
                return self._tool_error(exc)

        @tool
        def grant_agent_capabilities(
            task_id: str,
            tools: list[str],
            response: str,
        ) -> dict[str, Any]:
            """Grant requested local or mcp:<tool> capabilities, then resume the child."""
            try:
                return self.grant_capabilities(
                    task_id,
                    tools,
                    response,
                    expected_session_id=owner_session_id,
                )
            except Exception as exc:
                return self._tool_error(exc)

        return [
            create_agent_tasks,
            inspect_agent_task,
            wait_agent_tasks,
            request_agent_revision,
            accept_agent_result,
            cancel_agent_task,
            respond_agent_request,
            grant_agent_capabilities,
        ]

    def create_agent_task(
        self,
        *,
        name: str,
        objective: str,
        scope: list[str],
        acceptance: list[str],
        allowed_tools: list[str],
        workspace_mode: str = "required",
    ) -> dict[str, Any]:
        """Compatibility wrapper for a one-task delegation run."""
        result = self.create_agent_tasks(
            [
                {
                    "name": name,
                    "objective": objective,
                    "scope": scope,
                    "acceptance": acceptance,
                    "allowed_tools": allowed_tools,
                    "workspace_mode": workspace_mode,
                }
            ]
        )
        task = result["tasks"][0]
        return {"ok": True, "run_id": result["run_id"], **task}

    def create_agent_tasks(
        self,
        tasks: list[dict[str, Any]],
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist and dispatch one complete delegation group."""
        parent_session_id = session_id or self._session_id
        if self.config is None or parent_session_id is None:
            raise fail("SUBAGENT_UNAVAILABLE", "Subagent runtime is not configured.")
        if not 1 <= len(tasks) <= 2:
            raise fail(
                "SUBAGENT_CONTRACT_INVALID",
                "A delegation group must contain one or two child tasks.",
            )
        contracts = [self._normalize_contract(task) for task in tasks]
        recorder = active_recorder()
        turn_id = recorder.turn_id if recorder is not None else None
        with self._lock:
            if active := self.repository.active_run(parent_session_id):
                raise fail(
                    "SUBAGENT_CONCURRENCY_LIMIT",
                    f"Delegation run {active['run_id']} is still active.",
                )
            base_commit = self.workspaces.head_commit()
            run_id = self.repository.create_run(
                parent_session_id,
                base_commit,
                turn_id=turn_id,
                expected_task_count=len(contracts),
            )
            created = []
            for contract in contracts:
                task_id = self.repository.create_task(
                    run_id=run_id,
                    session_id=parent_session_id,
                    name=contract["name"],
                    objective=contract["objective"],
                    output_path=", ".join(contract["scope"]),
                    scope=contract["scope"],
                    acceptance=contract["acceptance"],
                )
                self._contracts[task_id] = contract
                parent_span_id = getattr(recorder, "root_span_id", None)
                if isinstance(parent_span_id, str):
                    contract["parent_span_id"] = parent_span_id
                attempt_id = self.repository.create_attempt(
                    task_id=task_id,
                    attempt_number=1,
                    base_commit=base_commit,
                    allowed_tools=tuple(contract["allowed_tools"]),
                    workspace_mode=contract["workspace_mode"],
                )
                self._event(
                    run_id,
                    task_id,
                    attempt_id,
                    "attempt.queued",
                    {
                        "attempt_number": 1,
                        "allowed_tools": contract["allowed_tools"],
                        "base_commit": base_commit,
                        "scope": contract["scope"],
                        "acceptance": contract["acceptance"],
                    },
                )
                created.append(
                    {
                        "task_id": task_id,
                        "attempt_id": attempt_id,
                        "status": "queued",
                    }
                )
            self.repository.set_run_status(run_id, "running")
            for item in created:
                self._submit_agent_attempt(
                    run_id,
                    str(item["task_id"]),
                    str(item["attempt_id"]),
                )
            return {"ok": True, "run_id": run_id, "tasks": created}

    def _normalize_contract(self, task: dict[str, Any]) -> dict[str, Any]:
        name = str(task.get("name", "")).strip()[:80]
        objective = str(task.get("objective", "")).strip()
        scope = [str(value) for value in task.get("scope", [])]
        acceptance = [str(value) for value in task.get("acceptance", [])]
        workspace_mode = str(task.get("workspace_mode", "required"))
        if workspace_mode not in {"none", "auto", "required"}:
            raise fail(
                "SUBAGENT_CONTRACT_INVALID",
                "workspace_mode must be one of: none, auto, required.",
            )
        grants = tuple(
            dict.fromkeys(str(value) for value in task.get("allowed_tools", []))
        )
        mcp_tools = tuple(
            value.removeprefix("mcp:")
            for value in grants
            if value.startswith("mcp:") and value.removeprefix("mcp:")
        )
        normalized_tools = tuple(value for value in grants if not value.startswith("mcp:"))
        malformed_mcp = [value for value in grants if value == "mcp:"]
        forbidden = sorted((set(normalized_tools) - _CHILD_TOOLS) | set(malformed_mcp))
        if forbidden:
            raise fail(
                "SUBAGENT_TOOL_FORBIDDEN",
                f"Tools are not available to child Agents: {', '.join(forbidden)}",
            )
        if workspace_mode == "none" and {"apply_patch", "run_command"} & set(normalized_tools):
            raise fail(
                "SUBAGENT_WORKSPACE_FORBIDDEN",
                "workspace_mode=none cannot grant apply_patch or run_command.",
            )
        if mcp_tools and (self.config is None or not self.config.mcp_servers):
            raise fail(
                "SUBAGENT_MCP_UNAVAILABLE",
                "MCP tools were granted, but no configured MCP server is available.",
            )
        if "run_command" in normalized_tools:
            normalized_tools = tuple(
                dict.fromkeys(
                    (
                        *normalized_tools,
                        "inspect_tool_run",
                        "wait_for_tools",
                        "cancel_tool_run",
                    )
                )
            )
        if not name or not objective or not scope or not acceptance or not grants:
            raise fail(
                "SUBAGENT_CONTRACT_INVALID",
                "name, objective, scope, acceptance, and allowed_tools must not be empty.",
            )
        child_contract(
            objective=objective, scope=scope, acceptance=acceptance,
            workspace_mode=workspace_mode,
        )
        return {
            "name": name,
            "objective": objective,
            "scope": scope,
            "acceptance": acceptance,
            "allowed_tools": list(grants),
            "local_tools": list(normalized_tools),
            "mcp_tools": list(mcp_tools),
            "workspace_mode": workspace_mode,
        }

    def _submit_agent_attempt(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str,
    ) -> None:
        cancelled = threading.Event()
        self._attempt_cancellations[attempt_id] = cancelled
        future = self.executor.submit(
            self._run_agent_task,
            run_id,
            task_id,
            attempt_id,
            cancelled,
        )
        self._futures.add(future)
        self._task_futures[task_id] = future

        def settled(done: Future[None]) -> None:
            with self._lock:
                self._futures.discard(done)
                if self._task_futures.get(task_id) is done:
                    self._task_futures.pop(task_id, None)
                if self._attempt_cancellations.get(attempt_id) is cancelled:
                    self._attempt_cancellations.pop(attempt_id, None)

        future.add_done_callback(settled)

    def _submit_demo_task(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str,
        requires_revision: bool,
    ) -> None:
        cancelled = threading.Event()
        self._attempt_cancellations[attempt_id] = cancelled
        future = self.executor.submit(
            self._run_task,
            run_id,
            task_id,
            requires_revision,
            cancelled,
        )
        self._futures.add(future)
        self._task_futures[task_id] = future

        def settled(done: Future[None]) -> None:
            with self._lock:
                self._futures.discard(done)
                if self._task_futures.get(task_id) is done:
                    self._task_futures.pop(task_id, None)
                for candidate, token in list(self._attempt_cancellations.items()):
                    if token is cancelled:
                        self._attempt_cancellations.pop(candidate, None)

        future.add_done_callback(settled)

    def cancel_task(
        self,
        task_id: str,
        *,
        expected_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Request cancellation for one task without affecting sibling tasks."""
        with self._lock:
            task = self._task_for_session(task_id, expected_session_id)
            status = str(task["status"])
            if status in _TERMINAL_TASKS:
                return {"ok": True, "task_id": task_id, "status": status}
            if status == "integrating":
                raise fail(
                    "SUBAGENT_NOT_CANCELLABLE",
                    "A child task cannot be cancelled while integration is in progress.",
                )
            self.repository.complete_task_wakes(task_id)
            attempt_id = str(task["active_attempt_id"])
            self._event(
                str(task["run_id"]),
                task_id,
                attempt_id,
                "cancellation.requested",
                {"prior_status": status},
            )
            cancelled = self._attempt_cancellations.get(attempt_id)
            if cancelled is not None:
                cancelled.set()
            future = self._task_futures.get(task_id)
            if status in {
                "awaiting_review",
                "revision_required",
                "waiting_parent",
                "waiting_capability",
            } or (
                future is not None and future.cancel()
            ):
                self._mark_cancelled(str(task["run_id"]), task_id, attempt_id)
                self._finish_run_if_ready(str(task["run_id"]))
                return {"ok": True, "task_id": task_id, "status": "cancelled"}
            self.repository.transition_task(
                task_id,
                ("queued", "running"),
                status="cancelling",
            )
            return {"ok": True, "task_id": task_id, "status": "cancelling"}

    def respond_to_request(
        self,
        task_id: str,
        response: str,
        *,
        expected_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Resume a child paused on an explicit parent-information request."""
        return self._resume_waiting_task(
            task_id,
            response=response,
            expected_session_id=expected_session_id,
        )

    def grant_capabilities(
        self,
        task_id: str,
        tools: list[str],
        response: str,
        *,
        expected_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Validate a capability delta against the parent catalog and resume."""
        with self._lock:
            task = self._task_for_session(task_id, expected_session_id)
            contract = self._contracts.get(task_id)
            if contract is None:
                raise fail(
                    "SUBAGENT_CONTEXT_MISSING",
                    "The child task contract is unavailable.",
                )
            candidate = self._normalize_contract(
                {
                    **contract,
                    "allowed_tools": [*contract["allowed_tools"], *tools],
                }
            )
            self._contracts[task_id] = candidate
            attempt_id = str(task["active_attempt_id"])
            self.repository.update_attempt(
                attempt_id,
                allowed_tools_json=json.dumps(candidate["allowed_tools"]),
            )
            self._event(
                str(task["run_id"]),
                task_id,
                attempt_id,
                "capability.granted",
                {"tools": tools, "response": response},
            )
            return self._resume_waiting_task(
                task_id,
                response=response,
                expected_session_id=expected_session_id,
            )

    def _resume_waiting_task(
        self,
        task_id: str,
        *,
        response: str,
        expected_session_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            task = self._task_for_session(task_id, expected_session_id)
            if task["status"] not in {"waiting_parent", "waiting_capability"}:
                raise fail(
                    "SUBAGENT_NOT_WAITING",
                    "The child task is not waiting for a parent response.",
                )
            self.repository.complete_task_wakes(task_id)
            attempt_id = str(task["active_attempt_id"])
            contract = self._contracts.get(task_id)
            if contract is None:
                raise fail("SUBAGENT_CONTEXT_MISSING", "The child task contract is unavailable.")
            contract.setdefault("parent_responses", []).append(response)
            if not self.repository.transition_task(
                task_id,
                ("waiting_parent", "waiting_capability"),
                status="running",
                feedback=response,
            ):
                raise fail("SUBAGENT_STATE_CHANGED", "The child task state already changed.")
            self.repository.transition_attempt(
                attempt_id,
                ("waiting_parent", "waiting_capability"),
                status="queued",
            )
            self._event(
                str(task["run_id"]),
                task_id,
                attempt_id,
                "parent.response",
                {"response": response},
            )
            self._submit_agent_attempt(str(task["run_id"]), task_id, attempt_id)
            return {
                "ok": True,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "status": "queued",
            }

    def close(self) -> None:
        self._cancelled.set()
        for cancelled in self._attempt_cancellations.values():
            cancelled.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.repository.close()

    def start_demo(self, session_id: str) -> dict[str, Any]:
        """Create two tasks and return immediately while their workers run."""
        with self._lock:
            if active := self.repository.active_run(session_id):
                raise fail(
                    "SUBAGENT_DEMO_ACTIVE",
                    f"Demo {active['run_id']} is already running for this session.",
                )
            base_commit = self.workspaces.head_commit()
            run_id = self.repository.create_run(session_id, base_commit)
            definitions = (
                (
                    "Agent A",
                    "生成通过验收的异步执行报告",
                    f".coding-agent-demo/{run_id}/agent-a.md",
                    False,
                ),
                (
                    "Agent B",
                    "先暴露验收失败，再根据主 Agent 反馈完成返工",
                    f".coding-agent-demo/{run_id}/agent-b.md",
                    True,
                ),
            )
            jobs: list[tuple[str, str, bool]] = []
            for name, objective, output_path, requires_revision in definitions:
                task_id = self.repository.create_task(
                    run_id=run_id,
                    session_id=session_id,
                    name=name,
                    objective=objective,
                    output_path=output_path,
                    scope=[output_path],
                    acceptance=[
                        "Only the declared deliverable path changes.",
                        "verification_status equals pass.",
                    ],
                )
                attempt_id = self.repository.create_attempt(
                    task_id=task_id,
                    attempt_number=1,
                    base_commit=base_commit,
                    allowed_tools=_ALLOWED_TOOLS,
                )
                self._event(
                    run_id,
                    task_id,
                    attempt_id,
                    "attempt.queued",
                    {
                        "attempt_number": 1,
                        "allowed_tools": list(_ALLOWED_TOOLS),
                        "base_commit": base_commit,
                    },
                )
                jobs.append((task_id, attempt_id, requires_revision))
            self.repository.set_run_status(run_id, "running")
            for task_id, attempt_id, requires_revision in jobs:
                self._submit_demo_task(
                    run_id,
                    task_id,
                    attempt_id,
                    requires_revision,
                )
            return self.repository.run(run_id)

    def latest(self, session_id: str) -> dict[str, Any] | None:
        return self.repository.latest_run(session_id)

    def inspect(self, run_id: str) -> dict[str, Any]:
        try:
            return self.repository.run(run_id)
        except KeyError as exc:
            raise fail("SUBAGENT_DEMO_NOT_FOUND", f"Unknown demo run: {run_id}") from exc

    def request_revision(
        self,
        task_id: str,
        feedback: str,
        *,
        expected_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Reject the current result and start a new Attempt from its commit."""
        with self._lock:
            task = self._task_for_session(task_id, expected_session_id)
            if task["status"] != "awaiting_review":
                raise fail(
                    "SUBAGENT_NOT_REVIEWABLE",
                    "Only a completed child task can be revised.",
                )
            previous_attempt_id = str(task["active_attempt_id"])
            previous = self.repository.attempt(previous_attempt_id)
            contract = self._contracts.get(task_id)
            if contract is None:
                raise fail(
                    "SUBAGENT_CONTEXT_MISSING",
                    "The task context is unavailable after a Host restart.",
                )
            attempt_number = int(previous["attempt_number"]) + 1
            if attempt_number > 3:
                raise fail(
                    "SUBAGENT_ATTEMPTS_EXHAUSTED",
                    "The task reached its Attempt limit.",
                )
            base_commit = str(previous["result_commit"] or previous["base_commit"])
            attempt_id = self.repository.create_revision_attempt(
                task_id=task_id,
                previous_attempt_id=previous_attempt_id,
                attempt_number=attempt_number,
                base_commit=base_commit,
                allowed_tools=tuple(contract["allowed_tools"]),
                workspace_mode=contract["workspace_mode"],
                feedback=feedback,
            )
            if attempt_id is None:
                raise fail(
                    "SUBAGENT_STATE_CHANGED",
                    "The child task state already changed.",
                )
            self.repository.complete_task_wakes(task_id)
            self._event(
                str(task["run_id"]),
                task_id,
                previous_attempt_id,
                "parent.feedback",
                {"decision": "REVISE", "message": feedback},
            )
            self._event(
                str(task["run_id"]),
                task_id,
                attempt_id,
                "attempt.queued",
                {
                    "attempt_number": attempt_number,
                    "allowed_tools": contract["allowed_tools"],
                    "base_commit": base_commit,
                    "reason": feedback,
                },
            )
            self._submit_agent_attempt(str(task["run_id"]), task_id, attempt_id)
            return {
                "ok": True,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "status": "queued",
            }

    def accept_result(
        self,
        task_id: str,
        *,
        expected_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply a reviewed child result to the parent workspace."""
        with self._lock:
            task = self._task_for_session(task_id, expected_session_id)
            if task["status"] != "awaiting_review":
                raise fail(
                    "SUBAGENT_NOT_REVIEWABLE",
                    "Only a completed child task can be accepted.",
                )
            self.repository.complete_task_wakes(task_id)
            attempt_id = str(task["active_attempt_id"])
            attempt = self.repository.attempt(attempt_id)
            result_commit = attempt.get("result_commit")
            if not result_commit:
                raw_result = attempt.get("result_envelope_json")
                result = json.loads(str(raw_result)) if raw_result else {}
                if result.get("result_kind") != "analysis":
                    raise fail(
                        "SUBAGENT_RESULT_MISSING",
                        "The child task has no result commit.",
                    )
                self.repository.update_attempt(attempt_id, status="accepted")
                self.repository.update_task(
                    task_id,
                    status="merged",
                    accepted_attempt_id=attempt_id,
                )
                self._event(
                    str(task["run_id"]),
                    task_id,
                    attempt_id,
                    "review.accepted",
                    {"decision": "ACCEPT", "result_kind": "analysis"},
                )
                self._finish_run_if_ready(str(task["run_id"]))
                return {
                    "ok": True,
                    "task_id": task_id,
                    "status": "merged",
                    "integration_commit": None,
                    "changed_paths": [],
                }
            contract = self._contracts.get(task_id)
            if contract is None:
                raise fail(
                    "SUBAGENT_CONTEXT_MISSING",
                    "The task context is unavailable after a Host restart.",
                )
            changed = self._workspace_changed_paths(
                str(attempt["base_commit"]),
                str(result_commit),
            )
            unexpected = self._unexpected_paths(changed, contract["scope"])
            if unexpected:
                raise fail(
                    "SUBAGENT_SCOPE_VIOLATION",
                    f"Child changed paths outside its contract: {', '.join(unexpected)}",
                )
            self._event(
                str(task["run_id"]),
                task_id,
                attempt_id,
                "review.started",
                {"checks": ["scope", "git_apply_check", "result_commit"]},
            )
            try:
                self.workspaces.check_apply_to_parent(
                    str(attempt["base_commit"]),
                    str(result_commit),
                )
                self.repository.update_task(task_id, status="integrating")
                integration_commit = self.workspaces.apply_to_parent(
                    str(attempt["base_commit"]),
                    str(result_commit),
                )
            except BaseException as exc:
                self.repository.update_task(task_id, status="awaiting_review")
                self._event(
                    str(task["run_id"]),
                    task_id,
                    attempt_id,
                    "integration.failed",
                    {"error": str(exc), "changed_paths": changed},
                )
                raise
            if not self.repository.complete_integration(
                task_id,
                attempt_id,
                integration_commit,
            ):
                raise fail(
                    "SUBAGENT_STATE_CHANGED",
                    "The child task state changed while integrating its result.",
                )
            self._event(
                str(task["run_id"]),
                task_id,
                attempt_id,
                "review.accepted",
                {"decision": "ACCEPT", "changed_paths": changed},
            )
            self._event(
                str(task["run_id"]),
                task_id,
                attempt_id,
                "integration.completed",
                {"integration_commit": integration_commit, "target": "parent workspace"},
            )
            self._finish_run_if_ready(str(task["run_id"]))
            return {
                "ok": True,
                "task_id": task_id,
                "status": "merged",
                "integration_commit": integration_commit,
                "changed_paths": changed,
            }

    def _run_agent_task(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str,
        cancelled: threading.Event,
    ) -> None:
        recorder: TraceRecorder | None = None
        if self._trace_store is not None and self.config is not None:
            task = self.repository.task(task_id)
            recorder = TraceRecorder(
                store=self._trace_store,
                trace_id=new_id(),
                turn_id=attempt_id,
                session_id=str(task["session_id"]),
                timeline_id=run_id,
                logical_run_id=task_id,
                model_id=self.config.model,
                secrets=(self.config.api_key or "",),
            )
            parent_span_id = self._contracts.get(task_id, {}).get("parent_span_id")
            if isinstance(parent_span_id, str):
                recorder.link_spans(parent_span_id, recorder.root_span_id, "delegated_to")
        try:
            self._run_agent_attempt(run_id, task_id, attempt_id, cancelled, recorder)
            if recorder is not None:
                recorder.finish(status="completed")
        except CodingAgentError as exc:
            if recorder is not None:
                recorder.finish(
                    status="cancelled" if exc.code == "SUBAGENT_CANCELLED" else "failed",
                    error=exc,
                )
            if exc.code == "SUBAGENT_CANCELLED":
                self._mark_cancelled(run_id, task_id, attempt_id)
            else:
                self._fail_task(run_id, task_id, exc)
        except BaseException as exc:
            if recorder is not None:
                recorder.finish(status="failed", error=exc)
            self._fail_task(run_id, task_id, exc)
        finally:
            if recorder is not None and self._trace_exporter is not None:
                self._trace_exporter.submit(recorder.trace_id)
            self._finish_run_if_ready(run_id)

    def _run_agent_attempt(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str,
        cancelled: threading.Event,
        recorder: TraceRecorder | None = None,
    ) -> None:
        from coding_agent.runtime import AgentRuntime, approve_by_default

        if self.config is None:
            raise fail("SUBAGENT_UNAVAILABLE", "Subagent runtime is not configured.")
        config = self.config
        if cancelled.is_set():
            raise fail("SUBAGENT_CANCELLED", "Child Agent execution was cancelled.")
        task = self.repository.task(task_id)
        attempt = self.repository.attempt(attempt_id)
        contract = self._contracts[task_id]
        attempt_number = int(attempt["attempt_number"])
        workspace_mode = str(contract["workspace_mode"])
        allocated: dict[str, Path | str] = {}
        allocation_lock = threading.Lock()
        parent_request: dict[str, Any] = {}
        submitted_result: dict[str, Any] = {}
        existing_worktree = attempt.get("worktree_path")
        if existing_worktree:
            worktree = Path(str(existing_worktree))
            prefix = self.workspace.root.relative_to(self.workspace.repo_root)
            allocated.update(
                worktree=worktree,
                attempt_workspace=worktree if prefix == Path(".") else worktree / prefix,
                branch="existing",
            )

        def allocate_workspace(reason: str) -> dict[str, Any]:
            if workspace_mode == "none":
                raise fail(
                    "SUBAGENT_WORKSPACE_FORBIDDEN",
                    "This analysis task is not allowed to create a worktree.",
                )
            with allocation_lock:
                if "worktree" not in allocated:
                    worktree, attempt_workspace, branch = self.workspaces.create_attempt(
                        run_id=run_id,
                        task_name=str(task["name"]),
                        attempt_id=attempt_id,
                        attempt_number=attempt_number,
                        base_commit=str(attempt["base_commit"]),
                    )
                    allocated.update(
                        worktree=worktree,
                        attempt_workspace=attempt_workspace,
                        branch=branch,
                    )
                    self.repository.update_attempt(
                        attempt_id,
                        worktree_path=str(worktree),
                        workspace_state="ready",
                    )
                    self._event(
                        run_id,
                        task_id,
                        attempt_id,
                        "workspace.allocated",
                        {"branch": branch, "reason": reason},
                    )
            return {"ok": True, "workspace_state": "ready"}

        if not self.repository.transition_attempt(
            attempt_id,
            ("queued",),
            status="running",
            started_at=utc_now().isoformat(),
        ):
            raise fail("SUBAGENT_CANCELLED", "Child Agent execution was cancelled.")
        if workspace_mode == "required":
            allocate_workspace("required by task contract")
        self._event(
            run_id,
            task_id,
            attempt_id,
            "agent.started",
            {
                "attempt_number": attempt_number,
                "workspace_mode": workspace_mode,
                "workspace_state": "ready" if allocated else "unallocated",
            },
        )
        role_instruction = child_contract(
            objective=contract["objective"],
            scope=contract["scope"],
            acceptance=contract["acceptance"],
            feedback=task.get("feedback") or "",
            parent_responses=contract.get("parent_responses", []),
            workspace_mode=workspace_mode,
        )
        self._event(
            run_id,
            task_id,
            attempt_id,
            "plan.updated",
            {
                "summary": "子 Agent 已接收任务契约并开始执行。",
                "scope": contract["scope"],
                "acceptance": contract["acceptance"],
                "workspace_mode": workspace_mode,
            },
        )
        checkpoint_dir = self.workspace.data_dir / "subagent-checkpoints"
        checkpoint_dir.mkdir(mode=0o700, exist_ok=True)

        @tool
        def request_parent_input(question: str) -> dict[str, Any]:
            """Pause after this turn and ask the parent Agent for missing information."""
            parent_request.update(kind="parent_input", question=question)
            return {"ok": True, "status": "waiting_parent", "instruction": "End this turn now."}

        @tool
        def request_capability(tools: list[str], reason: str) -> dict[str, Any]:
            """Pause after this turn and request additional local or MCP tools."""
            parent_request.update(kind="capability", tools=tools, reason=reason)
            return {
                "ok": True,
                "status": "waiting_capability",
                "instruction": "End this turn now.",
            }

        @tool
        def submit_agent_result(
            summary: str,
            checks: list[dict[str, Any]] | None = None,
            evidence: list[dict[str, Any]] | None = None,
            provenance: list[dict[str, Any]] | None = None,
            risks: list[str] | None = None,
            unresolved: list[str] | None = None,
        ) -> dict[str, Any]:
            """Submit the concise structured result consumed by review and the UI."""
            submitted_result.update(
                result_kind="analysis",
                summary=summary,
                checks=checks or [],
                evidence=evidence or [],
                provenance=provenance or [],
                risks=risks or [],
                unresolved=unresolved or [],
            )
            return {"ok": True, "status": "result_recorded"}

        parent_tools = [request_parent_input, request_capability, submit_agent_result]
        parent_tool_names = (
            "request_parent_input",
            "request_capability",
            "submit_agent_result",
        )
        thread_id = str(attempt.get("thread_id") or new_id())
        thread_initialized = bool(attempt.get("thread_id"))

        def run_phase(
            *,
            workspace_root: Path,
            repo_root: Path,
            allowed_tools: tuple[str, ...],
            extra_tools: list[BaseTool] | None,
            user_text: str,
        ) -> str:
            nonlocal thread_initialized
            runtime = AgentRuntime(
                config=config,
                workspace_root=workspace_root,
                repo_root=repo_root,
                checkpoint_path=checkpoint_dir / f"{attempt_id}.db",
                extra_tools=extra_tools,
                allowed_tool_names=frozenset(allowed_tools),
                allowed_mcp_tool_names=frozenset(contract["mcp_tools"]),
                role_instruction=role_instruction,
                model_call_limit=config.subagent_model_call_limit,
            )
            try:
                if not thread_initialized:
                    checkpoint_id = runtime.initialize_thread(thread_id)
                    self.repository.update_attempt(
                        attempt_id,
                        thread_id=thread_id,
                        checkpoint_id=checkpoint_id,
                    )
                    thread_initialized = True
                response, checkpoint_id = runtime.run_turn(
                    thread_id=thread_id,
                    user_text=user_text,
                    approve=approve_by_default,
                    recorder=recorder,
                    cancelled=cancelled,
                    cancellation_code="SUBAGENT_CANCELLED",
                    on_tool_event=lambda event_type, payload: self._event(
                        run_id,
                        task_id,
                        attempt_id,
                        event_type,
                        payload,
                    ),
                )
                self.repository.update_attempt(
                    attempt_id,
                    checkpoint_id=checkpoint_id,
                )
                return response
            finally:
                runtime.close()

        mcp_entry_tools = (
            ("activate_configured_mcp", "call_configured_mcp")
            if contract["mcp_tools"]
            else ()
        )
        if allocated:
            response = run_phase(
                workspace_root=Path(allocated["attempt_workspace"]),
                repo_root=Path(allocated["worktree"]),
                allowed_tools=tuple(
                    (*contract["local_tools"], *mcp_entry_tools, *parent_tool_names)
                ),
                extra_tools=parent_tools,
                user_text=contract["objective"],
            )
        else:
            extra_tools: list[BaseTool] = [*parent_tools]
            phase_tools = tuple(
                tool_name
                for tool_name in contract["local_tools"]
                if tool_name not in {"apply_patch", "run_command"}
            )
            if workspace_mode == "auto":

                @tool
                def request_workspace(reason: str) -> dict[str, Any]:
                    """Allocate an isolated worktree before starting file modifications."""
                    return allocate_workspace(reason)

                extra_tools.append(request_workspace)
                phase_tools = (*phase_tools, "request_workspace")
            response = run_phase(
                workspace_root=self.workspace.root,
                repo_root=self.workspace.repo_root,
                allowed_tools=tuple(
                    (*phase_tools, *mcp_entry_tools, *parent_tool_names)
                ),
                extra_tools=extra_tools,
                user_text=contract["objective"],
            )
            if allocated:
                submitted_result.clear()
                response = run_phase(
                    workspace_root=Path(allocated["attempt_workspace"]),
                    repo_root=Path(allocated["worktree"]),
                    allowed_tools=tuple(
                        (*contract["local_tools"], *mcp_entry_tools, *parent_tool_names)
                    ),
                    extra_tools=parent_tools,
                    user_text=phase_context(contract["objective"], response),
                )
            elif not parent_request:
                result = build_result_envelope(
                    json.dumps(submitted_result) if submitted_result else response,
                    result_kind="analysis",
                    changed_files=[],
                    result_commit=None,
                )
                if not self.repository.transition_attempt(
                    attempt_id,
                    ("running",),
                    status="completed",
                    result_envelope_json=result.model_dump_json(),
                    ended_at=utc_now().isoformat(),
                ):
                    raise fail("SUBAGENT_CANCELLED", "Child Agent execution was cancelled.")
                if not self.repository.transition_task(
                    task_id,
                    ("running",),
                    status="awaiting_review",
                ):
                    raise fail("SUBAGENT_CANCELLED", "Child Agent execution was cancelled.")
                self._event(
                    run_id,
                    task_id,
                    attempt_id,
                    "result.committed",
                    {
                        "result_commit": None,
                        "changed_paths": [],
                        "summary": result.summary,
                        "result": result.model_dump(
                            mode="json",
                            exclude={"legacy_raw_output"},
                        ),
                    },
                )
                return
        if parent_request:
            capability_request = parent_request["kind"] == "capability"
            status = "waiting_capability" if capability_request else "waiting_parent"
            event_type = (
                "capability.requested"
                if capability_request
                else "parent_input.requested"
            )
            payload = (
                {
                    "tools": parent_request["tools"],
                    "reason": parent_request["reason"],
                }
                if capability_request
                else {"question": parent_request["question"]}
            )
            event_id = self.repository.pause_for_parent(
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                status=status,
                event_type=event_type,
                payload=payload,
                wake_type=event_type,
                wake_priority=10,
            )
            if event_id is None:
                raise fail("SUBAGENT_CANCELLED", "Child Agent execution was cancelled.")
            self._notify_wake(task_id, event_id)
            return
        if cancelled.is_set():
            raise fail("SUBAGENT_CANCELLED", "Child Agent execution was cancelled.")
        worktree = Path(allocated["worktree"])
        result_commit = self.workspaces.commit(
            worktree,
            message=f"subagent: {task['name']} attempt {attempt_number}",
        )
        changed = self._workspace_changed_paths(
            str(attempt["base_commit"]),
            result_commit,
            worktree,
        )
        if not changed:
            raise fail("SUBAGENT_NO_CHANGES", "Child Agent completed without changing files.")
        unexpected = self._unexpected_paths(changed, contract["scope"])
        if unexpected:
            raise fail(
                "SUBAGENT_SCOPE_VIOLATION",
                f"Child changed paths outside its contract: {', '.join(unexpected)}",
            )
        result = build_result_envelope(
            json.dumps(submitted_result) if submitted_result else response,
            result_kind="patch",
            changed_files=changed,
            result_commit=result_commit,
        )
        if not self.repository.transition_attempt(
            attempt_id,
            ("running",),
            status="completed",
            result_commit=result_commit,
            result_envelope_json=result.model_dump_json(),
            ended_at=utc_now().isoformat(),
        ):
            raise fail("SUBAGENT_CANCELLED", "Child Agent execution was cancelled.")
        if not self.repository.transition_task(
            task_id,
            ("running",),
            status="awaiting_review",
        ):
            raise fail("SUBAGENT_CANCELLED", "Child Agent execution was cancelled.")
        self._event(
            run_id,
            task_id,
            attempt_id,
            "result.committed",
            {
                "result_commit": result_commit,
                "changed_paths": changed,
                "summary": result.summary,
                "result": result.model_dump(mode="json", exclude={"legacy_raw_output"}),
            },
        )

    def _workspace_changed_paths(
        self,
        base_commit: str,
        result_commit: str,
        worktree: Path | None = None,
    ) -> list[str]:
        changed = self.workspaces.changed_paths(
            base_commit,
            result_commit,
            cwd=worktree,
        )
        prefix = self.workspace.root.relative_to(self.workspace.repo_root)
        if prefix == Path("."):
            return changed
        result = []
        for path in changed:
            with_prefix = Path(path)
            if with_prefix.is_relative_to(prefix):
                result.append(with_prefix.relative_to(prefix).as_posix())
        return result

    @staticmethod
    def _unexpected_paths(changed: list[str], scope: list[str]) -> list[str]:
        return [
            path
            for path in changed
            if not any(
                fnmatch(path, pattern)
                or (
                    pattern.endswith("/**")
                    and path.startswith(pattern.removesuffix("**"))
                )
                for pattern in scope
            )
        ]

    @staticmethod
    def _tool_error(exc: Exception) -> dict[str, Any]:
        if isinstance(exc, CodingAgentError):
            return {"ok": False, "error_code": exc.code, "message": exc.user_message}
        return {"ok": False, "error_code": "SUBAGENT_ERROR", "message": str(exc)}

    def _run_task(
        self,
        run_id: str,
        task_id: str,
        requires_revision: bool,
        cancelled: threading.Event,
    ) -> None:
        try:
            first = self.repository.task(task_id)
            attempt_id = str(first["active_attempt_id"])
            accepted = self._run_attempt(
                run_id=run_id,
                task_id=task_id,
                attempt_id=attempt_id,
                should_pass=not requires_revision,
                cancelled=cancelled,
            )
            if not accepted and requires_revision:
                rejected = self.repository.attempt(attempt_id)
                result_commit = str(rejected["result_commit"])
                feedback = (
                    "验收项 verification_status 未通过；请保留现有交付物，"
                    "将状态修正为 pass 后重新提交。"
                )
                self.repository.update_task(
                    task_id,
                    status="revision_required",
                    feedback=feedback,
                )
                self._event(
                    run_id,
                    task_id,
                    attempt_id,
                    "parent.feedback",
                    {
                        "decision": "REVISE",
                        "message": feedback,
                        "blocking_finding": "verification_status must equal pass",
                    },
                )
                next_attempt_id = self.repository.create_attempt(
                    task_id=task_id,
                    attempt_number=2,
                    base_commit=result_commit,
                    allowed_tools=_ALLOWED_TOOLS,
                )
                self._attempt_cancellations.pop(attempt_id, None)
                self._attempt_cancellations[next_attempt_id] = cancelled
                self._event(
                    run_id,
                    task_id,
                    next_attempt_id,
                    "attempt.queued",
                    {
                        "attempt_number": 2,
                        "allowed_tools": list(_ALLOWED_TOOLS),
                        "base_commit": result_commit,
                        "reason": "主 Agent要求返工",
                    },
                )
                self._run_attempt(
                    run_id=run_id,
                    task_id=task_id,
                    attempt_id=next_attempt_id,
                    should_pass=True,
                    cancelled=cancelled,
                )
        except CodingAgentError as exc:
            if exc.code == "SUBAGENT_CANCELLED":
                task = self.repository.task(task_id)
                self._mark_cancelled(
                    run_id,
                    task_id,
                    str(task["active_attempt_id"]),
                )
            else:
                self._fail_task(run_id, task_id, exc)
        except BaseException as exc:
            self._fail_task(run_id, task_id, exc)
        finally:
            self._finish_run_if_ready(run_id)

    def _run_attempt(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt_id: str,
        should_pass: bool,
        cancelled: threading.Event,
    ) -> bool:
        task = self.repository.task(task_id)
        attempt = self.repository.attempt(attempt_id)
        attempt_number = int(attempt["attempt_number"])
        worktree, attempt_workspace, branch = self.workspaces.create_attempt(
            run_id=run_id,
            task_name=str(task["name"]),
            attempt_id=attempt_id,
            attempt_number=attempt_number,
            base_commit=str(attempt["base_commit"]),
        )
        self.repository.update_attempt(
            attempt_id,
            status="running",
            worktree_path=str(worktree),
            started_at=utc_now().isoformat(),
        )
        self._event(
            run_id,
            task_id,
            attempt_id,
            "agent.started",
            {
                "attempt_number": attempt_number,
                "branch": branch,
                "worktree": str(worktree),
            },
        )
        self._event(
            run_id,
            task_id,
            attempt_id,
            "plan.updated",
            {
                "summary": "执行授权的等待工具，生成交付物，提交 commit 并等待验收。",
                "steps": ["运行 mock_sleep", "写入交付物", "提交结果", "等待验收"],
            },
        )
        runtime = AttemptToolRuntime(
            workspace=attempt_workspace,
            allowed_tools=tuple(json.loads(str(attempt["allowed_tools_json"]))),
            cancelled=cancelled,
            on_event=lambda event_type, payload: self._event(
                run_id,
                task_id,
                attempt_id,
                event_type,
                payload,
            ),
        )
        runtime.invoke("mock_sleep", {"seconds": 10})
        status = "pass" if should_pass else "fail"
        content = (
            f"# {task['name']} deliverable\n\n"
            f"attempt: {attempt_number}\n"
            f"verification_status: {status}\n"
            f"allowed_tools: {', '.join(_ALLOWED_TOOLS)}\n"
        )
        runtime.invoke(
            "write_deliverable",
            {"path": str(task["output_path"]), "content": content},
        )
        if cancelled.is_set():
            raise fail("SUBAGENT_CANCELLED", "Subagent execution was cancelled.")
        result_commit = self.workspaces.commit(
            worktree,
            message=f"demo(subagent): {task['name']} attempt {attempt_number}",
        )
        self.repository.update_attempt(
            attempt_id,
            status="completed",
            result_commit=result_commit,
            ended_at=utc_now().isoformat(),
        )
        self.repository.update_task(task_id, status="awaiting_review")
        self._event(
            run_id,
            task_id,
            attempt_id,
            "result.committed",
            {
                "result_commit": result_commit,
                "output_path": task["output_path"],
            },
        )
        return self._review_and_integrate(run_id, task_id, attempt_id)

    def _review_and_integrate(self, run_id: str, task_id: str, attempt_id: str) -> bool:
        task = self.repository.task(task_id)
        attempt = self.repository.attempt(attempt_id)
        result_commit = str(attempt["result_commit"])
        self._event(
            run_id,
            task_id,
            attempt_id,
            "review.started",
            {"checks": ["scope", "verification_status", "result_commit"]},
        )
        expected_repo_path = (self.workspace.root / str(task["output_path"])).relative_to(
            self.workspace.repo_root
        )
        changed = self.workspaces.changed_paths(str(attempt["base_commit"]), result_commit)
        content = self.workspaces.file_at_commit(result_commit, str(task["output_path"]))
        scope_ok = changed == [expected_repo_path.as_posix()]
        verification_ok = "verification_status: pass" in content
        if not scope_ok or not verification_ok:
            findings = []
            if not scope_ok:
                findings.append("提交包含授权范围外的文件")
            if not verification_ok:
                findings.append("verification_status 不是 pass")
            self.repository.update_attempt(
                attempt_id,
                status="rejected",
                ended_at=utc_now().isoformat(),
            )
            self._event(
                run_id,
                task_id,
                attempt_id,
                "review.rejected",
                {
                    "decision": "REVISE",
                    "findings": findings,
                    "changed_paths": changed,
                },
            )
            return False

        self.repository.update_attempt(attempt_id, status="accepted")
        self.repository.update_task(
            task_id,
            status="integrating",
            accepted_attempt_id=attempt_id,
        )
        integration_commit = self.workspaces.integrate(
            run_id,
            task_id,
            result_commit,
        )
        self.repository.update_task(
            task_id,
            status="merged",
            integration_commit=integration_commit,
        )
        self._event(
            run_id,
            task_id,
            attempt_id,
            "review.accepted",
            {
                "decision": "ACCEPT",
                "changed_paths": changed,
                "verification_status": "pass",
            },
        )
        self._event(
            run_id,
            task_id,
            attempt_id,
            "integration.completed",
            {
                "integration_commit": integration_commit,
                "target": f"refs/coding-agent/subagent-integrations/{run_id}/{task_id}",
            },
        )
        return True

    def _fail_task(self, run_id: str, task_id: str, exc: BaseException) -> None:
        task = self.repository.task(task_id)
        attempt_id = task.get("active_attempt_id")
        message = exc.user_message if isinstance(exc, CodingAgentError) else str(exc)
        transitioned = self.repository.transition_task(
            task_id,
            (
                "queued",
                "running",
                "cancelling",
                "integrating",
                "waiting_parent",
                "waiting_capability",
                "revision_required",
            ),
            status="failed",
        )
        if not transitioned:
            return
        if attempt_id:
            self.repository.transition_attempt(
                str(attempt_id),
                ("queued", "running", "waiting_parent", "waiting_capability"),
                status="failed",
                ended_at=utc_now().isoformat(),
                error=message,
            )
        self._event(
            run_id,
            task_id,
            str(attempt_id) if attempt_id else None,
            "agent.failed",
            {"error": message},
        )

    def _mark_cancelled(self, run_id: str, task_id: str, attempt_id: str) -> None:
        if not self.repository.transition_task(
            task_id,
            (
                "queued",
                "running",
                "cancelling",
                "awaiting_review",
                "revision_required",
                "waiting_parent",
                "waiting_capability",
            ),
            status="cancelled",
        ):
            return
        self.repository.transition_attempt(
            attempt_id,
            (
                "queued",
                "running",
                "completed",
                "waiting_parent",
                "waiting_capability",
                "rejected",
            ),
            status="cancelled",
            ended_at=utc_now().isoformat(),
            error="Cancelled by parent Agent",
        )
        self._event(
            run_id,
            task_id,
            attempt_id,
            "agent.cancelled",
            {"reason": "Cancelled by parent Agent"},
        )

    def _finish_run_if_ready(self, run_id: str) -> None:
        run = self.repository.run(run_id)
        statuses = {str(task["status"]) for task in run["tasks"]}
        expected = int(run["expected_task_count"])
        if len(run["tasks"]) != expected or not statuses.issubset(_TERMINAL_TASKS):
            return
        if "failed" in statuses:
            status = "failed"
        elif "cancelled" in statuses:
            status = "cancelled"
        else:
            status = "completed"
        self.repository.set_run_status(
            run_id,
            status,
            expected=("queued", "running"),
        )

    def _event(
        self,
        run_id: str,
        task_id: str,
        attempt_id: str | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        wake_types = {
            "result.committed": ("result.ready", 50),
            "agent.failed": ("task.failed", 20),
            "parent_input.requested": ("parent_input.requested", 10),
            "capability.requested": ("capability.requested", 10),
        }
        wake = wake_types.get(event_type)
        event_id = self.repository.append_event(
            run_id=run_id,
            task_id=task_id,
            attempt_id=attempt_id,
            event_type=event_type,
            payload=payload,
            wake_type=wake[0] if wake else None,
            wake_priority=wake[1] if wake else 100,
        )
        if wake is not None:
            self._notify_wake(task_id, event_id)

    def _notify_wake(self, task_id: str, event_id: int) -> None:
        if self._on_wake is None:
            return
        task = self.repository.task(task_id)
        request = next(
            (
                item
                for item in self.repository.pending_wakes(str(task["session_id"]))
                if int(item["event_id"]) == event_id
            ),
            None,
        )
        if request is not None:
            self._on_wake(request)
