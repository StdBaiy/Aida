import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coding_agent.config import AgentConfig
from coding_agent.errors import CodingAgentError, fail
from coding_agent.subagents import SubagentDemoManager
from coding_agent.subagents.results import build_result_envelope
from coding_agent.subagents.tools import AttemptToolRuntime
from coding_agent.tracing.context import activate_recorder
from coding_agent.workspace import resolve_workspace


def git(root: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return process.stdout.strip()


def repository(tmp_path: Path) -> Path:
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "README.md").write_text("# fixture\n")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-m", "baseline")
    return tmp_path


def test_attempt_runtime_rejects_tools_not_granted(tmp_path: Path) -> None:
    runtime = AttemptToolRuntime(
        workspace=tmp_path,
        allowed_tools=("write_deliverable",),
        cancelled=threading.Event(),
        on_event=lambda _event, _payload: None,
    )

    with pytest.raises(CodingAgentError) as error:
        runtime.invoke("mock_sleep", {"seconds": 10})

    assert error.value.code == "SUBAGENT_TOOL_FORBIDDEN"


def test_unstructured_child_output_is_not_used_as_result_summary() -> None:
    raw = "= · = · P · R · O · V · E · N · A · N · C · E"

    result = build_result_envelope(
        raw,
        result_kind="patch",
        changed_files=["result.txt"],
        result_commit="abc123",
    )

    assert result.summary == "子 Agent 已完成任务，等待主 Agent review。"
    assert result.legacy_raw_output == raw
    assert result.changed_files == ["result.txt"]
    assert result.result_commit == "abc123"


def test_structured_child_result_preserves_provenance() -> None:
    result = build_result_envelope(
        """{
          "result_kind": "analysis",
          "summary": "Located the scheduler behavior.",
          "provenance": [{"provider": "deep_wiki", "tool": "ask"}]
        }""",
        result_kind="analysis",
        changed_files=[],
        result_commit=None,
    )

    assert result.summary == "Located the scheduler behavior."
    assert result.provenance[0].provider == "deep_wiki"


def test_demo_runs_two_tasks_and_revises_failed_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository(tmp_path)
    started = threading.Barrier(2)
    sleep_calls = 0
    sleep_lock = threading.Lock()

    def fast_sleep(
        self: AttemptToolRuntime,
        seconds: int = 10,
    ) -> dict[str, Any]:
        nonlocal sleep_calls
        del self
        with sleep_lock:
            sleep_calls += 1
            call_number = sleep_calls
        if call_number <= 2:
            started.wait(timeout=3)
        return {"slept_seconds": seconds, "duration_ms": seconds * 1000}

    monkeypatch.setattr(AttemptToolRuntime, "_mock_sleep", fast_sleep)
    manager = SubagentDemoManager(resolve_workspace(root))
    try:
        created = manager.start_demo("session-1")
        assert created["status"] == "running"
        assert len(created["tasks"]) == 2

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = manager.inspect(created["run_id"])
            if result["status"] != "running":
                break
            time.sleep(0.05)
        else:
            pytest.fail("subagent demo did not finish")

        assert result["status"] == "completed"
        tasks = {task["name"]: task for task in result["tasks"]}
        assert tasks["Agent A"]["status"] == "merged"
        assert len(tasks["Agent A"]["attempts"]) == 1
        assert tasks["Agent B"]["status"] == "merged"
        assert len(tasks["Agent B"]["attempts"]) == 2
        assert tasks["Agent B"]["attempts"][0]["status"] == "rejected"
        assert tasks["Agent B"]["attempts"][1]["status"] == "accepted"
        assert tasks["Agent B"]["feedback"]
        assert any(
            event["event_type"] == "parent.feedback"
            for event in tasks["Agent B"]["events"]
        )
        worktrees = {
            attempt["worktree_path"]
            for task in result["tasks"]
            for attempt in task["attempts"]
        }
        assert len(worktrees) == 3
        assert None not in worktrees
    finally:
        manager.close()


def test_main_agent_can_dispatch_two_isolated_tasks_and_integrate_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository(tmp_path)
    started = threading.Barrier(2)
    granted_tools: list[frozenset[str]] = []

    class FakeChildRuntime:
        def __init__(
            self,
            *,
            workspace_root: Path,
            allowed_tool_names: frozenset[str],
            role_instruction: str,
            **_kwargs: Any,
        ) -> None:
            self.workspace_root = workspace_root
            self.role_instruction = role_instruction
            granted_tools.append(allowed_tool_names)

        def initialize_thread(self, _thread_id: str) -> str:
            return "checkpoint"

        def run_turn(self, **_kwargs: Any) -> tuple[str, str]:
            started.wait(timeout=3)
            path = (
                self.workspace_root / "backend.txt"
                if "backend" in self.role_instruction
                else self.workspace_root / "frontend.txt"
            )
            path.write_text(self.role_instruction)
            return "implemented and checked", "checkpoint"

        def close(self) -> None:
            pass

    monkeypatch.setattr("coding_agent.runtime.AgentRuntime", FakeChildRuntime)
    manager = SubagentDemoManager(
        resolve_workspace(root),
        AgentConfig(model="test", api_key="test-key"),
    )
    manager.bind_session("session-1")
    try:
        created = manager.create_agent_tasks(
            [
                {
                    "name": "Backend",
                    "objective": "implement backend contract",
                    "scope": ["backend.txt"],
                    "acceptance": ["backend file exists"],
                    "allowed_tools": ["read_file", "apply_patch"],
                },
                {
                    "name": "Frontend",
                    "objective": "implement frontend contract",
                    "scope": ["frontend.txt"],
                    "acceptance": ["frontend file exists"],
                    "allowed_tools": ["read_file", "apply_patch"],
                },
            ]
        )
        backend, frontend = created["tasks"]

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            backend_task = manager.repository.task(backend["task_id"])
            frontend_task = manager.repository.task(frontend["task_id"])
            if {
                backend_task["status"],
                frontend_task["status"],
            } == {"awaiting_review"}:
                break
            time.sleep(0.05)
        else:
            pytest.fail("child Agents did not produce reviewable results")

        backend_result = manager.accept_result(backend["task_id"])
        frontend_result = manager.accept_result(frontend["task_id"])

        assert backend_result["status"] == "merged"
        assert frontend_result["status"] == "merged"
        assert (root / "backend.txt").is_file()
        assert (root / "frontend.txt").is_file()
        assert len(granted_tools) == 2
        assert all(
            tools
            == {
                "read_file",
                "apply_patch",
                "request_parent_input",
                "request_capability",
                "submit_agent_result",
            }
            for tools in granted_tools
        )
        assert manager.inspect(created["run_id"])["status"] == "completed"
    finally:
        manager.close()


def test_single_child_run_closes_after_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository(tmp_path)

    class FakeChildRuntime:
        def __init__(self, *, workspace_root: Path, **_kwargs: Any) -> None:
            self.workspace_root = workspace_root

        def initialize_thread(self, _thread_id: str) -> str:
            return "checkpoint"

        def run_turn(self, **_kwargs: Any) -> tuple[str, str]:
            (self.workspace_root / "result.txt").write_text("done")
            return "done", "checkpoint"

        def close(self) -> None:
            pass

    monkeypatch.setattr("coding_agent.runtime.AgentRuntime", FakeChildRuntime)
    manager = SubagentDemoManager(
        resolve_workspace(root),
        AgentConfig(model="test", api_key="test-key"),
    )
    manager.bind_session("session-1")
    try:
        with activate_recorder(SimpleNamespace(turn_id="turn-1")):  # type: ignore[arg-type]
            created = manager.create_agent_task(
                name="Only child",
                objective="write result",
                scope=["result.txt"],
                acceptance=["result exists"],
                allowed_tools=["apply_patch"],
            )
        deadline = time.monotonic() + 5
        while manager.repository.task(created["task_id"])["status"] != "awaiting_review":
            if time.monotonic() >= deadline:
                pytest.fail("child did not become reviewable")
            time.sleep(0.02)

        manager.accept_result(created["task_id"])

        run = manager.inspect(created["run_id"])
        assert run["status"] == "completed"
        assert run["turn_id"] == "turn-1"
    finally:
        manager.close()


def test_analysis_child_completes_without_allocating_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository(tmp_path)

    class FakeChildRuntime:
        def __init__(self, *, workspace_root: Path, **_kwargs: Any) -> None:
            self.workspace_root = workspace_root

        def initialize_thread(self, _thread_id: str) -> str:
            return "checkpoint"

        def run_turn(self, **_kwargs: Any) -> tuple[str, str]:
            return (
                '{"result_kind":"analysis","summary":"No code change is required."}',
                "checkpoint",
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("coding_agent.runtime.AgentRuntime", FakeChildRuntime)
    manager = SubagentDemoManager(
        resolve_workspace(root),
        AgentConfig(model="test", api_key="test-key"),
    )
    manager.bind_session("session-1")
    try:
        created = manager.create_agent_task(
            name="Analysis",
            objective="inspect the repository",
            scope=["README.md"],
            acceptance=["report findings"],
            allowed_tools=["read_file"],
            workspace_mode="none",
        )
        deadline = time.monotonic() + 5
        while manager.repository.task(created["task_id"])["status"] != "awaiting_review":
            if time.monotonic() >= deadline:
                pytest.fail("analysis child did not become reviewable")
            time.sleep(0.02)

        run = manager.inspect(created["run_id"])
        attempt = run["tasks"][0]["attempts"][0]
        assert attempt["worktree_path"] is None
        assert attempt["workspace_state"] == "unallocated"
        assert attempt["result"]["result_kind"] == "analysis"
        assert manager.repository.pending_wakes("session-1")[0]["wake_type"] == "result.ready"

        accepted = manager.accept_result(created["task_id"])
        assert accepted["status"] == "merged"
        assert accepted["integration_commit"] is None
    finally:
        manager.close()


def test_child_can_request_parent_input_and_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository(tmp_path)
    invocations = 0
    initialized_threads: list[str] = []

    class FakeChildRuntime:
        def __init__(self, *, extra_tools: list[Any] | None = None, **_kwargs: Any) -> None:
            self.extra_tools = extra_tools or []

        def initialize_thread(self, thread_id: str) -> str:
            initialized_threads.append(thread_id)
            return "checkpoint"

        def run_turn(self, **_kwargs: Any) -> tuple[str, str]:
            nonlocal invocations
            invocations += 1
            if invocations == 1:
                request_tool = next(
                    item for item in self.extra_tools if item.name == "request_parent_input"
                )
                request_tool.invoke({"question": "Which API behavior is expected?"})
                return "waiting for parent", "checkpoint"
            return (
                '{"result_kind":"analysis","summary":"Used the parent response."}',
                "checkpoint",
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("coding_agent.runtime.AgentRuntime", FakeChildRuntime)
    manager = SubagentDemoManager(
        resolve_workspace(root),
        AgentConfig(model="test", api_key="test-key"),
    )
    manager.bind_session("session-1")
    try:
        created = manager.create_agent_task(
            name="Needs context",
            objective="inspect behavior",
            scope=["README.md"],
            acceptance=["report findings"],
            allowed_tools=["read_file"],
            workspace_mode="none",
        )
        deadline = time.monotonic() + 5
        while manager.repository.task(created["task_id"])["status"] != "waiting_parent":
            if time.monotonic() >= deadline:
                pytest.fail("child did not request parent input")
            time.sleep(0.02)

        wakes = manager.repository.pending_wakes("session-1")
        assert wakes[0]["wake_type"] == "parent_input.requested"
        resumed = manager.respond_to_request(created["task_id"], "Use the documented API.")
        assert resumed["status"] == "queued"

        deadline = time.monotonic() + 5
        while manager.repository.task(created["task_id"])["status"] != "awaiting_review":
            if time.monotonic() >= deadline:
                pytest.fail("child did not resume")
            time.sleep(0.02)
        assert invocations == 2
        assert len(initialized_threads) == 1
    finally:
        manager.close()


def test_auto_workspace_is_allocated_only_after_child_requests_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository(tmp_path)
    invocations = 0

    class FakeChildRuntime:
        def __init__(
            self,
            *,
            workspace_root: Path,
            extra_tools: list[Any] | None = None,
            **_kwargs: Any,
        ) -> None:
            self.workspace_root = workspace_root
            self.extra_tools = extra_tools or []

        def initialize_thread(self, _thread_id: str) -> str:
            return "checkpoint"

        def run_turn(self, **_kwargs: Any) -> tuple[str, str]:
            nonlocal invocations
            invocations += 1
            if invocations == 1:
                request_tool = next(
                    item for item in self.extra_tools if item.name == "request_workspace"
                )
                request_tool.invoke({"reason": "The fix requires editing result.txt"})
                return "worktree requested", "checkpoint"
            (self.workspace_root / "result.txt").write_text("done")
            return "implemented", "checkpoint"

        def close(self) -> None:
            pass

    monkeypatch.setattr("coding_agent.runtime.AgentRuntime", FakeChildRuntime)
    manager = SubagentDemoManager(
        resolve_workspace(root),
        AgentConfig(model="test", api_key="test-key"),
    )
    manager.bind_session("session-1")
    try:
        created = manager.create_agent_task(
            name="Lazy edit",
            objective="write result only when needed",
            scope=["result.txt"],
            acceptance=["result exists"],
            allowed_tools=["read_file", "apply_patch"],
            workspace_mode="auto",
        )
        deadline = time.monotonic() + 5
        while manager.repository.task(created["task_id"])["status"] != "awaiting_review":
            if time.monotonic() >= deadline:
                pytest.fail("auto workspace child did not finish")
            time.sleep(0.02)

        attempt = manager.inspect(created["run_id"])["tasks"][0]["attempts"][0]
        assert invocations == 2
        assert attempt["workspace_state"] == "ready"
        assert attempt["worktree_path"]
        assert not (root / "result.txt").exists()
    finally:
        manager.close()


def test_integration_failure_returns_task_to_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository(tmp_path)

    class FakeChildRuntime:
        def __init__(self, *, workspace_root: Path, **_kwargs: Any) -> None:
            self.workspace_root = workspace_root

        def initialize_thread(self, _thread_id: str) -> str:
            return "checkpoint"

        def run_turn(self, **_kwargs: Any) -> tuple[str, str]:
            (self.workspace_root / "result.txt").write_text("done")
            return "done", "checkpoint"

        def close(self) -> None:
            pass

    monkeypatch.setattr("coding_agent.runtime.AgentRuntime", FakeChildRuntime)
    manager = SubagentDemoManager(
        resolve_workspace(root),
        AgentConfig(model="test", api_key="test-key"),
    )
    manager.bind_session("session-1")
    try:
        created = manager.create_agent_task(
            name="Only child",
            objective="write result",
            scope=["result.txt"],
            acceptance=["result exists"],
            allowed_tools=["apply_patch"],
        )
        deadline = time.monotonic() + 5
        while manager.repository.task(created["task_id"])["status"] != "awaiting_review":
            if time.monotonic() >= deadline:
                pytest.fail("child did not become reviewable")
            time.sleep(0.02)

        monkeypatch.setattr(
            manager.workspaces,
            "apply_to_parent",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("conflict")),
        )
        with pytest.raises(RuntimeError, match="conflict"):
            manager.accept_result(created["task_id"])

        task = manager.inspect(created["run_id"])["tasks"][0]
        assert task["status"] == "awaiting_review"
        assert task["attempts"][0]["status"] == "completed"
        assert task["events"][-1]["event_type"] == "integration.failed"
    finally:
        manager.close()


def test_running_child_can_be_cancelled_without_a_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = repository(tmp_path)
    started = threading.Event()

    class FakeChildRuntime:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def initialize_thread(self, _thread_id: str) -> str:
            return "checkpoint"

        def run_turn(self, **kwargs: Any) -> tuple[str, str]:
            started.set()
            cancelled = kwargs["cancelled"]
            assert cancelled.wait(timeout=3)
            raise fail("SUBAGENT_CANCELLED", "cancelled")

        def close(self) -> None:
            pass

    monkeypatch.setattr("coding_agent.runtime.AgentRuntime", FakeChildRuntime)
    manager = SubagentDemoManager(
        resolve_workspace(root),
        AgentConfig(model="test", api_key="test-key"),
    )
    manager.bind_session("session-1")
    try:
        created = manager.create_agent_task(
            name="Cancellable child",
            objective="wait",
            scope=["result.txt"],
            acceptance=["result exists"],
            allowed_tools=["apply_patch"],
        )
        assert started.wait(timeout=3)

        result = manager.cancel_task(created["task_id"])
        assert result["status"] in {"cancelling", "cancelled"}

        deadline = time.monotonic() + 5
        while manager.repository.task(created["task_id"])["status"] != "cancelled":
            if time.monotonic() >= deadline:
                pytest.fail("child did not cancel")
            time.sleep(0.02)
        assert manager.inspect(created["run_id"])["status"] == "cancelled"
    finally:
        manager.close()


def test_restart_preserves_reviewable_child_and_requeues_claimed_wake(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    workspace = resolve_workspace(root)
    manager = SubagentDemoManager(
        workspace,
        AgentConfig(model="test", api_key="test-key"),
    )
    run_id = manager.repository.create_run("session-1", git(root, "rev-parse", "HEAD"))
    task_id = manager.repository.create_task(
        run_id=run_id,
        session_id="session-1",
        name="Analysis",
        objective="inspect state",
        output_path="README.md",
        scope=["README.md"],
        acceptance=["summary exists"],
    )
    attempt_id = manager.repository.create_attempt(
        task_id=task_id,
        attempt_number=1,
        base_commit=git(root, "rev-parse", "HEAD"),
        allowed_tools=("read_file",),
        workspace_mode="none",
    )
    result = build_result_envelope(
        '{"summary":"done"}',
        result_kind="analysis",
        changed_files=[],
        result_commit=None,
    )
    manager.repository.update_attempt(
        attempt_id,
        status="completed",
        result_envelope_json=result.model_dump_json(),
    )
    manager.repository.update_task(task_id, status="awaiting_review")
    manager.repository.set_run_status(run_id, "running")
    manager.repository.append_event(
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        event_type="result.committed",
        payload={"summary": "done"},
        wake_type="result.ready",
    )
    assert manager.repository.claim_next_wake("session-1") is not None
    manager.close()

    recovered = SubagentDemoManager(
        workspace,
        AgentConfig(model="test", api_key="test-key"),
    )
    try:
        assert recovered.repository.task(task_id)["status"] == "awaiting_review"
        assert len(recovered.repository.pending_wakes("session-1")) == 1
        accepted = recovered.accept_result(task_id)
        assert accepted["status"] == "merged"
    finally:
        recovered.close()
