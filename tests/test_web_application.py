import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from coding_agent.api import create_app
from coding_agent.application.approvals import ApprovalBroker
from coding_agent.application.events import EventJournal
from coding_agent.application.service import CodingAgentHost
from coding_agent.errors import CodingAgentError
from coding_agent.models import Workspace
from coding_agent.repository import SqliteCheckpointRepository


class FakeHost:
    def __init__(self) -> None:
        self.updated_settings: dict[str, Any] | None = None

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {
            "service": "ready",
            "workspace": "/tmp/repo",
            "workspace_name": "repo",
            "branch": "main",
            "model": "test",
            "session_id": "session",
            "timeline_id": "timeline",
            "active_operation": None,
            "pending_approvals": [],
        }

    def sessions(self, *, limit: int, offset: int) -> dict[str, Any]:
        return {"items": [], "next_offset": None}

    def settings(self) -> dict[str, Any]:
        return {
            "model": "test",
            "base_url": "https://api.example.com/v1",
            "langsmith_enabled": False,
            "langsmith_project": "test-project",
            "model_timeout_seconds": 180,
            "main_agent_model_call_limit": 20,
            "subagent_model_call_limit": 20,
            "command_timeout_seconds": 120,
            "max_parallel_sessions": 4,
            "model_api_key_configured": True,
            "langsmith_api_key_configured": False,
        }

    async def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        self.updated_settings = values
        return {**self.settings(), **values}

    async def create_session(self) -> dict[str, Any]:
        return {"session_id": "new-session"}

    def subagent_runs(self, session_id: str) -> list[dict[str, Any]]:
        del session_id
        return []

    def cancel_subagent_task(self, task_id: str) -> dict[str, Any]:
        return {"ok": True, "task_id": task_id, "status": "cancelled"}

    def cancel_operation(self, operation_id: str) -> dict[str, Any]:
        return {"operation_id": operation_id, "status": "cancel_requested"}

    def context(self, session_id: str) -> dict[str, Any]:
        return {
            "context_owner_id": f"main:{session_id}:timeline",
            "session_id": session_id,
            "timeline_id": "timeline",
            "used_tokens": 800,
            "max_tokens": 1_000,
            "usage_ratio": 0.8,
            "message_count": 4,
            "compression_count": 0,
            "updated_at": "2026-09-17T00:00:00Z",
        }

    def turn_context(self, session_id: str, turn_number: int) -> dict[str, Any]:
        return {
            "turn": {"turn_number": turn_number},
            "context": {
                "checkpoint_id": "checkpoint",
                "message_count": 1,
                "messages": [{"type": "human", "content": f"{session_id}:{turn_number}"}],
            },
        }

    def submit_context_compaction(self, **values: Any) -> dict[str, Any]:
        return {
            "operation_id": "compact-operation",
            "status": "queued",
            **values,
        }


def test_web_host_requests_approval_only_for_skill_activation() -> None:
    host = CodingAgentHost.__new__(CodingAgentHost)
    requests: list[tuple[str, dict[str, Any]]] = []
    host.approvals = SimpleNamespace(
        request=lambda operation_id, request: requests.append((operation_id, request)) or False
    )

    assert host._approve_turn_request(
        "operation", {"name": "run_command", "args": {"argv": ["pytest"]}}
    )
    assert not host._approve_turn_request(
        "operation", {"name": "activate_skill", "args": {"name": "alpha"}}
    )
    assert requests == [
        ("operation", {"name": "activate_skill", "args": {"name": "alpha"}})
    ]


def test_event_journal_replays_ordered_events_and_idempotency(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "agent.db")
    operation_id, created = journal.create_operation(
        session_id="session",
        timeline_id="timeline",
        kind="turn",
        client_request_id="request-1",
    )
    replayed_id, replayed_created = journal.create_operation(
        session_id="session",
        timeline_id="timeline",
        kind="turn",
        client_request_id="request-1",
    )
    journal.append(operation_id, "assistant.delta", {"text": "hello"})
    journal.append(operation_id, "turn.committed", {"turn_number": 1})

    assert created
    assert not replayed_created
    assert replayed_id == operation_id
    assert [event["sequence"] for event in journal.events_after(operation_id, 0)] == [1, 2, 3]
    assert [event["event_type"] for event in journal.events_after(operation_id, 1)] == [
        "assistant.delta",
        "turn.committed",
    ]
    journal.close()


def test_running_operation_can_request_cancellation(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "agent.db")
    operation_id, _ = journal.create_operation(
        session_id="session",
        timeline_id="timeline",
        kind="turn",
        client_request_id="request-1",
    )
    journal.set_status(operation_id, "running")

    assert journal.request_cancel(operation_id)
    assert not journal.request_cancel(operation_id)
    assert journal.operation(operation_id)["status"] == "cancel_requested"  # type: ignore[index]
    assert journal.active_operation()["operation_id"] == operation_id  # type: ignore[index]
    journal.close()


def test_interrupted_operation_gets_a_replayable_terminal_event(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    journal = EventJournal(database)
    operation_id, _ = journal.create_operation(
        session_id="session",
        timeline_id="timeline",
        kind="turn",
        client_request_id="request-1",
    )
    journal.set_status(operation_id, "running")
    journal.close()

    recovered = EventJournal(database)

    assert recovered.operation(operation_id)["status"] == "recovery_required"  # type: ignore[index]
    assert recovered.events_after(operation_id, 0)[-1]["event_type"] == (
        "operation.recovery_required"
    )
    recovered.close()


def test_operation_cancellation_propagates_to_active_tool_runs(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "agent.db")
    operation_id, _ = journal.create_operation(
        session_id="session",
        timeline_id="timeline",
        kind="turn",
        client_request_id="request-1",
    )
    journal.set_status(operation_id, "running")
    cancelled = threading.Event()
    cancellation_reasons: list[str] = []
    cancelled_approvals: list[str] = []
    host = CodingAgentHost.__new__(CodingAgentHost)
    host.journal = journal
    host._operation_tokens = {operation_id: cancelled}
    host._runners_lock = threading.RLock()
    host._runners = {
        "session": SimpleNamespace(
            runtime=SimpleNamespace(
                cancel_active_tools=lambda *, reason: cancellation_reasons.append(reason)
            )
        )
    }
    host.approvals = SimpleNamespace(
        cancel_operation=lambda target_id: cancelled_approvals.append(target_id)
    )

    result = host.cancel_operation(operation_id)

    assert result["status"] == "cancel_requested"
    assert cancelled.is_set()
    assert cancellation_reasons == ["operation cancelled by user"]
    assert cancelled_approvals == [operation_id]
    journal.close()


def test_cancel_request_cannot_be_overwritten_by_late_completion(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "agent.db")
    operation_id, _ = journal.create_operation(
        session_id="session",
        timeline_id="timeline",
        kind="turn",
        client_request_id="request-1",
    )
    assert journal.set_status(operation_id, "running")
    assert journal.request_cancel(operation_id)

    assert not journal.set_status(operation_id, "committing")
    assert not journal.set_status(operation_id, "completed")
    assert journal.set_status(operation_id, "cancelled", "OPERATION_CANCELLED")
    assert journal.operation(operation_id)["status"] == "cancelled"  # type: ignore[index]
    journal.close()


def test_cancelled_operation_is_persisted_as_visible_turn(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    repository = SqliteCheckpointRepository(database)
    workspace = Workspace(
        repo_root=tmp_path,
        root=tmp_path,
        git_dir=tmp_path / ".git",
        data_dir=tmp_path,
    )
    session = repository.create_session(workspace, "test")
    timeline = repository.active_timeline(session.session_id)
    repository.add_turn(
        timeline_id=timeline.timeline_id,
        checkpoint_id="checkpoint-base",
        snapshot_oid="snapshot-base",
        user_text="previous",
        assistant_text="previous response",
    )
    journal = EventJournal(database)
    operation_id, _ = journal.create_operation(
        session_id=session.session_id,
        timeline_id=timeline.timeline_id,
        kind="turn",
        client_request_id="request-cancelled",
    )
    journal.append(operation_id, "assistant.delta", {"text": "partial response"})
    journal.append(operation_id, "tool.started", {"run_id": "tool-a"})
    journal.append(operation_id, "tool.started", {"run_id": "tool-b"})
    host = CodingAgentHost.__new__(CodingAgentHost)
    host.repository = repository
    host.journal = journal

    host._record_cancelled_turn(
        operation_id=operation_id,
        session_id=session.session_id,
        user_text="run two tools",
    )

    turns = repository.turns(timeline.timeline_id)
    cancelled = turns[-1]
    assert cancelled.status == "cancelled"
    assert cancelled.user_text == "run two tools"
    assert cancelled.checkpoint_id == "checkpoint-base"
    assert cancelled.snapshot_oid == "snapshot-base"
    assert "partial response" in cancelled.assistant_text
    assert "2 个后台工具" in cancelled.assistant_text
    journal.close()
    repository.close()


def test_cancelling_operation_wakes_pending_approval(tmp_path: Path) -> None:
    journal = EventJournal(tmp_path / "agent.db")
    broker = ApprovalBroker(journal)
    operation_id, _ = journal.create_operation(
        session_id="session",
        timeline_id="timeline",
        kind="turn",
        client_request_id="request-1",
    )
    cancelled = threading.Event()
    errors: list[CodingAgentError] = []

    def wait_for_approval() -> None:
        try:
            broker.request(
                operation_id,
                {"name": "activate_skill", "args": {"name": "alpha"}},
                cancelled,
            )
        except CodingAgentError as exc:
            errors.append(exc)

    worker = threading.Thread(target=wait_for_approval)
    worker.start()
    deadline = time.monotonic() + 2
    while not journal.pending_approvals():
        if time.monotonic() >= deadline:
            raise AssertionError("approval was not persisted")
        time.sleep(0.01)
    assert journal.pending_approvals()[0]["session_id"] == "session"

    cancelled.set()
    broker.cancel_operation(operation_id)
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert [error.code for error in errors] == ["OPERATION_CANCELLED"]
    assert journal.pending_approvals() == []
    journal.close()


def test_session_operation_slots_are_independent() -> None:
    host = CodingAgentHost.__new__(CodingAgentHost)
    host._operation_lock = threading.Lock()
    runners = {
        "session-a": SimpleNamespace(operation_lock=threading.Lock()),
        "session-b": SimpleNamespace(operation_lock=threading.Lock()),
    }
    host._runner = lambda session_id: runners[session_id]  # type: ignore[method-assign]

    first = host._acquire_session_operation("session-a")
    second = host._acquire_session_operation("session-b")
    try:
        assert first is runners["session-a"]
        assert second is runners["session-b"]
        try:
            host._acquire_session_operation("session-a")
        except CodingAgentError as exc:
            assert exc.code == "SESSION_BUSY"
        else:
            raise AssertionError("same-session operation was not rejected")
    finally:
        first.operation_lock.release()
        second.operation_lock.release()


def test_empty_session_context_does_not_create_baseline_turn() -> None:
    host = CodingAgentHost.__new__(CodingAgentHost)
    host.workspace = SimpleNamespace(root=Path("/tmp/workspace"))
    host.repository = SimpleNamespace(
        validate_workspace=lambda session_id, _workspace: SimpleNamespace(
            session_id=session_id
        ),
        active_timeline=lambda _session_id: SimpleNamespace(
            timeline_id="timeline",
            thread_id="thread",
            head_checkpoint_id=None,
        ),
        context_state=lambda _owner_id: None,
        turns=lambda _timeline_id: [],
    )
    host._runner = lambda _session_id: SimpleNamespace(  # type: ignore[method-assign]
        runtime=SimpleNamespace(
            accountant=SimpleNamespace(
                snapshot=lambda: {
                    "used_tokens": 0,
                    "max_tokens": 128_000,
                    "usage_ratio": 0.0,
                }
            )
        ),
        coordinator=SimpleNamespace(
            ensure_baseline=lambda: (_ for _ in ()).throw(
                AssertionError("context query must not initialize baseline")
            )
        ),
    )

    result = host.context("session")

    assert result["used_tokens"] == 0
    assert result["timeline_id"] == "timeline"
    assert result["context_owner_id"] == "main:session:timeline"


def test_session_can_be_created_while_an_existing_session_is_running() -> None:
    host = CodingAgentHost.__new__(CodingAgentHost)
    host.workspace = SimpleNamespace()
    host.config = SimpleNamespace(model="test")
    host._runners_lock = threading.RLock()
    host.session_id = "running-session"
    host.repository = SimpleNamespace(
        create_session=lambda _workspace, _model: SimpleNamespace(
            session_id="new-session",
            model_dump=lambda **_kwargs: {
                "session_id": "new-session",
                "active_timeline_id": "new-timeline",
            },
        )
    )

    result = asyncio.run(host.create_session())

    assert result == {
        "session_id": "new-session",
        "active_timeline_id": "new-timeline",
    }
    assert host.session_id == "new-session"


def test_restore_keeps_its_original_session_after_selection_changes() -> None:
    host = CodingAgentHost.__new__(CodingAgentHost)
    host.session_id = "session-b"
    host._operation_lock = threading.Lock()
    host._operation_lock.acquire()
    restored: list[int] = []
    timeline_lookups: list[str] = []
    events: list[tuple[str, dict[str, Any]]] = []
    statuses: list[str] = []
    runner = SimpleNamespace(
        coordinator=SimpleNamespace(restore=lambda turn_number: restored.append(turn_number))
    )
    host.repository = SimpleNamespace(
        active_timeline=lambda session_id: (
            timeline_lookups.append(session_id)
            or SimpleNamespace(timeline_id=f"{session_id}-restored")
        )
    )
    host.journal = SimpleNamespace(
        set_status=lambda _operation_id, status, **_kwargs: statuses.append(status) or True,
        append=lambda _operation_id, event_type, payload: events.append((event_type, payload)),
    )
    host.workspace_status = lambda: {"clean": True}  # type: ignore[method-assign]

    host._run_restore("operation-a", "session-a", runner, 3)

    assert restored == [3]
    assert timeline_lookups == ["session-a"]
    assert statuses == ["running", "completed"]
    assert ("step.completed", {"kind": "restore", "timeline_id": "session-a-restored"}) in events
    assert (
        "operation.completed",
        {"kind": "restore", "timeline_id": "session-a-restored"},
    ) in events
    assert not host._operation_lock.locked()


def test_host_uses_configured_parallel_session_capacity() -> None:
    host = CodingAgentHost.__new__(CodingAgentHost)
    host.executor = ThreadPoolExecutor(max_workers=1)
    host._executor_workers = 1
    host._event_loop = None

    def initialize() -> None:
        host.config = SimpleNamespace(max_parallel_sessions=6)
        host.repository = SimpleNamespace(sessions=lambda _workspace: [])
        host.workspace = SimpleNamespace(root=Path("/tmp/workspace"))

    host._initialize = initialize  # type: ignore[method-assign]
    host._dispatch_pending_wake = lambda _session_id: None  # type: ignore[method-assign]

    asyncio.run(host.start())
    try:
        assert host._executor_workers == 6
        assert host.executor._max_workers == 6
    finally:
        host.executor.shutdown(wait=True)


def test_api_bootstrap_cookie_csrf_and_origin() -> None:
    host = FakeHost()
    app = create_app(host)  # type: ignore[arg-type]
    with TestClient(app, base_url="http://127.0.0.1") as client:
        unauthorized = client.get("/api/v1/sessions")
        assert unauthorized.status_code == 401

        status = client.get("/api/v1/status")
        assert status.status_code == 200
        assert status.cookies.get("coding_agent_session")
        csrf_token = status.json()["csrf_token"]

        assert client.get("/api/v1/sessions").status_code == 200
        assert client.post("/api/v1/sessions").status_code == 403
        assert (
            client.post(
                "/api/v1/sessions",
                headers={"X-CSRF-Token": csrf_token},
            ).status_code
            == 201
        )
        assert (
            client.get(
                "/api/v1/sessions",
                headers={"Origin": "https://malicious.example"},
            ).status_code
            == 403
        )

        settings = client.get("/api/v1/settings")
        assert settings.status_code == 200
        assert "api_key" not in settings.json()
        updated = client.post(
            "/api/v1/settings",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "model": "next-model",
                "base_url": "https://next.example.com/v1",
                "langsmith_enabled": True,
                "langsmith_project": "next-project",
                "model_timeout_seconds": 90,
                "main_agent_model_call_limit": 30,
                "subagent_model_call_limit": 40,
                "command_timeout_seconds": 300,
                "max_parallel_sessions": 6,
            },
        )
        assert updated.status_code == 200
        assert host.updated_settings is not None
        assert host.updated_settings["model"] == "next-model"
        assert host.updated_settings["max_parallel_sessions"] == 6
        assert host.updated_settings["main_agent_model_call_limit"] == 30
        assert host.updated_settings["subagent_model_call_limit"] == 40

        assert client.get("/api/v1/sessions/session/subagent-runs").json() == {"runs": []}
        assert client.get("/api/v1/sessions/session/subagent-demo").status_code == 404
        assert (
            client.post(
                "/api/v1/sessions/session/subagent-demo",
                headers={"X-CSRF-Token": csrf_token},
            ).status_code
            == 404
        )
        cancelled = client.post(
            "/api/v1/operations/operation-1/cancel",
            headers={"X-CSRF-Token": csrf_token},
        )
        assert cancelled.json()["status"] == "cancel_requested"
        context = client.get("/api/v1/sessions/session/context")
        assert context.json()["used_tokens"] == 800
        turn_context = client.get("/api/v1/sessions/session/turns/3/context")
        assert turn_context.status_code == 200
        assert turn_context.json()["context"]["messages"][0]["content"] == "session:3"
        compact = client.post(
            "/api/v1/sessions/session/context/compact",
            headers={"X-CSRF-Token": csrf_token},
            json={
                "expected_timeline_id": "timeline",
                "client_request_id": "request-compact",
            },
        )
        assert compact.status_code == 202
        assert compact.json()["operation_id"] == "compact-operation"
