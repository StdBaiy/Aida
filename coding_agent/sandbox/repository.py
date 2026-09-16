"""Persistent sandbox execution lifecycle records."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from coding_agent.models import utc_now

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sandbox_executions (
    execution_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    host_instance_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    state TEXT NOT NULL,
    resource_id TEXT,
    container_id TEXT,
    image_digest TEXT,
    policy_digest TEXT,
    termination_reason TEXT,
    usage_json TEXT NOT NULL DEFAULT '{}',
    cleanup_pending INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sandbox_executions_state
ON sandbox_executions(state, cleanup_pending);
CREATE TABLE IF NOT EXISTS sandbox_provider_health (
    provider TEXT PRIMARY KEY,
    health_json TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
"""


class SandboxExecutionRepository:
    """Thread-safe execution journal independent of LangGraph checkpoints."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(_SCHEMA)
        self._ensure_column("sandbox_executions", "resource_id", "TEXT")
        self._ensure_column("sandbox_executions", "policy_digest", "TEXT")
        self.connection.commit()
        os.chmod(path, 0o600)

    def create(
        self,
        execution_id: str,
        owner_id: str,
        host_instance_id: str,
        *,
        provider: str,
    ) -> None:
        now = utc_now().isoformat()
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO sandbox_executions (
                    execution_id, owner_id, host_instance_id, provider, state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'waiting_resources', ?, ?)
                """,
                (execution_id, owner_id, host_instance_id, provider, now, now),
            )

    def update(
        self,
        execution_id: str,
        state: str,
        *,
        resource_id: str | None = None,
        container_id: str | None = None,
        image_digest: str | None = None,
        policy_digest: str | None = None,
        termination_reason: str | None = None,
        usage: dict[str, Any] | None = None,
        cleanup_pending: bool | None = None,
    ) -> None:
        assignments = ["state = ?", "updated_at = ?"]
        values: list[object] = [state, utc_now().isoformat()]
        for column, value in (
            ("resource_id", resource_id),
            ("container_id", container_id),
            ("image_digest", image_digest),
            ("policy_digest", policy_digest),
            ("termination_reason", termination_reason),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(value)
        if usage is not None:
            assignments.append("usage_json = ?")
            values.append(json.dumps(usage, sort_keys=True))
        if cleanup_pending is not None:
            assignments.append("cleanup_pending = ?")
            values.append(int(cleanup_pending))
        values.append(execution_id)
        with self._lock, self.connection:
            self.connection.execute(
                f"UPDATE sandbox_executions SET {', '.join(assignments)} "
                "WHERE execution_id = ?",
                values,
            )

    def active(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM sandbox_executions
                WHERE state NOT IN ('completed', 'failed', 'cancelled')
                   OR cleanup_pending = 1
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def save_health(self, health: dict[str, Any]) -> None:
        checked_at = str(health.get("checked_at") or utc_now().isoformat())
        provider = str(health.get("provider") or "unknown")
        with self._lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO sandbox_provider_health(provider, health_json, checked_at)
                VALUES (?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    health_json = excluded.health_json,
                    checked_at = excluded.checked_at
                """,
                (
                    provider,
                    json.dumps(health, sort_keys=True, default=str),
                    checked_at,
                ),
            )

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {
            str(row["name"])
            for row in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self.connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )
