"""Fail-open local trace recording backed by SQLite and artifacts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import traceback
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from coding_agent.models import utc_now
from coding_agent.repository import new_id
from coding_agent.tracing.artifacts import LocalArtifactStore
from coding_agent.tracing.redaction import redact

_LOGGER = logging.getLogger(__name__)
_INLINE_PAYLOAD_LIMIT = 16 * 1024
_TRACE_DISPLAY_LIMIT = 4096

_TRACE_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    timeline_id TEXT NOT NULL,
    logical_run_id TEXT NOT NULL,
    root_span_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    langsmith_run_id TEXT,
    langsmith_export_status TEXT NOT NULL DEFAULT 'pending',
    langsmith_export_attempts INTEGER NOT NULL DEFAULT 0,
    langsmith_export_last_error TEXT
);
CREATE INDEX IF NOT EXISTS traces_by_turn ON traces(turn_id);
CREATE TABLE IF NOT EXISTS spans (
    span_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    parent_span_id TEXT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    input_artifact_id TEXT,
    output_artifact_id TEXT,
    attributes_json TEXT NOT NULL,
    error_type TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS spans_by_trace ON spans(trace_id, started_at);
CREATE TABLE IF NOT EXISTS span_links (
    source_span_id TEXT NOT NULL,
    target_span_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    attributes_json TEXT NOT NULL,
    PRIMARY KEY (source_span_id, target_span_id, relation)
);
CREATE INDEX IF NOT EXISTS span_links_by_target ON span_links(target_span_id);
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    relative_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trace_artifacts (
    trace_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    PRIMARY KEY (trace_id, artifact_id)
);
CREATE TABLE IF NOT EXISTS compressions (
    compression_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    timeline_id TEXT NOT NULL,
    turn_number INTEGER NOT NULL,
    urgency TEXT NOT NULL,
    strategy TEXT NOT NULL,
    original_message_count INTEGER NOT NULL,
    compressed_message_count INTEGER NOT NULL,
    original_token_count INTEGER NOT NULL,
    compressed_token_count INTEGER NOT NULL,
    compression_ratio REAL NOT NULL,
    cache_hit_before INTEGER DEFAULT 0,
    cache_miss_before INTEGER DEFAULT 0,
    pre_compress_artifact_id TEXT,
    created_at TEXT NOT NULL
);
"""


class TraceStore:
    """Store local trace rows independently from the Agent transaction path."""

    def __init__(self, database_path: Path, artifact_root: Path) -> None:
        self.database_path = database_path
        self.artifacts = LocalArtifactStore(artifact_root)
        self.connection = sqlite3.connect(database_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(_TRACE_SCHEMA)
        self._recover_interrupted_traces()
        self.connection.commit()
        os.chmod(database_path, 0o600)
        self._lock = threading.RLock()

    def _recover_interrupted_traces(self) -> None:
        """Close traces left running by an unclean Host shutdown."""
        ended_at = utc_now().isoformat()
        self.connection.execute(
            """
            UPDATE spans
            SET status = 'cancelled', ended_at = ?,
                error_type = COALESCE(error_type, 'HostRestart'),
                error_message = COALESCE(
                    error_message,
                    'Host restarted before this span completed'
                )
            WHERE status = 'running'
            """,
            (ended_at,),
        )
        self.connection.execute(
            """
            UPDATE traces
            SET status = 'interrupted', ended_at = ?
            WHERE status = 'running'
            """,
            (ended_at,),
        )

    def close(self) -> None:
        """Close trace storage."""
        self.connection.close()

    def execute(self, sql: str, parameters: Sequence[object] = ()) -> None:
        """Execute one trace mutation."""
        with self._lock, self.connection:
            self.connection.execute(sql, parameters)

    def query_one(self, sql: str, parameters: Sequence[object] = ()) -> sqlite3.Row | None:
        """Return one trace row."""
        with self._lock:
            return cast(
                "sqlite3.Row | None",
                self.connection.execute(sql, parameters).fetchone(),
            )

    def query_all(self, sql: str, parameters: Sequence[object] = ()) -> list[sqlite3.Row]:
        """Return trace rows."""
        with self._lock:
            return self.connection.execute(sql, parameters).fetchall()

    def save_artifact(self, payload: bytes, media_type: str) -> str:
        """Persist one artifact and its metadata."""
        artifact_id, relative_path = self.artifacts.put(payload)
        self.execute(
            "INSERT OR IGNORE INTO artifacts VALUES (?, ?, ?, ?, ?, ?)",
            (
                artifact_id,
                artifact_id,
                media_type,
                len(payload),
                relative_path,
                utc_now().isoformat(),
            ),
        )
        return artifact_id

    def trace_for_turn(self, timeline_id: str, turn_number: int) -> dict[str, Any] | None:
        """Load a compact trace summary for a committed turn."""
        row = self.query_one(
            """
            SELECT tr.*
            FROM traces tr
            JOIN turns t ON t.turn_id = tr.turn_id
            WHERE t.timeline_id = ? AND t.turn_number = ?
            ORDER BY tr.started_at DESC LIMIT 1
            """,
            (timeline_id, turn_number),
        )
        return self._trace_summary(row)

    def trace_for_turn_id(self, turn_id: str) -> dict[str, Any] | None:
        """Load a compact trace summary for a physical turn ID."""
        row = self.query_one(
            "SELECT * FROM traces WHERE turn_id = ? ORDER BY started_at DESC LIMIT 1",
            (turn_id,),
        )
        return self._trace_summary(row)

    def _trace_summary(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        """Build a compact trace summary from a trace row."""
        if row is None:
            return None
        spans = self.query_all(
            """
            SELECT span_id, parent_span_id, kind, name, status, started_at, ended_at,
                   input_artifact_id, output_artifact_id, attributes_json, error_type
            FROM spans WHERE trace_id = ? ORDER BY started_at
            """,
            (row["trace_id"],),
        )
        rendered_spans = []
        for span in spans:
            rendered = dict(span)
            attributes = str(rendered["attributes_json"])
            rendered["attributes_json"] = truncate_middle(
                attributes,
                _TRACE_DISPLAY_LIMIT,
            )
            rendered["attributes_json_length"] = len(attributes)
            rendered["attributes_json_truncated"] = len(attributes) > _TRACE_DISPLAY_LIMIT
            rendered_spans.append(rendered)
        links = [
            dict(link)
            for link in self.query_all(
                """
                SELECT source_span_id, target_span_id, relation, attributes_json
                FROM span_links
                WHERE source_span_id IN (SELECT span_id FROM spans WHERE trace_id = ?)
                   OR target_span_id IN (SELECT span_id FROM spans WHERE trace_id = ?)
                ORDER BY source_span_id, target_span_id, relation
                """,
                (row["trace_id"], row["trace_id"]),
            )
        ]
        return {
            "trace_id": row["trace_id"],
            "status": row["status"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "langsmith_export_status": row["langsmith_export_status"],
            "artifact_root": str(self.artifacts.root),
            "spans": rendered_spans,
            "span_links": links,
        }


class TraceRecorder:
    """Record one turn's complete local execution tree without breaking execution."""

    def __init__(
        self,
        *,
        store: TraceStore,
        trace_id: str,
        turn_id: str,
        session_id: str,
        timeline_id: str,
        logical_run_id: str,
        model_id: str,
        turn_number: int = 0,
        secrets: Sequence[str] = (),
    ) -> None:
        self.store = store
        self.trace_id = trace_id
        self.turn_id = turn_id
        self.session_id = session_id
        self.timeline_id = timeline_id
        self.turn_number = turn_number
        self.root_span_id = new_id()
        self.secrets = tuple(secret for secret in secrets if secret)
        self._lock = threading.RLock()
        self._started_spans: set[str] = set()
        started_at = utc_now().isoformat()
        self._safe_execute(
            "INSERT INTO traces VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, NULL, "
            "0, 0, NULL, 'pending', 0, NULL)",
            (
                trace_id,
                turn_id,
                session_id,
                timeline_id,
                logical_run_id,
                self.root_span_id,
                model_id,
                started_at,
            ),
        )
        self._safe_execute(
            "INSERT INTO spans VALUES (?, ?, NULL, 'turn', 'turn', 'running', ?, "
            "NULL, NULL, NULL, '{}', NULL, NULL)",
            (self.root_span_id, trace_id, started_at),
        )
        self._started_spans.add(self.root_span_id)

    @contextmanager
    def span(
        self,
        name: str,
        *,
        kind: str = "application",
        parent_span_id: str | None = None,
        inputs: Any = None,
        attributes: dict[str, Any] | None = None,
    ) -> Generator[str, None, None]:
        """Record a timed application span."""
        span_id = self.start_span(
            name,
            kind=kind,
            parent_span_id=parent_span_id,
            inputs=inputs,
            attributes=attributes,
        )
        try:
            yield span_id
        except BaseException as exc:
            self.end_span(span_id, status="error", error=exc)
            raise
        else:
            self.end_span(span_id)

    def start_span(
        self,
        name: str,
        *,
        kind: str,
        parent_span_id: str | None = None,
        inputs: Any = None,
        attributes: dict[str, Any] | None = None,
        span_id: str | None = None,
    ) -> str:
        """Start a span and return its ID."""
        with self._lock:
            local_span_id = span_id or new_id()
            input_artifact_id, payload_attributes = self._persist_payload(inputs)
            safe_attributes = redact(attributes or {}, secrets=self.secrets)
            if payload_attributes is not None:
                safe_attributes["input"] = payload_attributes
            self._safe_execute(
                "INSERT OR IGNORE INTO spans VALUES (?, ?, ?, ?, ?, 'running', ?, NULL, "
                "?, NULL, ?, NULL, NULL)",
                (
                    local_span_id,
                    self.trace_id,
                    parent_span_id or self.root_span_id,
                    kind,
                    name,
                    utc_now().isoformat(),
                    input_artifact_id,
                    self._json(safe_attributes),
                ),
            )
            self._started_spans.add(local_span_id)
            return local_span_id

    def end_span(
        self,
        span_id: str,
        *,
        status: str = "ok",
        output: Any = None,
        attributes: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        """Finish a previously started span."""
        with self._lock:
            if span_id not in self._started_spans:
                return
            output_artifact_id, payload_attributes = self._persist_payload(output)
            try:
                row = self.store.query_one(
                    "SELECT attributes_json FROM spans WHERE span_id = ?", (span_id,)
                )
                existing = json.loads(row["attributes_json"]) if row is not None else {}
            except Exception as exc:
                _LOGGER.warning("Local trace read failed: %s", exc)
                existing = {}
            existing.update(redact(attributes or {}, secrets=self.secrets))
            if payload_attributes is not None:
                existing["output"] = payload_attributes
            error_type = type(error).__name__ if error is not None else None
            error_message = (
                str(redact(str(error), secrets=self.secrets))[:4096]
                if error is not None
                else None
            )
            if error is not None:
                trace = redact("".join(traceback.format_exception(error)), secrets=self.secrets)
                try:
                    artifact_id = self.capture_text_artifact(str(trace))
                    existing["traceback_artifact_id"] = artifact_id
                except Exception as exc:
                    _LOGGER.warning("Traceback artifact write failed: %s", exc)
            self._safe_execute(
                """
                UPDATE spans
                SET status = ?, ended_at = ?, output_artifact_id = ?, attributes_json = ?,
                    error_type = ?, error_message = ?
                WHERE span_id = ?
                """,
                (
                    status,
                    utc_now().isoformat(),
                    output_artifact_id,
                    self._json(existing),
                    error_type,
                    error_message,
                    span_id,
                ),
            )
            self._started_spans.discard(span_id)

    def link_spans(
        self,
        source_span_id: str,
        target_span_id: str,
        relation: str,
        *,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """Record a causal relation that cannot be represented by one parent."""
        self._safe_execute(
            "INSERT OR REPLACE INTO span_links VALUES (?, ?, ?, ?)",
            (
                source_span_id,
                target_span_id,
                relation,
                self._json(redact(attributes or {}, secrets=self.secrets)),
            ),
        )

    def add_tokens(self, input_tokens: int, output_tokens: int) -> None:
        """Accumulate model usage for the trace."""
        self._safe_execute(
            """
            UPDATE traces
            SET input_tokens = input_tokens + ?, output_tokens = output_tokens + ?
            WHERE trace_id = ?
            """,
            (max(input_tokens, 0), max(output_tokens, 0), self.trace_id),
        )

    def add_cache_stats(
        self,
        cache_hit_tokens: int,
        cache_miss_tokens: int,
    ) -> None:
        """Record prompt cache statistics on the root span as attributes."""
        with self._lock:
            existing = self.store.query_one(
                "SELECT attributes_json FROM spans WHERE span_id = ?",
                (self.root_span_id,),
            )
            if existing is None:
                return
            try:
                attrs = json.loads(existing["attributes_json"])
            except (json.JSONDecodeError, TypeError):
                attrs = {}
            total_hit = int(attrs.get("cache_hit_tokens", 0)) + max(cache_hit_tokens, 0)
            total_miss = int(attrs.get("cache_miss_tokens", 0)) + max(cache_miss_tokens, 0)
            attrs["cache_hit_tokens"] = total_hit
            attrs["cache_miss_tokens"] = total_miss
            attrs["cache_hit_rate"] = round(
                total_hit / max(total_hit + total_miss, 1),
                4,
            )
            self._safe_execute(
                "UPDATE spans SET attributes_json = ? WHERE span_id = ?",
                (self._json(attrs), self.root_span_id),
            )

    def record_compression(
        self,
        *,
        session_id: str,
        timeline_id: str,
        turn_number: int,
        urgency: str,
        strategy: str,
        original_message_count: int,
        compressed_message_count: int,
        original_token_count: int,
        compressed_token_count: int,
        cache_hit_before: int = 0,
        cache_miss_before: int = 0,
        pre_compress_artifact_id: str | None = None,
    ) -> None:
        """Record a compression event in the compressions table."""
        from coding_agent.repository import new_id

        ratio = (
            round(compressed_token_count / max(original_token_count, 1), 4)
            if original_token_count
            else 0.0
        )
        self._safe_execute(
            """
            INSERT INTO compressions (
                compression_id,
                session_id,
                timeline_id,
                turn_number,
                urgency,
                strategy,
                original_message_count,
                compressed_message_count,
                original_token_count,
                compressed_token_count,
                compression_ratio,
                cache_hit_before,
                cache_miss_before,
                pre_compress_artifact_id,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                session_id,
                timeline_id,
                turn_number,
                urgency,
                strategy,
                original_message_count,
                compressed_message_count,
                original_token_count,
                compressed_token_count,
                ratio,
                cache_hit_before,
                cache_miss_before,
                pre_compress_artifact_id,
                utc_now().isoformat(),
            ),
        )
        span_id = self.start_span(
            "context.compression",
            kind="context",
            attributes={
                "urgency": urgency,
                "strategy": strategy,
                "turn_number": turn_number,
                "original_message_count": original_message_count,
                "compressed_message_count": compressed_message_count,
                "original_token_count": original_token_count,
                "compressed_token_count": compressed_token_count,
                "compression_ratio": ratio,
                "cache_hit_before": cache_hit_before,
                "cache_miss_before": cache_miss_before,
                "pre_compress_artifact_id": pre_compress_artifact_id,
            },
        )
        self.end_span(span_id)

    def capture_text_artifact(self, value: bytes | str, media_type: str = "text/plain") -> str:
        """Persist a redacted full-text artifact and return its content ID."""
        text = value.decode(errors="replace") if isinstance(value, bytes) else value
        safe_text = str(redact(text, secrets=self.secrets))
        artifact_id = self.store.save_artifact(safe_text.encode(), media_type)
        self._link_artifact(artifact_id)
        return artifact_id

    def finish(self, *, status: str, error: BaseException | None = None) -> None:
        """Close the root span and trace."""
        with self._lock:
            unfinished = self._started_spans - {self.root_span_id}
            for span_id in list(unfinished):
                self.end_span(
                    span_id,
                    status="cancelled",
                    attributes={"forced_closed": True},
                )
            root_status = {
                "completed": "ok",
                "cancelled": "cancelled",
                "interrupted": "cancelled",
            }.get(status, "error")
            self.end_span(self.root_span_id, status=root_status, error=error)
            self._safe_execute(
                "UPDATE traces SET status = ?, ended_at = ? WHERE trace_id = ?",
                (status, utc_now().isoformat(), self.trace_id),
            )

    def _persist_payload(self, value: Any) -> tuple[str | None, Any | None]:
        if value is None:
            return None, None
        safe_value = redact(value, secrets=self.secrets)
        encoded = self._json(safe_value).encode()
        if len(encoded) <= _INLINE_PAYLOAD_LIMIT:
            return None, safe_value
        try:
            artifact_id = self.store.save_artifact(encoded, "application/json")
            self._link_artifact(artifact_id)
            return artifact_id, None
        except Exception as exc:
            _LOGGER.warning("Trace artifact write failed: %s", exc)
            return None, {
                "capture_error": type(exc).__name__,
                "sha256": hashlib.sha256(encoded).hexdigest(),
            }

    def _link_artifact(self, artifact_id: str) -> None:
        self._safe_execute(
            "INSERT OR IGNORE INTO trace_artifacts VALUES (?, ?)",
            (self.trace_id, artifact_id),
        )

    def _safe_execute(self, sql: str, parameters: Sequence[object]) -> None:
        try:
            self.store.execute(sql, parameters)
        except Exception as exc:
            _LOGGER.warning("Local trace write failed: %s", exc)

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def duration_ms(started_at: str, ended_at: str | None) -> int:
    """Return a non-negative duration between persisted timestamps."""
    if ended_at is None:
        return 0
    start = datetime.fromisoformat(started_at)
    end = datetime.fromisoformat(ended_at)
    return max(0, int((end - start).total_seconds() * 1000))


def truncate_middle(value: str, limit: int) -> str:
    """Limit text by preserving both ends and marking the omitted middle."""
    if limit < 5:
        raise ValueError("limit must be at least 5")
    if len(value) <= limit:
        return value
    marker = "\n...\n"
    available = limit - len(marker)
    head_length = available // 2
    tail_length = available - head_length
    return f"{value[:head_length]}{marker}{value[-tail_length:]}"
