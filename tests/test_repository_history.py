import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coding_agent.errors import CodingAgentError
from coding_agent.models import Workspace
from coding_agent.repository import SqliteCheckpointRepository, new_id


def make_repository(tmp_path: Path) -> tuple[SqliteCheckpointRepository, Workspace]:
    workspace = Workspace(
        repo_root=tmp_path,
        root=tmp_path,
        git_dir=tmp_path / ".git",
        data_dir=tmp_path / ".git" / "coding-agent",
    )
    return SqliteCheckpointRepository(tmp_path / "agent.db"), workspace


def test_fork_keeps_visible_history_and_continues_numbering(tmp_path: Path) -> None:
    repository, workspace = make_repository(tmp_path)
    session = repository.create_session(workspace, "test-model")
    source = repository.active_timeline(session.session_id)
    for number in range(3):
        repository.add_turn(
            timeline_id=source.timeline_id,
            checkpoint_id=f"checkpoint-{number}",
            snapshot_oid=f"snapshot-{number}",
            user_text=f"user-{number}",
            assistant_text=f"assistant-{number}",
            turn_number=number,
        )
    source_turn = repository.get_turn(source.timeline_id, 2)
    target_timeline_id = new_id()

    repository.fork(
        session_id=session.session_id,
        source=source,
        source_turn=source_turn,
        thread_id=new_id(),
        checkpoint_id="fork-checkpoint",
        timeline_id=target_timeline_id,
    )

    assert [turn.turn_number for turn in repository.history_turns(target_timeline_id)] == [
        0,
        1,
        2,
    ]
    inherited = repository.get_turn(target_timeline_id, 1)
    assert inherited.user_text == "user-1"
    restored_summary = repository.session_summaries(
        workspace.root,
        active_session_id=session.session_id,
        limit=1,
        offset=0,
    )[0]
    assert restored_summary["turn_count"] == 3

    new_turn = repository.add_turn(
        timeline_id=target_timeline_id,
        checkpoint_id="checkpoint-3",
        snapshot_oid="snapshot-3",
        user_text="user-3",
        assistant_text="assistant-3",
    )
    assert new_turn.turn_number == 3
    assert [turn.turn_number for turn in repository.history_turns(target_timeline_id)] == [
        0,
        1,
        2,
        3,
    ]
    updated_summary = repository.session_summaries(
        workspace.root,
        active_session_id=session.session_id,
        limit=1,
        offset=0,
    )[0]
    assert updated_summary["turn_count"] == 4
    repository.close()


def test_turn_timing_is_persisted_with_millisecond_precision(tmp_path: Path) -> None:
    repository, workspace = make_repository(tmp_path)
    session = repository.create_session(workspace, "test-model")
    timeline = repository.active_timeline(session.session_id)
    turn = repository.add_turn(
        timeline_id=timeline.timeline_id,
        checkpoint_id="checkpoint",
        snapshot_oid="snapshot",
        user_text="request",
        assistant_text="response",
    )
    started_at = datetime(2026, 9, 18, 10, 0, 0, tzinfo=UTC)
    completed_at = started_at + timedelta(milliseconds=12_345)

    timed = repository.set_turn_timing(
        turn.turn_id,
        started_at=started_at,
        completed_at=completed_at,
    )

    assert timed.started_at == started_at
    assert timed.completed_at == completed_at
    assert timed.duration_ms == 12_345
    repository.close()


def test_legacy_fork_turn_numbers_are_migrated(tmp_path: Path) -> None:
    repository, workspace = make_repository(tmp_path)
    session = repository.create_session(workspace, "test-model")
    source = repository.active_timeline(session.session_id)
    repository.add_turn(
        timeline_id=source.timeline_id,
        checkpoint_id="source-checkpoint",
        snapshot_oid="source-snapshot",
        user_text="source",
        assistant_text="source",
        turn_number=4,
    )
    target_timeline_id = new_id()
    repository.fork(
        session_id=session.session_id,
        source=source,
        source_turn=repository.get_turn(source.timeline_id, 4),
        thread_id=new_id(),
        checkpoint_id="fork-checkpoint",
        timeline_id=target_timeline_id,
    )
    with repository.connection:
        repository.connection.execute(
            "UPDATE turns SET turn_number = 0 WHERE timeline_id = ?",
            (target_timeline_id,),
        )
        repository.connection.execute("DELETE FROM schema_migrations WHERE version = 1")
    repository.close()

    migrated = SqliteCheckpointRepository(tmp_path / "agent.db")
    assert [turn.turn_number for turn in migrated.turns(target_timeline_id)] == [4]
    migrated.close()


def test_existing_database_schema_can_be_opened(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY, repo_root TEXT NOT NULL,
            workspace_root TEXT NOT NULL, model TEXT NOT NULL,
            active_timeline_id TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE timelines (
            timeline_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            thread_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
            forked_from_timeline_id TEXT, forked_from_turn_number INTEGER,
            created_at TEXT NOT NULL
        );
        CREATE TABLE turns (
            turn_id TEXT PRIMARY KEY, timeline_id TEXT NOT NULL,
            turn_number INTEGER NOT NULL, checkpoint_id TEXT NOT NULL,
            snapshot_oid TEXT NOT NULL, user_text TEXT NOT NULL,
            assistant_text TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(timeline_id, turn_number)
        );
        """
    )
    connection.close()

    repository = SqliteCheckpointRepository(path)
    version = repository.connection.execute("SELECT version FROM schema_migrations").fetchone()
    assert version["version"] == 1
    turn_columns = {
        row["name"]
        for row in repository.connection.execute("PRAGMA table_info(turns)").fetchall()
    }
    assert {"started_at", "completed_at", "duration_ms"} <= turn_columns
    repository.close()


def test_session_summaries_and_turn_history_are_loaded_in_pages(tmp_path: Path) -> None:
    repository, workspace = make_repository(tmp_path)
    first = repository.create_session(workspace, "test-model")
    timeline = repository.active_timeline(first.session_id)
    for number in range(1, 5):
        repository.add_turn(
            timeline_id=timeline.timeline_id,
            checkpoint_id=f"checkpoint-{number}",
            snapshot_oid=f"snapshot-{number}",
            user_text=f"request-{number}",
            assistant_text=f"response-{number}",
            turn_number=number,
        )
    newest = repository.create_session(workspace, "test-model")

    assert repository.latest_session(workspace.root) == newest
    first_page = repository.session_summaries(
        workspace.root,
        active_session_id=first.session_id,
        limit=1,
        offset=0,
    )
    second_page = repository.session_summaries(
        workspace.root,
        active_session_id=first.session_id,
        limit=1,
        offset=1,
    )
    assert first_page[0]["session_id"] == first.session_id
    assert first_page[0]["title"] == "request-1"
    assert first_page[0]["turn_count"] == 4
    assert second_page[0]["session_id"] == newest.session_id

    recent, before = repository.history_turn_page(
        timeline.timeline_id,
        limit=2,
        before_turn_number=None,
    )
    older, final_cursor = repository.history_turn_page(
        timeline.timeline_id,
        limit=2,
        before_turn_number=before,
    )
    assert [turn.turn_number for turn in recent] == [3, 4]
    assert before == 3
    assert [turn.turn_number for turn in older] == [1, 2]
    assert final_cursor is None
    repository.close()


def test_repository_pages_can_be_read_from_web_request_thread(tmp_path: Path) -> None:
    repository, workspace = make_repository(tmp_path)
    session = repository.create_session(workspace, "test-model")
    timeline = repository.active_timeline(session.session_id)
    repository.add_turn(
        timeline_id=timeline.timeline_id,
        checkpoint_id="checkpoint",
        snapshot_oid="snapshot",
        user_text="cross-thread",
        assistant_text="ok",
        turn_number=1,
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        turns, before = executor.submit(
            repository.history_turn_page,
            timeline.timeline_id,
            limit=30,
            before_turn_number=None,
        ).result()
        summaries = executor.submit(
            repository.session_summaries,
            workspace.root,
            active_session_id=session.session_id,
            limit=30,
            offset=0,
        ).result()

    assert [turn.user_text for turn in turns] == ["cross-thread"]
    assert before is None
    assert summaries[0]["title"] == "cross-thread"


def test_context_state_round_trips_and_timeline_head_uses_compare_and_swap(
    tmp_path: Path,
) -> None:
    repository, workspace = make_repository(tmp_path)
    session = repository.create_session(workspace, "test-model")
    timeline = repository.active_timeline(session.session_id)
    repository.add_turn(
        timeline_id=timeline.timeline_id,
        checkpoint_id="checkpoint-1",
        thread_id="thread-1",
        snapshot_oid="snapshot-1",
        user_text="hello",
        assistant_text="world",
        turn_number=1,
    )
    owner_id = f"main:{session.session_id}:{timeline.timeline_id}"

    saved = repository.save_context_state(
        context_owner_id=owner_id,
        session_id=session.session_id,
        timeline_id=timeline.timeline_id,
        attempt_id=None,
        snapshot={
            "used_tokens": 800,
            "max_tokens": 1_000,
            "usage_ratio": 0.8,
            "message_count": 4,
            "compression_count": 0,
        },
    )

    assert saved.used_tokens == 800
    assert repository.context_state(owner_id) == saved
    assert repository.active_timeline(session.session_id).head_checkpoint_id == "checkpoint-1"

    repository.set_timeline_head(
        timeline.timeline_id,
        thread_id="thread-2",
        checkpoint_id="checkpoint-2",
        expected_checkpoint_id="checkpoint-1",
    )
    with pytest.raises(CodingAgentError, match="timeline head changed"):
        repository.set_timeline_head(
            timeline.timeline_id,
            thread_id="thread-3",
            checkpoint_id="checkpoint-3",
            expected_checkpoint_id="checkpoint-1",
        )
    repository.close()
    repository.close()
