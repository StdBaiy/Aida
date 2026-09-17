"""SQLite metadata repository."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from coding_agent.errors import fail
from coding_agent.models import (
    AgentContextState,
    SessionRecord,
    TimelineRecord,
    TimelineStatus,
    TurnRecord,
    Workspace,
    utc_now,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    repo_root TEXT NOT NULL,
    workspace_root TEXT NOT NULL,
    model TEXT NOT NULL,
    active_timeline_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '新任务',
    turn_count INTEGER NOT NULL DEFAULT 0,
    summary_ready INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS timelines (
    timeline_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    thread_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    head_checkpoint_id TEXT,
    forked_from_timeline_id TEXT,
    forked_from_turn_number INTEGER,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_timeline
ON timelines(session_id) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS turns (
    turn_id TEXT PRIMARY KEY,
    timeline_id TEXT NOT NULL,
    turn_number INTEGER NOT NULL,
    checkpoint_id TEXT NOT NULL,
    snapshot_oid TEXT NOT NULL,
    user_text TEXT NOT NULL,
    assistant_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'completed',
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    duration_ms INTEGER,
    UNIQUE(timeline_id, turn_number)
);
CREATE TABLE IF NOT EXISTS agent_context_states (
    context_owner_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    timeline_id TEXT,
    attempt_id TEXT,
    snapshot_json TEXT NOT NULL,
    used_tokens INTEGER NOT NULL,
    max_tokens INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_context_states_session
ON agent_context_states(session_id);
CREATE TABLE IF NOT EXISTS context_compactions (
    compaction_id TEXT PRIMARY KEY,
    context_owner_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    timeline_id TEXT NOT NULL,
    trigger TEXT NOT NULL,
    summary_id TEXT NOT NULL,
    base_checkpoint_id TEXT NOT NULL,
    result_checkpoint_id TEXT NOT NULL,
    before_tokens INTEGER NOT NULL,
    after_tokens INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_notices (
    notice_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    timeline_id TEXT NOT NULL,
    notice_kind TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def new_id() -> str:
    """Create an opaque lowercase application ID."""
    return str(uuid.uuid4())


class SqliteCheckpointRepository:
    """Persist session, timeline, and user-visible checkpoint metadata."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(_SCHEMA)
        self._migrate_fork_turn_numbers()
        self._migrate_session_summaries()
        self._migrate_turn_status()
        self._migrate_context_state()
        self._migrate_turn_timing()
        self.connection.commit()
        os.chmod(path, 0o600)

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection]:
        """Run metadata writes atomically."""
        try:
            with self._lock, self.connection:
                yield self.connection
        except sqlite3.Error as exc:
            raise fail("DATABASE_ERROR", str(exc)) from exc

    def close(self) -> None:
        """Close the database."""
        with self._lock:
            self.connection.close()

    def create_session(self, workspace: Workspace, model: str) -> SessionRecord:
        """Create a session with one active timeline."""
        session_id, timeline_id, thread_id = new_id(), new_id(), new_id()
        created_at = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    session_id, repo_root, workspace_root, model,
                    active_timeline_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    str(workspace.repo_root),
                    str(workspace.root),
                    model,
                    timeline_id,
                    created_at.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO timelines (
                    timeline_id, session_id, thread_id, status,
                    head_checkpoint_id, forked_from_timeline_id,
                    forked_from_turn_number, created_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?)
                """,
                (
                    timeline_id,
                    session_id,
                    thread_id,
                    TimelineStatus.ACTIVE,
                    created_at.isoformat(),
                ),
            )
        return self.get_session(session_id)

    def get_session(self, session_id: str) -> SessionRecord:
        """Load a session by ID."""
        row = self._query_one(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        )
        if row is None:
            raise fail("SESSION_NOT_FOUND", f"Unknown session: {session_id}")
        return SessionRecord.model_validate(dict(row))

    def sessions(self, workspace_root: Path | None = None) -> list[SessionRecord]:
        """List sessions newest first, optionally restricted to one workspace."""
        if workspace_root is None:
            rows = self._query_all(
                "SELECT * FROM sessions ORDER BY created_at DESC"
            )
        else:
            rows = self._query_all(
                "SELECT * FROM sessions WHERE workspace_root = ? ORDER BY created_at DESC",
                (str(workspace_root),),
            )
        return [SessionRecord.model_validate(dict(row)) for row in rows]

    def latest_session(self, workspace_root: Path) -> SessionRecord | None:
        """Load only the newest session for one workspace."""
        row = self._query_one(
            """
            SELECT * FROM sessions
            WHERE workspace_root = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (str(workspace_root),),
        )
        return SessionRecord.model_validate(dict(row)) if row is not None else None

    def session_summaries(
        self,
        workspace_root: Path,
        *,
        active_session_id: str,
        limit: int,
        offset: int,
    ) -> list[dict[str, object]]:
        """Load one bounded page without reading turn bodies."""
        query = """
            SELECT session_id, active_timeline_id, created_at, title, turn_count, summary_ready
            FROM sessions
            WHERE workspace_root = ?
            ORDER BY CASE WHEN session_id = ? THEN 0 ELSE 1 END, created_at DESC
            LIMIT ? OFFSET ?
        """
        parameters = (str(workspace_root), active_session_id, limit, offset)
        rows = self._query_all(
            query,
            parameters,
        )
        for row in rows:
            if not int(row["summary_ready"]):
                self._backfill_session_summary(
                    str(row["session_id"]),
                    str(row["active_timeline_id"]),
                )
        rows = self._query_all(
            """
            SELECT session_id, active_timeline_id, created_at, title, turn_count
            FROM sessions
            WHERE workspace_root = ?
            ORDER BY CASE WHEN session_id = ? THEN 0 ELSE 1 END, created_at DESC
            LIMIT ? OFFSET ?
            """,
            parameters,
        )
        return [dict(row) for row in rows]

    def active_timeline(self, session_id: str) -> TimelineRecord:
        """Load the active timeline."""
        row = self._query_one(
            "SELECT * FROM timelines WHERE session_id = ? AND status = 'active'",
            (session_id,),
        )
        if row is None:
            raise fail("TIMELINE_NOT_FOUND", "Session has no active timeline.")
        return TimelineRecord.model_validate(dict(row))

    def turns(self, timeline_id: str) -> list[TurnRecord]:
        """List committed turns in ascending order."""
        rows = self._query_all(
            "SELECT * FROM turns WHERE timeline_id = ? ORDER BY turn_number",
            (timeline_id,),
        )
        return [TurnRecord.model_validate(dict(row)) for row in rows]

    def history_turns(self, timeline_id: str) -> list[TurnRecord]:
        """List the visible history, including turns inherited through restores."""
        timeline = self._get_timeline(timeline_id)
        local_turns = self.turns(timeline_id)
        if timeline.forked_from_timeline_id is None or timeline.forked_from_turn_number is None:
            return local_turns
        inherited = [
            turn
            for turn in self.history_turns(timeline.forked_from_timeline_id)
            if turn.turn_number <= timeline.forked_from_turn_number
        ]
        own = [turn for turn in local_turns if turn.turn_number > timeline.forked_from_turn_number]
        return [*inherited, *own]

    def history_turn_page(
        self,
        timeline_id: str,
        *,
        limit: int,
        before_turn_number: int | None,
    ) -> tuple[list[TurnRecord], int | None]:
        """Load one newest-first page from a restored timeline's visible history."""
        rows = self._query_all(
            """
            WITH RECURSIVE lineage(timeline_id, max_turn) AS (
                SELECT ?, NULL
                UNION ALL
                SELECT current.forked_from_timeline_id,
                       CASE
                           WHEN lineage.max_turn IS NULL
                               THEN current.forked_from_turn_number
                           WHEN current.forked_from_turn_number < lineage.max_turn
                               THEN current.forked_from_turn_number
                           ELSE lineage.max_turn
                       END
                FROM lineage
                JOIN timelines current ON current.timeline_id = lineage.timeline_id
                WHERE current.forked_from_timeline_id IS NOT NULL
            ),
            visible AS (
                SELECT turns.*
                FROM lineage
                JOIN turns ON turns.timeline_id = lineage.timeline_id
                WHERE turns.user_text <> ''
                  AND (lineage.max_turn IS NULL OR turns.turn_number <= lineage.max_turn)
            )
            SELECT * FROM visible
            WHERE (? IS NULL OR turn_number < ?)
            ORDER BY turn_number DESC
            LIMIT ?
            """,
            (
                timeline_id,
                before_turn_number,
                before_turn_number,
                limit + 1,
            ),
        )
        has_more = len(rows) > limit
        page = rows[:limit]
        records = [TurnRecord.model_validate(dict(row)) for row in reversed(page)]
        next_before = records[0].turn_number if has_more and records else None
        return records, next_before

    def get_turn(self, timeline_id: str, turn_number: int) -> TurnRecord:
        """Load a visible turn, following restore ancestry when necessary."""
        timeline = self._get_timeline(timeline_id)
        if (
            timeline.forked_from_timeline_id is not None
            and timeline.forked_from_turn_number is not None
            and turn_number <= timeline.forked_from_turn_number
        ):
            return self.get_turn(timeline.forked_from_timeline_id, turn_number)
        row = self._query_one(
            "SELECT * FROM turns WHERE timeline_id = ? AND turn_number = ?",
            (timeline_id, turn_number),
        )
        if row is None:
            raise fail("TURN_NOT_RESTORABLE", f"Turn {turn_number} does not exist.")
        return TurnRecord.model_validate(dict(row))

    def add_turn(
        self,
        *,
        timeline_id: str,
        checkpoint_id: str,
        thread_id: str | None = None,
        snapshot_oid: str,
        user_text: str,
        assistant_text: str,
        status: str = "completed",
        turn_number: int | None = None,
        turn_id: str | None = None,
        started_at: datetime | None = None,
        context_owner_id: str | None = None,
        context_snapshot: dict[str, Any] | None = None,
    ) -> TurnRecord:
        """Commit a joint graph/filesystem checkpoint."""
        if turn_number is None:
            row = self._query_one(
                "SELECT COALESCE(MAX(turn_number), -1) + 1 AS value FROM turns "
                "WHERE timeline_id = ?",
                (timeline_id,),
            )
            if row is None:
                raise RuntimeError("Turn number query returned no row.")
            turn_number = int(row["value"])
        completed_at = utc_now()
        duration_ms = (
            max(0, round((completed_at - started_at).total_seconds() * 1000))
            if started_at is not None
            else None
        )
        record = TurnRecord(
            turn_id=turn_id or new_id(),
            timeline_id=timeline_id,
            turn_number=turn_number,
            checkpoint_id=checkpoint_id,
            snapshot_oid=snapshot_oid,
            user_text=user_text,
            assistant_text=assistant_text,
            status=status,
            created_at=completed_at,
            started_at=started_at,
            completed_at=completed_at if started_at is not None else None,
            duration_ms=duration_ms,
        )
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO turns (
                    turn_id, timeline_id, turn_number, checkpoint_id, snapshot_oid,
                    user_text, assistant_text, status, created_at,
                    started_at, completed_at, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.turn_id,
                    record.timeline_id,
                    record.turn_number,
                    record.checkpoint_id,
                    record.snapshot_oid,
                    record.user_text,
                    record.assistant_text,
                    record.status,
                    record.created_at.isoformat(),
                    record.started_at.isoformat() if record.started_at else None,
                    record.completed_at.isoformat() if record.completed_at else None,
                    record.duration_ms,
                ),
            )
            if thread_id is not None:
                cursor = connection.execute(
                    """
                    UPDATE timelines
                    SET thread_id = ?, head_checkpoint_id = ?
                    WHERE timeline_id = ?
                    """,
                    (thread_id, checkpoint_id, timeline_id),
                )
                if cursor.rowcount != 1:
                    raise fail("TIMELINE_NOT_FOUND", "Cannot commit turn to missing timeline.")
            if user_text:
                connection.execute(
                    """
                    UPDATE sessions
                    SET title = CASE WHEN turn_count = 0 THEN substr(?, 1, 72) ELSE title END,
                        turn_count = turn_count + 1,
                        summary_ready = 1
                    WHERE active_timeline_id = ?
                    """,
                    (user_text, timeline_id),
                )
            if context_owner_id is not None and context_snapshot is not None:
                self._save_context_state(
                    connection,
                    context_owner_id=context_owner_id,
                    session_id=self._get_timeline(timeline_id).session_id,
                    timeline_id=timeline_id,
                    attempt_id=None,
                    snapshot=context_snapshot,
                )
        return record

    def set_turn_timing(
        self,
        turn_id: str,
        *,
        started_at: datetime,
        completed_at: datetime,
    ) -> TurnRecord:
        """Persist the complete operation duration after turn-boundary work."""
        duration_ms = max(0, round((completed_at - started_at).total_seconds() * 1000))
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE turns
                SET started_at = ?, completed_at = ?, duration_ms = ?
                WHERE turn_id = ?
                """,
                (
                    started_at.isoformat(),
                    completed_at.isoformat(),
                    duration_ms,
                    turn_id,
                ),
            )
            if cursor.rowcount != 1:
                raise fail("TURN_NOT_FOUND", "Cannot update timing for a missing turn.")
        row = self._query_one("SELECT * FROM turns WHERE turn_id = ?", (turn_id,))
        if row is None:
            raise fail("TURN_NOT_FOUND", "Cannot load the timed turn.")
        return TurnRecord.model_validate(dict(row))

    def fork(
        self,
        *,
        session_id: str,
        source: TimelineRecord,
        source_turn: TurnRecord,
        thread_id: str,
        checkpoint_id: str,
        timeline_id: str,
    ) -> TimelineRecord:
        """Fork a source turn into a new active timeline."""
        created_at = utc_now()
        visible_turns = [
            turn
            for turn in self.history_turns(source.timeline_id)
            if turn.user_text and turn.turn_number <= source_turn.turn_number
        ]
        with self.transaction() as connection:
            connection.execute(
                "UPDATE timelines SET status = 'read_only' WHERE timeline_id = ?",
                (source.timeline_id,),
            )
            connection.execute(
                """
                INSERT INTO timelines (
                    timeline_id, session_id, thread_id, status,
                    head_checkpoint_id, forked_from_timeline_id,
                    forked_from_turn_number, created_at
                ) VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    timeline_id,
                    session_id,
                    thread_id,
                    checkpoint_id,
                    source.timeline_id,
                    source_turn.turn_number,
                    created_at.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE sessions
                SET active_timeline_id = ?, title = ?, turn_count = ?, summary_ready = 1
                WHERE session_id = ?
                """,
                (
                    timeline_id,
                    visible_turns[0].user_text[:72] if visible_turns else "新任务",
                    len(visible_turns),
                    session_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO turns (
                    turn_id, timeline_id, turn_number, checkpoint_id, snapshot_oid,
                    user_text, assistant_text, status, created_at
                ) VALUES (?, ?, ?, ?, ?, '', '', 'completed', ?)
                """,
                (
                    new_id(),
                    timeline_id,
                    source_turn.turn_number,
                    checkpoint_id,
                    source_turn.snapshot_oid,
                    created_at.isoformat(),
                ),
            )
        return self.active_timeline(session_id)

    def set_timeline_head(
        self,
        timeline_id: str,
        *,
        thread_id: str,
        checkpoint_id: str,
        expected_checkpoint_id: str | None = None,
    ) -> None:
        """Advance the execution head, optionally with compare-and-swap."""
        with self.transaction() as connection:
            if expected_checkpoint_id is None:
                cursor = connection.execute(
                    """
                    UPDATE timelines
                    SET thread_id = ?, head_checkpoint_id = ?
                    WHERE timeline_id = ?
                    """,
                    (thread_id, checkpoint_id, timeline_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE timelines
                    SET thread_id = ?, head_checkpoint_id = ?
                    WHERE timeline_id = ? AND head_checkpoint_id = ?
                    """,
                    (thread_id, checkpoint_id, timeline_id, expected_checkpoint_id),
                )
            if cursor.rowcount != 1:
                raise fail(
                    "TIMELINE_CHANGED",
                    "The timeline head changed while context state was being committed.",
                )

    def save_context_state(
        self,
        *,
        context_owner_id: str,
        session_id: str,
        timeline_id: str | None,
        attempt_id: str | None,
        snapshot: dict[str, Any],
    ) -> AgentContextState:
        """Upsert one durable Agent context snapshot."""
        updated_at = utc_now()
        with self.transaction() as connection:
            payload = self._save_context_state(
                connection,
                context_owner_id=context_owner_id,
                session_id=session_id,
                timeline_id=timeline_id,
                attempt_id=attempt_id,
                snapshot=snapshot,
                updated_at=updated_at.isoformat(),
            )
        return AgentContextState.model_validate(payload)

    @staticmethod
    def _save_context_state(
        connection: sqlite3.Connection,
        *,
        context_owner_id: str,
        session_id: str,
        timeline_id: str | None,
        attempt_id: str | None,
        snapshot: dict[str, Any],
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        timestamp = updated_at or utc_now().isoformat()
        payload = {
            **snapshot,
            "context_owner_id": context_owner_id,
            "session_id": session_id,
            "timeline_id": timeline_id,
            "attempt_id": attempt_id,
            "updated_at": timestamp,
        }
        connection.execute(
            """
            INSERT INTO agent_context_states (
                context_owner_id, session_id, timeline_id, attempt_id,
                snapshot_json, used_tokens, max_tokens, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(context_owner_id) DO UPDATE SET
                session_id = excluded.session_id,
                timeline_id = excluded.timeline_id,
                attempt_id = excluded.attempt_id,
                snapshot_json = excluded.snapshot_json,
                used_tokens = excluded.used_tokens,
                max_tokens = excluded.max_tokens,
                updated_at = excluded.updated_at
            """,
            (
                context_owner_id,
                session_id,
                timeline_id,
                attempt_id,
                json.dumps(payload, ensure_ascii=False),
                int(snapshot.get("used_tokens", 0) or 0),
                int(snapshot.get("max_tokens", 0) or 0),
                timestamp,
            ),
        )
        return payload

    def context_state(self, context_owner_id: str) -> AgentContextState | None:
        """Load the latest durable snapshot for one context owner."""
        import json

        row = self._query_one(
            "SELECT snapshot_json FROM agent_context_states WHERE context_owner_id = ?",
            (context_owner_id,),
        )
        if row is None:
            return None
        return AgentContextState.model_validate(json.loads(str(row["snapshot_json"])))

    def commit_context_compaction(
        self,
        *,
        context_owner_id: str,
        session_id: str,
        timeline_id: str,
        thread_id: str,
        base_checkpoint_id: str,
        result_checkpoint_id: str,
        trigger: str,
        summary_id: str,
        before_tokens: int,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        """Commit a compacted checkpoint, owner state, audit row, and UI notice."""
        created_at = utc_now().isoformat()
        compaction_id = new_id()
        notice_id = new_id()
        after_tokens = int(snapshot.get("used_tokens", 0) or 0)
        text = (
            f"上下文压缩完成：{before_tokens:,} -> {after_tokens:,} tokens，"
            f"已生成 {summary_id}"
        )
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE timelines
                SET thread_id = ?, head_checkpoint_id = ?
                WHERE timeline_id = ? AND head_checkpoint_id = ?
                """,
                (
                    thread_id,
                    result_checkpoint_id,
                    timeline_id,
                    base_checkpoint_id,
                ),
            )
            if cursor.rowcount != 1:
                raise fail(
                    "TIMELINE_CHANGED",
                    "The timeline changed while context compression was running.",
                )
            self._save_context_state(
                connection,
                context_owner_id=context_owner_id,
                session_id=session_id,
                timeline_id=timeline_id,
                attempt_id=None,
                snapshot={**snapshot, "last_compaction_id": compaction_id},
                updated_at=created_at,
            )
            connection.execute(
                """
                INSERT INTO context_compactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    compaction_id,
                    context_owner_id,
                    session_id,
                    timeline_id,
                    trigger,
                    summary_id,
                    base_checkpoint_id,
                    result_checkpoint_id,
                    before_tokens,
                    after_tokens,
                    created_at,
                ),
            )
            connection.execute(
                "INSERT INTO conversation_notices VALUES (?, ?, ?, ?, ?, ?)",
                (
                    notice_id,
                    session_id,
                    timeline_id,
                    "context.compression.completed",
                    text,
                    created_at,
                ),
            )
        return {
            "compaction_id": compaction_id,
            "notice_id": notice_id,
            "context_owner_id": context_owner_id,
            "trigger": trigger,
            "summary_id": summary_id,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "max_tokens": int(snapshot.get("max_tokens", 0) or 0),
            "usage_ratio": float(snapshot.get("usage_ratio", 0.0) or 0.0),
            "text": text,
            "created_at": created_at,
        }

    def conversation_notices(
        self,
        *,
        session_id: str,
        timeline_id: str,
    ) -> list[dict[str, Any]]:
        rows = self._query_all(
            """
            SELECT notice_id, notice_kind, text, created_at
            FROM conversation_notices
            WHERE session_id = ? AND timeline_id = ?
            ORDER BY created_at
            """,
            (session_id, timeline_id),
        )
        return [dict(row) for row in rows]

    def validate_workspace(self, session_id: str, workspace: Workspace) -> SessionRecord:
        """Ensure a resumed session belongs to the same resolved paths."""
        session = self.get_session(session_id)
        wrong_repo = Path(session.repo_root) != workspace.repo_root
        wrong_workspace = Path(session.workspace_root) != workspace.root
        if wrong_repo or wrong_workspace:
            raise fail("SESSION_WORKSPACE_MISMATCH", "Session belongs to another workspace.")
        return session

    def _get_timeline(self, timeline_id: str) -> TimelineRecord:
        row = self._query_one(
            "SELECT * FROM timelines WHERE timeline_id = ?", (timeline_id,)
        )
        if row is None:
            raise fail("TIMELINE_NOT_FOUND", f"Unknown timeline: {timeline_id}")
        return TimelineRecord.model_validate(dict(row))

    def _migrate_fork_turn_numbers(self) -> None:
        """Move legacy restored timelines from local numbering to visible numbering."""
        version = 1
        applied = self.connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
        ).fetchone()
        if applied is not None:
            return
        timelines = self.connection.execute(
            """
            SELECT timeline_id, forked_from_turn_number
            FROM timelines
            WHERE forked_from_turn_number IS NOT NULL
              AND forked_from_turn_number > 0
            """
        ).fetchall()
        for timeline in timelines:
            timeline_id = str(timeline["timeline_id"])
            offset = int(timeline["forked_from_turn_number"])
            self.connection.execute(
                "UPDATE turns SET turn_number = -turn_number - 1 WHERE timeline_id = ?",
                (timeline_id,),
            )
            self.connection.execute(
                """
                UPDATE turns SET turn_number = -turn_number - 1 + ?
                WHERE timeline_id = ?
                """,
                (offset, timeline_id),
            )
        self.connection.execute(
            "INSERT INTO schema_migrations VALUES (?, ?)",
            (version, utc_now().isoformat()),
        )

    def _migrate_session_summaries(self) -> None:
        """Add summary columns without eagerly reading every historical turn."""
        version = 3
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "title" not in columns:
            self.connection.execute(
                "ALTER TABLE sessions ADD COLUMN title TEXT NOT NULL DEFAULT '新任务'"
            )
        if "turn_count" not in columns:
            self.connection.execute(
                "ALTER TABLE sessions ADD COLUMN turn_count INTEGER NOT NULL DEFAULT 0"
            )
        if "summary_ready" not in columns:
            prior_backfill = self.connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = 2"
            ).fetchone()
            default = 1 if prior_backfill is not None else 0
            self.connection.execute(
                "ALTER TABLE sessions ADD COLUMN summary_ready "
                f"INTEGER NOT NULL DEFAULT {default}"
            )
        applied = self.connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
        ).fetchone()
        if applied is not None:
            return
        self.connection.execute(
            "INSERT INTO schema_migrations VALUES (?, ?)",
            (version, utc_now().isoformat()),
        )

    def _migrate_turn_status(self) -> None:
        """Add a durable terminal status to legacy user-visible turns."""
        version = 4
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(turns)").fetchall()
        }
        if "status" not in columns:
            self.connection.execute(
                "ALTER TABLE turns ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'"
            )
        applied = self.connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (version,),
        ).fetchone()
        if applied is None:
            self.connection.execute(
                "INSERT INTO schema_migrations VALUES (?, ?)",
                (version, utc_now().isoformat()),
            )

    def _migrate_context_state(self) -> None:
        """Add timeline execution heads to databases created before context state."""
        version = 5
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(timelines)").fetchall()
        }
        if "head_checkpoint_id" not in columns:
            self.connection.execute(
                "ALTER TABLE timelines ADD COLUMN head_checkpoint_id TEXT"
            )
        self.connection.execute(
            """
            UPDATE timelines
            SET head_checkpoint_id = (
                SELECT checkpoint_id FROM turns
                WHERE turns.timeline_id = timelines.timeline_id
                ORDER BY turn_number DESC LIMIT 1
            )
            WHERE head_checkpoint_id IS NULL
            """
        )
        applied = self.connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (version,),
        ).fetchone()
        if applied is None:
            self.connection.execute(
                "INSERT INTO schema_migrations VALUES (?, ?)",
                (version, utc_now().isoformat()),
            )

    def _migrate_turn_timing(self) -> None:
        """Add nullable turn timing fields without inventing legacy durations."""
        version = 6
        columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(turns)").fetchall()
        }
        additions = {
            "started_at": "TEXT",
            "completed_at": "TEXT",
            "duration_ms": "INTEGER",
        }
        for column, data_type in additions.items():
            if column not in columns:
                self.connection.execute(
                    f"ALTER TABLE turns ADD COLUMN {column} {data_type}"
                )
        applied = self.connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (version,),
        ).fetchone()
        if applied is None:
            self.connection.execute(
                "INSERT INTO schema_migrations VALUES (?, ?)",
                (version, utc_now().isoformat()),
            )

    def _backfill_session_summary(self, session_id: str, timeline_id: str) -> None:
        """Populate one legacy summary when its list page is requested."""
        turns = [turn for turn in self.history_turns(timeline_id) if turn.user_text]
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET title = ?, turn_count = ?, summary_ready = 1
                WHERE session_id = ?
                """,
                (
                    turns[0].user_text[:72] if turns else "新任务",
                    len(turns),
                    session_id,
                ),
            )

    def _query_one(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> sqlite3.Row | None:
        with self._lock:
            return cast(
                "sqlite3.Row | None",
                self.connection.execute(sql, parameters).fetchone(),
            )

    def _query_all(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> list[sqlite3.Row]:
        with self._lock:
            return cast(
                "list[sqlite3.Row]",
                self.connection.execute(sql, parameters).fetchall(),
            )
