import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from coding_agent.coordinator import TurnCancelled, TurnCoordinator
from coding_agent.errors import fail
from coding_agent.models import Workspace
from coding_agent.repository import SqliteCheckpointRepository
from coding_agent.tracing import MetricsLangSmithExporter, TraceStore
from coding_agent.workspace.mutation import WorkspaceMutationGate


class FakeSnapshots:
    def __init__(self) -> None:
        self.restored: list[str] = []
        self.refs: list[tuple[str, str]] = []

    def create(self, *, ref: str, parent_oid: str | None, reason: str) -> str:
        return "snapshot-new"

    def diff_stats(self, _old_oid: str, _new_oid: str) -> dict[str, int]:
        return {"changed_file_count": 0, "added_lines": 0, "deleted_lines": 0}

    def restore(self, oid: str) -> None:
        self.restored.append(oid)

    def point_ref(self, ref: str, oid: str) -> None:
        self.refs.append((ref, oid))


def build_repository(tmp_path: Path) -> tuple[SqliteCheckpointRepository, str, str]:
    repository = SqliteCheckpointRepository(tmp_path / "agent.db")
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
        user_text="",
        assistant_text="",
        turn_number=0,
    )
    return repository, session.session_id, timeline.thread_id


def test_concurrent_baseline_initialization_creates_one_turn(tmp_path: Path) -> None:
    repository = SqliteCheckpointRepository(tmp_path / "agent.db")
    workspace = Workspace(
        repo_root=tmp_path,
        root=tmp_path,
        git_dir=tmp_path / ".git",
        data_dir=tmp_path,
    )
    session = repository.create_session(workspace, "test")
    timeline = repository.active_timeline(session.session_id)

    class Runtime:
        def __init__(self) -> None:
            self.initialize_count = 0
            self._lock = threading.Lock()

        def initialize_thread(self, thread_id: str) -> str:
            assert thread_id == timeline.thread_id
            with self._lock:
                self.initialize_count += 1
            time.sleep(0.05)
            return "checkpoint-base"

        def measure_context(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "used_tokens": 0,
                "max_tokens": 1_000,
                "usage_ratio": 0.0,
            }

    runtime = Runtime()
    trace_store = TraceStore(tmp_path / "agent.db", tmp_path / "artifacts")
    exporter = MetricsLangSmithExporter(trace_store, enabled=False, project="test")
    coordinator = TurnCoordinator(
        session_id=session.session_id,
        repository=repository,
        runtime=runtime,  # type: ignore[arg-type]
        snapshots=FakeSnapshots(),  # type: ignore[arg-type]
        trace_store=trace_store,
        trace_exporter=exporter,
        model_id="test",
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(coordinator.ensure_baseline) for _ in range(2)]
        for future in futures:
            future.result()

    assert runtime.initialize_count == 1
    assert [turn.turn_number for turn in repository.turns(timeline.timeline_id)] == [0]
    exporter.close()
    trace_store.close()
    repository.close()


def test_successful_turn_atomically_advances_timeline_thread(tmp_path: Path) -> None:
    repository, session_id, original_thread = build_repository(tmp_path)

    class Runtime:
        def fork_checkpoint(self, **kwargs: Any) -> str:
            assert kwargs["source_thread_id"] == original_thread
            assert kwargs["source_checkpoint_id"] == "checkpoint-base"
            self.execution_thread = kwargs["target_thread_id"]
            return "checkpoint-fork"

        def run_turn(self, **kwargs: Any) -> tuple[str, str]:
            assert kwargs["thread_id"] == self.execution_thread
            return "done", "checkpoint-new"

        def measure_context(self, **kwargs: Any) -> dict[str, Any]:
            assert kwargs["thread_id"] == self.execution_thread
            assert kwargs["checkpoint_id"] == "checkpoint-new"
            return {
                "used_tokens": 120,
                "max_tokens": 1_000,
                "usage_ratio": 0.12,
            }

    trace_store = TraceStore(tmp_path / "agent.db", tmp_path / "artifacts")
    exporter = MetricsLangSmithExporter(trace_store, enabled=False, project="test")
    coordinator = TurnCoordinator(
        session_id=session_id,
        repository=repository,
        runtime=Runtime(),  # type: ignore[arg-type]
        snapshots=FakeSnapshots(),  # type: ignore[arg-type]
        trace_store=trace_store,
        trace_exporter=exporter,
        model_id="test",
    )

    assert coordinator.run_turn("hello", lambda _request: True) == "done"
    timeline = repository.active_timeline(session_id)
    assert timeline.thread_id != original_thread
    assert repository.turns(timeline.timeline_id)[-1].checkpoint_id == "checkpoint-new"
    exporter.close()
    trace_store.close()
    repository.close()


def test_context_persistence_failure_does_not_partially_commit_turn(
    tmp_path: Path,
) -> None:
    repository, session_id, _original_thread = build_repository(tmp_path)

    class Runtime:
        def fork_checkpoint(self, **kwargs: Any) -> str:
            self.execution_thread = kwargs["target_thread_id"]
            return "checkpoint-fork"

        def run_turn(self, **_kwargs: Any) -> tuple[str, str]:
            return "done", "checkpoint-new"

        def measure_context(self, **_kwargs: Any) -> dict[str, Any]:
            raise OSError("context measurement failed")

    trace_store = TraceStore(tmp_path / "agent.db", tmp_path / "artifacts")
    exporter = MetricsLangSmithExporter(trace_store, enabled=False, project="test")
    coordinator = TurnCoordinator(
        session_id=session_id,
        repository=repository,
        runtime=Runtime(),  # type: ignore[arg-type]
        snapshots=FakeSnapshots(),  # type: ignore[arg-type]
        trace_store=trace_store,
        trace_exporter=exporter,
        model_id="test",
    )

    with pytest.raises(OSError, match="context measurement failed"):
        coordinator.run_turn("hello", lambda _request: True)

    timeline = repository.active_timeline(session_id)
    assert timeline.thread_id == _original_thread
    assert len(repository.turns(timeline.timeline_id)) == 1
    exporter.close()
    trace_store.close()
    repository.close()


def test_failed_turn_keeps_committed_checkpoint_and_restores_owned_workspace(
    tmp_path: Path,
) -> None:
    repository, session_id, original_thread = build_repository(tmp_path)
    gate = WorkspaceMutationGate()

    class Runtime:
        def fork_checkpoint(self, **kwargs: Any) -> str:
            self.execution_thread = kwargs["target_thread_id"]
            return "checkpoint-fork"

        def run_turn(self, **kwargs: Any) -> tuple[str, str]:
            gate.acquire(kwargs["thread_id"])
            raise fail("OPERATION_CANCELLED", "cancelled")

        def annotate_cancelled_context(self, *, thread_id: str) -> str:
            assert thread_id == self.execution_thread
            return "checkpoint-cancelled"

    snapshots = FakeSnapshots()
    trace_store = TraceStore(tmp_path / "agent.db", tmp_path / "artifacts")
    exporter = MetricsLangSmithExporter(trace_store, enabled=False, project="test")
    coordinator = TurnCoordinator(
        session_id=session_id,
        repository=repository,
        runtime=Runtime(),  # type: ignore[arg-type]
        snapshots=snapshots,  # type: ignore[arg-type]
        trace_store=trace_store,
        trace_exporter=exporter,
        model_id="test",
        mutation_gate=gate,
    )

    with pytest.raises(TurnCancelled, match="cancelled") as exc_info:
        coordinator.run_turn("hello", lambda _request: True)

    tc = exc_info.value
    assert tc.execution_thread_id != original_thread
    assert tc.checkpoint_id == "checkpoint-cancelled"

    timeline = repository.active_timeline(session_id)
    assert timeline.thread_id == original_thread
    assert len(repository.turns(timeline.timeline_id)) == 1
    assert snapshots.restored == ["snapshot-base"]
    trace = trace_store.query_one("SELECT status FROM traces ORDER BY started_at DESC LIMIT 1")
    assert trace is not None
    assert trace["status"] == "cancelled"
    exporter.close()
    trace_store.close()
    repository.close()
