"""Persistent operations and replayable UI events."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from coding_agent.models import utc_now
from coding_agent.repository import new_id

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    timeline_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    client_request_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    error_code TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS operations_idempotency
ON operations(session_id, client_request_id) WHERE client_request_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS operation_events (
    operation_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (operation_id, sequence)
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    status TEXT NOT NULL,
    decision TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT
);
"""


class EventJournal:
    """Thread-safe SQLite journal used as the browser recovery source."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(_SCHEMA)
        self.connection.execute(
            """
            UPDATE operations
            SET status = 'recovery_required', ended_at = ?
            WHERE status IN (
                'queued', 'running', 'waiting_approval', 'cancel_requested', 'committing'
            )
            """,
            (utc_now().isoformat(),),
        )
        self.connection.commit()
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def create_operation(
        self,
        *,
        session_id: str,
        timeline_id: str,
        kind: str,
        client_request_id: str | None,
    ) -> tuple[str, bool]:
        """Create an operation or return the prior idempotent submission."""
        with self._lock, self.connection:
            if client_request_id:
                existing = self.connection.execute(
                    """
                    SELECT operation_id FROM operations
                    WHERE session_id = ? AND client_request_id = ?
                    """,
                    (session_id, client_request_id),
                ).fetchone()
                if existing is not None:
                    return str(existing["operation_id"]), False
            operation_id = new_id()
            self.connection.execute(
                "INSERT INTO operations VALUES (?, ?, ?, ?, 'queued', ?, ?, NULL, NULL)",
                (
                    operation_id,
                    session_id,
                    timeline_id,
                    kind,
                    client_request_id,
                    utc_now().isoformat(),
                ),
            )
        self.append(operation_id, "operation.queued", {"kind": kind})
        return operation_id, True

    def emit_context_usage(
        self, operation_id: str, usage: dict[str, Any]
    ) -> int:
        """Emit a context.window_usage event with current context metrics."""
        return self.append(operation_id, "context.window_usage", usage)

    def append(self, operation_id: str, event_type: str, payload: dict[str, Any]) -> int:
        """Append one event with an operation-local monotonic sequence."""
        with self._changed, self.connection:
            row = self.connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS value "
                "FROM operation_events WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            sequence = int(row["value"])
            created_at = utc_now().isoformat()
            body = {
                "schema_version": 1,
                "operation_id": operation_id,
                "sequence": sequence,
                "created_at": created_at,
                **payload,
            }
            self.connection.execute(
                "INSERT INTO operation_events VALUES (?, ?, ?, ?, ?)",
                (
                    operation_id,
                    sequence,
                    event_type,
                    json.dumps(body, ensure_ascii=False),
                    created_at,
                ),
            )
            self._changed.notify_all()
            return sequence

    def events_after(self, operation_id: str, sequence: int) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT sequence, event_type, payload_json
                FROM operation_events
                WHERE operation_id = ? AND sequence > ?
                ORDER BY sequence
                """,
                (operation_id, sequence),
            ).fetchall()
        return [
            {
                "sequence": int(row["sequence"]),
                "event_type": str(row["event_type"]),
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def set_status(
        self,
        operation_id: str,
        status: str,
        error_code: str | None = None,
        *,
        expected: tuple[str, ...] | None = None,
    ) -> bool:
        """Conditionally transition an operation without overwriting a newer state."""
        terminal = status in {"completed", "failed", "cancelled", "recovery_required"}
        allowed = expected or {
            "running": ("queued", "waiting_approval"),
            "waiting_approval": ("running",),
            "committing": ("running",),
            "completed": ("committing",),
            "failed": ("queued", "running", "waiting_approval", "committing"),
            "cancelled": ("queued", "running", "waiting_approval", "cancel_requested"),
            "recovery_required": (
                "queued",
                "running",
                "waiting_approval",
                "cancel_requested",
                "committing",
            ),
        }.get(status)
        condition = ""
        parameters: list[Any] = [
            status,
            utc_now().isoformat() if terminal else None,
            error_code,
            operation_id,
        ]
        if allowed:
            placeholders = ", ".join("?" for _ in allowed)
            condition = f" AND status IN ({placeholders})"
            parameters.extend(allowed)
        with self._changed, self.connection:
            cursor = self.connection.execute(
                "UPDATE operations SET status = ?, ended_at = ?, error_code = ? "
                f"WHERE operation_id = ?{condition}",
                parameters,
            )
            self._changed.notify_all()
            return cursor.rowcount == 1

    def request_cancel(self, operation_id: str) -> bool:
        """Move one active operation to cancel_requested exactly once."""
        with self._changed, self.connection:
            cursor = self.connection.execute(
                """
                UPDATE operations
                SET status = 'cancel_requested'
                WHERE operation_id = ?
                  AND status IN ('queued', 'running', 'waiting_approval')
                """,
                (operation_id,),
            )
            self._changed.notify_all()
            return cursor.rowcount == 1

    def operation(self, operation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def active_operation(self) -> dict[str, Any] | None:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT * FROM operations
                WHERE status IN (
                    'queued', 'running', 'waiting_approval', 'cancel_requested', 'committing'
                )
                ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
        return dict(row) if row is not None else None

    def active_operations(self) -> list[dict[str, Any]]:
        """Return every active operation, grouped by its owning session in callers."""
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM operations
                WHERE status IN (
                    'queued', 'running', 'waiting_approval', 'cancel_requested', 'committing'
                )
                ORDER BY started_at
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_approvals(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["request"] = json.loads(item.pop("request_json"))
            result.append(item)
        return result
