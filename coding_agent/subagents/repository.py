"""SQLite persistence for the subagent MVP."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from coding_agent.models import utc_now
from coding_agent.repository import new_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subagent_demo_runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    status TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    created_at TEXT NOT NULL,
    ended_at TEXT,
    turn_id TEXT,
    expected_task_count INTEGER NOT NULL DEFAULT 2
);
CREATE TABLE IF NOT EXISTS subagent_tasks (
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    name TEXT NOT NULL,
    objective TEXT NOT NULL,
    output_path TEXT NOT NULL,
    status TEXT NOT NULL,
    active_attempt_id TEXT,
    accepted_attempt_id TEXT,
    feedback TEXT,
    integration_commit TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    scope_json TEXT NOT NULL DEFAULT '[]',
    acceptance_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS subagent_attempts (
    attempt_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    result_commit TEXT,
    worktree_path TEXT,
    allowed_tools_json TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    error TEXT,
    result_envelope_json TEXT,
    workspace_mode TEXT NOT NULL DEFAULT 'required',
    workspace_state TEXT NOT NULL DEFAULT 'unallocated',
    thread_id TEXT,
    checkpoint_id TEXT,
    context_snapshot_json TEXT,
    UNIQUE(task_id, attempt_number)
);
CREATE TABLE IF NOT EXISTS subagent_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt_id TEXT,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS subagent_events_run_id
ON subagent_events(run_id, event_id);
CREATE TABLE IF NOT EXISTS agent_wake_requests (
    wake_id TEXT PRIMARY KEY,
    parent_session_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt_id TEXT,
    event_id INTEGER NOT NULL,
    wake_type TEXT NOT NULL,
    priority INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS agent_wake_requests_session
ON agent_wake_requests(parent_session_id, status, priority, created_at);
"""

_ACTIVE_RUN_STATUSES = ("queued", "running")


class _TransitionConflict(RuntimeError):
    pass


class SubagentRepository:
    """Thread-safe repository for demo runs, tasks, attempts, and events."""

    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(_SCHEMA)
        self._migrate_schema()
        self._lock = threading.RLock()
        self._recover_interrupted_state()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def _recover_interrupted_state(self) -> None:
        """Preserve reviewable work and fail only execution that cannot be resumed."""
        now = utc_now().isoformat()
        with self._lock, self.connection:
            self.connection.execute(
                """
                UPDATE agent_wake_requests
                SET status = 'completed', completed_at = ?
                WHERE status = 'claimed'
                  AND task_id IN (
                      SELECT task_id FROM subagent_tasks
                      WHERE status IN ('merged', 'failed', 'cancelled')
                  )
                """,
                (now,),
            )
            self.connection.execute(
                """
                UPDATE agent_wake_requests
                SET status = 'pending', claimed_at = NULL
                WHERE status = 'claimed'
                """,
            )
            self.connection.execute(
                """
                UPDATE subagent_tasks
                SET status = 'failed', updated_at = ?
                WHERE status IN ('queued', 'running', 'cancelling')
                """,
                (now,),
            )
            self.connection.execute(
                """
                UPDATE subagent_tasks
                SET status = 'failed', updated_at = ?
                WHERE status = 'revision_required'
                """,
                (now,),
            )
            self.connection.execute(
                """
                UPDATE subagent_attempts
                SET status = 'failed', ended_at = ?, error = 'Host restarted during execution'
                WHERE status IN ('queued', 'running')
                """,
                (now,),
            )
            self.connection.execute(
                """
                UPDATE subagent_demo_runs
                SET status = 'failed', ended_at = ?
                WHERE status IN ('queued', 'running')
                  AND EXISTS (
                      SELECT 1 FROM subagent_tasks
                      WHERE subagent_tasks.run_id = subagent_demo_runs.run_id
                        AND subagent_tasks.status = 'failed'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM subagent_tasks
                      WHERE subagent_tasks.run_id = subagent_demo_runs.run_id
                        AND subagent_tasks.status IN (
                            'awaiting_review', 'revision_required',
                            'waiting_parent', 'waiting_capability'
                        )
                  )
                """,
                (now,),
            )
            self.connection.execute(
                """
                UPDATE subagent_demo_runs
                SET status = 'cancelled', ended_at = ?
                WHERE status IN ('queued', 'running')
                  AND (
                      SELECT COUNT(*) FROM subagent_tasks
                      WHERE subagent_tasks.run_id = subagent_demo_runs.run_id
                  ) = expected_task_count
                  AND NOT EXISTS (
                      SELECT 1 FROM subagent_tasks
                      WHERE subagent_tasks.run_id = subagent_demo_runs.run_id
                        AND subagent_tasks.status NOT IN ('merged', 'cancelled')
                  )
                  AND EXISTS (
                      SELECT 1 FROM subagent_tasks
                      WHERE subagent_tasks.run_id = subagent_demo_runs.run_id
                        AND subagent_tasks.status = 'cancelled'
                  )
                """,
                (now,),
            )
            self.connection.execute(
                """
                UPDATE subagent_demo_runs
                SET status = 'completed', ended_at = ?
                WHERE status IN ('queued', 'running')
                  AND (
                      SELECT COUNT(*) FROM subagent_tasks
                      WHERE subagent_tasks.run_id = subagent_demo_runs.run_id
                  ) = expected_task_count
                  AND NOT EXISTS (
                      SELECT 1 FROM subagent_tasks
                      WHERE subagent_tasks.run_id = subagent_demo_runs.run_id
                        AND subagent_tasks.status != 'merged'
                  )
                """,
                (now,),
            )
            resumable = self.connection.execute(
                """
                SELECT task_id, run_id, session_id, active_attempt_id, status
                FROM subagent_tasks
                WHERE status IN (
                    'awaiting_review', 'waiting_parent', 'waiting_capability'
                )
                  AND NOT EXISTS (
                      SELECT 1 FROM agent_wake_requests
                      WHERE agent_wake_requests.task_id = subagent_tasks.task_id
                        AND agent_wake_requests.status IN ('pending', 'claimed')
                  )
                """
            ).fetchall()
            for task in resumable:
                payload = {
                    "status": str(task["status"]),
                    "reason": "Host restarted before the task was resolved.",
                }
                cursor = self.connection.execute(
                    """
                    INSERT INTO subagent_events (
                        run_id, task_id, attempt_id, event_type, payload_json, created_at
                    ) VALUES (?, ?, ?, 'task.recovered', ?, ?)
                    """,
                    (
                        task["run_id"],
                        task["task_id"],
                        task["active_attempt_id"],
                        json.dumps(payload),
                        now,
                    ),
                )
                event_id = cursor.lastrowid
                if event_id is None:
                    continue
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO agent_wake_requests (
                        wake_id, parent_session_id, task_id, attempt_id, event_id,
                        wake_type, priority, payload_json, status, dedupe_key,
                        created_at, claimed_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, 'task.recovered', 5, ?, 'pending', ?, ?, NULL, NULL)
                    """,
                    (
                        new_id(),
                        task["session_id"],
                        task["task_id"],
                        task["active_attempt_id"],
                        event_id,
                        json.dumps(payload),
                        f"recovery:{task['task_id']}:{task['active_attempt_id']}",
                        now,
                    ),
                )

    def active_run(self, session_id: str) -> dict[str, Any] | None:
        placeholders = ", ".join("?" for _ in _ACTIVE_RUN_STATUSES)
        with self._lock:
            row = self.connection.execute(
                f"""
                SELECT * FROM subagent_demo_runs
                WHERE session_id = ? AND status IN ({placeholders})
                ORDER BY created_at DESC LIMIT 1
                """,
                (session_id, *_ACTIVE_RUN_STATUSES),
            ).fetchone()
        return dict(row) if row is not None else None

    def _migrate_schema(self) -> None:
        run_columns = {
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA table_info(subagent_demo_runs)"
            ).fetchall()
        }
        task_columns = {
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA table_info(subagent_tasks)"
            ).fetchall()
        }
        attempt_columns = {
            str(row["name"])
            for row in self.connection.execute(
                "PRAGMA table_info(subagent_attempts)"
            ).fetchall()
        }
        with self.connection:
            if "turn_id" not in run_columns:
                self.connection.execute(
                    "ALTER TABLE subagent_demo_runs ADD COLUMN turn_id TEXT"
                )
            if "expected_task_count" not in run_columns:
                self.connection.execute(
                    "ALTER TABLE subagent_demo_runs ADD COLUMN "
                    "expected_task_count INTEGER NOT NULL DEFAULT 2"
                )
            if "scope_json" not in task_columns:
                self.connection.execute(
                    "ALTER TABLE subagent_tasks ADD COLUMN "
                    "scope_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "acceptance_json" not in task_columns:
                self.connection.execute(
                    "ALTER TABLE subagent_tasks ADD COLUMN "
                    "acceptance_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "result_envelope_json" not in attempt_columns:
                self.connection.execute(
                    "ALTER TABLE subagent_attempts ADD COLUMN result_envelope_json TEXT"
                )
            if "workspace_mode" not in attempt_columns:
                self.connection.execute(
                    "ALTER TABLE subagent_attempts ADD COLUMN "
                    "workspace_mode TEXT NOT NULL DEFAULT 'required'"
                )
            if "workspace_state" not in attempt_columns:
                self.connection.execute(
                    "ALTER TABLE subagent_attempts ADD COLUMN "
                    "workspace_state TEXT NOT NULL DEFAULT 'unallocated'"
                )
            if "thread_id" not in attempt_columns:
                self.connection.execute(
                    "ALTER TABLE subagent_attempts ADD COLUMN thread_id TEXT"
                )
            if "checkpoint_id" not in attempt_columns:
                self.connection.execute(
                    "ALTER TABLE subagent_attempts ADD COLUMN checkpoint_id TEXT"
                )
            if "context_snapshot_json" not in attempt_columns:
                self.connection.execute(
                    "ALTER TABLE subagent_attempts ADD COLUMN context_snapshot_json TEXT"
                )

    def create_run(
        self,
        session_id: str,
        base_commit: str,
        *,
        turn_id: str | None = None,
        expected_task_count: int = 2,
    ) -> str:
        run_id = new_id()
        now = utc_now().isoformat()
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO subagent_demo_runs (
                    run_id, session_id, status, base_commit, created_at,
                    ended_at, turn_id, expected_task_count
                ) VALUES (?, ?, 'queued', ?, ?, NULL, ?, ?)
                """,
                (
                    run_id,
                    session_id,
                    base_commit,
                    now,
                    turn_id,
                    expected_task_count,
                ),
            )
        return run_id

    def set_run_status(
        self,
        run_id: str,
        status: str,
        *,
        expected: tuple[str, ...] | None = None,
    ) -> bool:
        terminal = status in {"completed", "failed", "cancelled"}
        condition = ""
        parameters: list[Any] = [
            status,
            utc_now().isoformat() if terminal else None,
            run_id,
        ]
        if expected:
            placeholders = ", ".join("?" for _ in expected)
            condition = f" AND status IN ({placeholders})"
            parameters.extend(expected)
        with self._lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE subagent_demo_runs SET status = ?, ended_at = ? "
                f"WHERE run_id = ?{condition}",
                parameters,
            )
            return cursor.rowcount == 1

    def create_task(
        self,
        *,
        run_id: str,
        session_id: str,
        name: str,
        objective: str,
        output_path: str,
        scope: list[str] | None = None,
        acceptance: list[str] | None = None,
    ) -> str:
        task_id = new_id()
        now = utc_now().isoformat()
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO subagent_tasks (
                    task_id, run_id, session_id, name, objective, output_path,
                    status, active_attempt_id, accepted_attempt_id, feedback,
                    integration_commit, created_at, updated_at, scope_json,
                    acceptance_json
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, 'queued', NULL, NULL, NULL, NULL,
                    ?, ?, ?, ?
                )
                """,
                (
                    task_id,
                    run_id,
                    session_id,
                    name,
                    objective,
                    output_path,
                    now,
                    now,
                    json.dumps(scope or [], ensure_ascii=False),
                    json.dumps(acceptance or [], ensure_ascii=False),
                ),
            )
        return task_id

    def create_attempt(
        self,
        *,
        task_id: str,
        attempt_number: int,
        base_commit: str,
        allowed_tools: tuple[str, ...],
        workspace_mode: str = "required",
    ) -> str:
        attempt_id = new_id()
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO subagent_attempts (
                    attempt_id, task_id, attempt_number, status, base_commit,
                    result_commit, worktree_path, allowed_tools_json,
                    started_at, ended_at, error, result_envelope_json,
                    workspace_mode, workspace_state, thread_id, checkpoint_id
                ) VALUES (
                    ?, ?, ?, 'queued', ?, NULL, NULL, ?, NULL, NULL, NULL, NULL,
                    ?, 'unallocated', NULL, NULL
                )
                """,
                (
                    attempt_id,
                    task_id,
                    attempt_number,
                    base_commit,
                    json.dumps(allowed_tools),
                    workspace_mode,
                ),
            )
            self.connection.execute(
                """
                UPDATE subagent_tasks
                SET active_attempt_id = ?, status = 'running', updated_at = ?
                WHERE task_id = ?
                """,
                (attempt_id, utc_now().isoformat(), task_id),
            )
        return attempt_id

    def create_revision_attempt(
        self,
        *,
        task_id: str,
        previous_attempt_id: str,
        attempt_number: int,
        base_commit: str,
        allowed_tools: tuple[str, ...],
        workspace_mode: str,
        feedback: str,
    ) -> str | None:
        """Atomically replace one reviewable attempt with its revision."""
        attempt_id = new_id()
        now = utc_now().isoformat()
        try:
            with self._lock, self.connection:
                previous = self.connection.execute(
                    """
                    UPDATE subagent_attempts
                    SET status = 'rejected'
                    WHERE attempt_id = ? AND task_id = ? AND status = 'completed'
                    """,
                    (previous_attempt_id, task_id),
                )
                if previous.rowcount != 1:
                    raise _TransitionConflict
                self.connection.execute(
                    """
                    INSERT INTO subagent_attempts (
                        attempt_id, task_id, attempt_number, status, base_commit,
                        result_commit, worktree_path, allowed_tools_json,
                        started_at, ended_at, error, result_envelope_json,
                        workspace_mode, workspace_state, thread_id, checkpoint_id
                    ) VALUES (
                        ?, ?, ?, 'queued', ?, NULL, NULL, ?, NULL, NULL, NULL, NULL,
                        ?, 'unallocated', NULL, NULL
                    )
                    """,
                    (
                        attempt_id,
                        task_id,
                        attempt_number,
                        base_commit,
                        json.dumps(allowed_tools),
                        workspace_mode,
                    ),
                )
                task = self.connection.execute(
                    """
                    UPDATE subagent_tasks
                    SET active_attempt_id = ?, status = 'running', feedback = ?,
                        updated_at = ?
                    WHERE task_id = ? AND active_attempt_id = ?
                      AND status = 'awaiting_review'
                    """,
                    (attempt_id, feedback, now, task_id, previous_attempt_id),
                )
                if task.rowcount != 1:
                    raise _TransitionConflict
        except _TransitionConflict:
            return None
        return attempt_id

    def pause_for_parent(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt_id: str,
        status: str,
        event_type: str,
        payload: dict[str, Any],
        wake_type: str,
        wake_priority: int,
    ) -> int | None:
        """Atomically publish a parent request after the child turn has stopped."""
        now = utc_now().isoformat()
        try:
            with self._lock, self.connection:
                attempt = self.connection.execute(
                    """
                    UPDATE subagent_attempts SET status = ?
                    WHERE attempt_id = ? AND task_id = ? AND status = 'running'
                    """,
                    (status, attempt_id, task_id),
                )
                if attempt.rowcount != 1:
                    raise _TransitionConflict
                task = self.connection.execute(
                    """
                    UPDATE subagent_tasks SET status = ?, updated_at = ?
                    WHERE task_id = ? AND run_id = ? AND active_attempt_id = ?
                      AND status = 'running'
                    """,
                    (status, now, task_id, run_id, attempt_id),
                )
                if task.rowcount != 1:
                    raise _TransitionConflict
                cursor = self.connection.execute(
                    """
                    INSERT INTO subagent_events (
                        run_id, task_id, attempt_id, event_type, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        task_id,
                        attempt_id,
                        event_type,
                        json.dumps(payload, ensure_ascii=False),
                        now,
                    ),
                )
                event_id = cursor.lastrowid
                if event_id is None:
                    raise RuntimeError("SQLite did not return an event ID.")
                session = self.connection.execute(
                    "SELECT session_id FROM subagent_tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if session is None:
                    raise KeyError(task_id)
                self.connection.execute(
                    """
                    INSERT INTO agent_wake_requests (
                        wake_id, parent_session_id, task_id, attempt_id, event_id,
                        wake_type, priority, payload_json, status, dedupe_key,
                        created_at, claimed_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL)
                    """,
                    (
                        new_id(),
                        str(session["session_id"]),
                        task_id,
                        attempt_id,
                        event_id,
                        wake_type,
                        wake_priority,
                        json.dumps(payload, ensure_ascii=False),
                        f"{session['session_id']}:{event_id}:{wake_type}",
                        now,
                    ),
                )
                return int(event_id)
        except _TransitionConflict:
            return None

    def integrating_tasks(self) -> list[dict[str, Any]]:
        """Return integrations whose filesystem/SQLite commit needs reconciliation."""
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT t.*, a.base_commit, a.result_commit, a.status AS attempt_status
                FROM subagent_tasks t
                JOIN subagent_attempts a ON a.attempt_id = t.active_attempt_id
                WHERE t.status = 'integrating'
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def complete_integration(
        self,
        task_id: str,
        attempt_id: str,
        integration_commit: str,
    ) -> bool:
        """Atomically persist a completed filesystem integration."""
        now = utc_now().isoformat()
        try:
            with self._lock, self.connection:
                attempt = self.connection.execute(
                    """
                    UPDATE subagent_attempts SET status = 'accepted', ended_at = ?
                    WHERE attempt_id = ? AND task_id = ?
                      AND status IN ('completed', 'accepted')
                    """,
                    (now, attempt_id, task_id),
                )
                if attempt.rowcount != 1:
                    raise _TransitionConflict
                task = self.connection.execute(
                    """
                    UPDATE subagent_tasks
                    SET status = 'merged', accepted_attempt_id = ?,
                        integration_commit = ?, updated_at = ?
                    WHERE task_id = ? AND active_attempt_id = ?
                      AND status = 'integrating'
                    """,
                    (attempt_id, integration_commit, now, task_id, attempt_id),
                )
                if task.rowcount != 1:
                    raise _TransitionConflict
        except _TransitionConflict:
            return False
        return True

    def reset_integration(self, task_id: str, attempt_id: str) -> bool:
        """Return an unapplied interrupted integration to review."""
        return self.transition_task(
            task_id,
            ("integrating",),
            status="awaiting_review",
            active_attempt_id=attempt_id,
        )

    def update_attempt(self, attempt_id: str, **values: Any) -> None:
        if not values:
            return
        allowed = {
            "status",
            "result_commit",
            "worktree_path",
            "started_at",
            "ended_at",
            "error",
            "result_envelope_json",
            "workspace_state",
            "allowed_tools_json",
            "thread_id",
            "checkpoint_id",
            "context_snapshot_json",
        }
        if unknown := set(values) - allowed:
            raise ValueError(f"Unknown attempt fields: {sorted(unknown)}")
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._lock, self.connection:
            self.connection.execute(
                f"UPDATE subagent_attempts SET {assignments} WHERE attempt_id = ?",
                (*values.values(), attempt_id),
            )

    def transition_attempt(
        self,
        attempt_id: str,
        expected: tuple[str, ...],
        **values: Any,
    ) -> bool:
        """Apply an attempt update only while it is still in an expected state."""
        if not values:
            return False
        allowed = {
            "status",
            "result_commit",
            "worktree_path",
            "started_at",
            "ended_at",
            "error",
            "result_envelope_json",
            "workspace_state",
            "allowed_tools_json",
            "thread_id",
            "checkpoint_id",
        }
        if unknown := set(values) - allowed:
            raise ValueError(f"Unknown attempt fields: {sorted(unknown)}")
        assignments = ", ".join(f"{key} = ?" for key in values)
        placeholders = ", ".join("?" for _ in expected)
        with self._lock, self.connection:
            cursor = self.connection.execute(
                f"UPDATE subagent_attempts SET {assignments} "
                f"WHERE attempt_id = ? AND status IN ({placeholders})",
                (*values.values(), attempt_id, *expected),
            )
            return cursor.rowcount == 1

    def update_task(self, task_id: str, **values: Any) -> None:
        if not values:
            return
        allowed = {
            "status",
            "active_attempt_id",
            "accepted_attempt_id",
            "feedback",
            "integration_commit",
        }
        if unknown := set(values) - allowed:
            raise ValueError(f"Unknown task fields: {sorted(unknown)}")
        values["updated_at"] = utc_now().isoformat()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._lock, self.connection:
            self.connection.execute(
                f"UPDATE subagent_tasks SET {assignments} WHERE task_id = ?",
                (*values.values(), task_id),
            )

    def transition_task(
        self,
        task_id: str,
        expected: tuple[str, ...],
        **values: Any,
    ) -> bool:
        """Apply a task update only while it is still in an expected state."""
        if not values:
            return False
        allowed = {
            "status",
            "active_attempt_id",
            "accepted_attempt_id",
            "feedback",
            "integration_commit",
        }
        if unknown := set(values) - allowed:
            raise ValueError(f"Unknown task fields: {sorted(unknown)}")
        values["updated_at"] = utc_now().isoformat()
        assignments = ", ".join(f"{key} = ?" for key in values)
        placeholders = ", ".join("?" for _ in expected)
        with self._lock, self.connection:
            cursor = self.connection.execute(
                f"UPDATE subagent_tasks SET {assignments} "
                f"WHERE task_id = ? AND status IN ({placeholders})",
                (*values.values(), task_id, *expected),
            )
            return cursor.rowcount == 1

    def resumable_tasks(self) -> list[dict[str, Any]]:
        """Return persisted tasks whose contracts are needed after restart."""
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT t.*, a.allowed_tools_json, a.workspace_mode
                FROM subagent_tasks t
                JOIN subagent_attempts a ON a.attempt_id = t.active_attempt_id
                WHERE t.status IN (
                    'awaiting_review', 'revision_required',
                    'waiting_parent', 'waiting_capability'
                )
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def append_event(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt_id: str | None,
        event_type: str,
        payload: dict[str, Any],
        wake_type: str | None = None,
        wake_priority: int = 100,
    ) -> int:
        with self._lock, self.connection:
            cursor = self.connection.execute(
                """
                INSERT INTO subagent_events (
                    run_id, task_id, attempt_id, event_type, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    task_id,
                    attempt_id,
                    event_type,
                    json.dumps(payload, ensure_ascii=False),
                    utc_now().isoformat(),
                ),
            )
            event_id = cursor.lastrowid
            if event_id is None:
                raise RuntimeError("SQLite did not return an event ID.")
            if wake_type is not None:
                session = self.connection.execute(
                    "SELECT session_id FROM subagent_tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if session is None:
                    raise KeyError(task_id)
                wake_id = new_id()
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO agent_wake_requests (
                        wake_id, parent_session_id, task_id, attempt_id, event_id,
                        wake_type, priority, payload_json, status, dedupe_key,
                        created_at, claimed_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, NULL)
                    """,
                    (
                        wake_id,
                        str(session["session_id"]),
                        task_id,
                        attempt_id,
                        event_id,
                        wake_type,
                        wake_priority,
                        json.dumps(payload, ensure_ascii=False),
                        f"{session['session_id']}:{event_id}:{wake_type}",
                        utc_now().isoformat(),
                    ),
                )
            return event_id

    def pending_wakes(self, session_id: str) -> list[dict[str, Any]]:
        """Return durable main-Agent wake requests in scheduler order."""
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM agent_wake_requests
                WHERE parent_session_id = ? AND status = 'pending'
                ORDER BY priority, created_at
                """,
                (session_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def claim_next_wake(self, session_id: str) -> dict[str, Any] | None:
        """Claim one pending wake for the session's single main-Agent consumer."""
        with self._lock, self.connection:
            row = self.connection.execute(
                """
                SELECT * FROM agent_wake_requests
                WHERE parent_session_id = ? AND status = 'pending'
                ORDER BY priority, created_at
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            claimed_at = utc_now().isoformat()
            cursor = self.connection.execute(
                """
                UPDATE agent_wake_requests
                SET status = 'claimed', claimed_at = ?
                WHERE wake_id = ? AND status = 'pending'
                """,
                (claimed_at, row["wake_id"]),
            )
            if cursor.rowcount != 1:
                return None
        result = dict(row)
        result["status"] = "claimed"
        result["claimed_at"] = claimed_at
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def complete_wake(self, wake_id: str) -> None:
        """Mark a claimed wake as consumed after its parent operation settles."""
        with self._lock, self.connection:
            self.connection.execute(
                """
                UPDATE agent_wake_requests
                SET status = 'completed', completed_at = ?
                WHERE wake_id = ? AND status = 'claimed'
                """,
                (utc_now().isoformat(), wake_id),
            )

    def release_wake(self, wake_id: str) -> None:
        """Return an unhandled claimed wake to the durable queue."""
        with self._lock, self.connection:
            self.connection.execute(
                """
                UPDATE agent_wake_requests
                SET status = 'pending', claimed_at = NULL
                WHERE wake_id = ? AND status = 'claimed'
                """,
                (wake_id,),
            )

    def complete_task_wakes(self, task_id: str) -> None:
        """Acknowledge pending or claimed wakes after an explicit parent decision."""
        with self._lock, self.connection:
            self.connection.execute(
                """
                UPDATE agent_wake_requests
                SET status = 'completed', completed_at = ?
                WHERE task_id = ? AND status IN ('pending', 'claimed')
                """,
                (utc_now().isoformat(), task_id),
            )

    def task(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM subagent_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return dict(row)

    def attempt(self, attempt_id: str) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM subagent_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return dict(row)

    def latest_run(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT run_id FROM subagent_demo_runs
                WHERE session_id = ? ORDER BY created_at DESC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return self.run(str(row["run_id"])) if row is not None else None

    def runs(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT run_id FROM subagent_demo_runs
                WHERE session_id = ? ORDER BY created_at
                """,
                (session_id,),
            ).fetchall()
        return [self.run(str(row["run_id"])) for row in rows]

    def run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            run_row = self.connection.execute(
                "SELECT * FROM subagent_demo_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise KeyError(run_id)
            task_rows = self.connection.execute(
                "SELECT * FROM subagent_tasks WHERE run_id = ? ORDER BY created_at, name",
                (run_id,),
            ).fetchall()
            attempt_rows = self.connection.execute(
                """
                SELECT a.* FROM subagent_attempts a
                JOIN subagent_tasks t ON t.task_id = a.task_id
                WHERE t.run_id = ? ORDER BY a.task_id, a.attempt_number
                """,
                (run_id,),
            ).fetchall()
            event_rows = self.connection.execute(
                """
                SELECT * FROM subagent_events
                WHERE run_id = ? ORDER BY event_id
                """,
                (run_id,),
            ).fetchall()

        attempts_by_task: dict[str, list[dict[str, Any]]] = {}
        for row in attempt_rows:
            item = dict(row)
            item["allowed_tools"] = json.loads(item.pop("allowed_tools_json"))
            envelope = item.pop("result_envelope_json", None)
            item["result"] = json.loads(envelope) if envelope else None
            context_snapshot = item.pop("context_snapshot_json", None)
            item["context_usage"] = (
                json.loads(context_snapshot) if context_snapshot else None
            )
            attempts_by_task.setdefault(str(item["task_id"]), []).append(item)
        events_by_task: dict[str, list[dict[str, Any]]] = {}
        for row in event_rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            events_by_task.setdefault(str(item["task_id"]), []).append(item)
        tasks = []
        for row in task_rows:
            item = dict(row)
            task_id = str(item["task_id"])
            item["scope"] = json.loads(item.pop("scope_json"))
            item["acceptance"] = json.loads(item.pop("acceptance_json"))
            item["attempts"] = attempts_by_task.get(task_id, [])
            item["events"] = events_by_task.get(task_id, [])
            tasks.append(item)
        result = dict(run_row)
        result["tasks"] = tasks
        return result
