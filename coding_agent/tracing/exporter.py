"""Export a fixed, content-free metrics schema to LangSmith."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import suppress
from typing import Any, Literal, Protocol

from langsmith import Client

from coding_agent.models import utc_now
from coding_agent.tracing.recorder import TraceStore, duration_ms
from coding_agent.tracing.redaction import redact

_LOGGER = logging.getLogger(__name__)


class LangSmithClient(Protocol):
    """Narrow client surface used by the metrics exporter."""

    def create_run(
        self,
        name: str,
        inputs: dict[str, Any],
        run_type: Literal["tool", "chain", "llm", "retriever", "embedding", "prompt", "parser"],
        **kwargs: Any,
    ) -> None:
        """Create one synthetic metrics run."""

    def flush(self, timeout: float | None = None) -> None:
        """Flush pending writes."""


class MetricsLangSmithExporter:
    """Upload aggregate metrics without messages, code, paths, or commands."""

    def __init__(
        self,
        store: TraceStore,
        *,
        enabled: bool,
        project: str,
        client_factory: Callable[[], LangSmithClient] | None = None,
    ) -> None:
        self.store = store
        self.enabled = enabled
        self.project = project
        self.client_factory = client_factory or (
            lambda: Client(
                auto_batch_tracing=False,
                omit_traced_runtime_info=True,
            )
        )
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="langsmith-metrics")

    def submit(self, trace_id: str) -> Future[bool] | None:
        """Queue an export without delaying turn completion."""
        if not self.enabled:
            self.export(trace_id)
            return None
        return self._executor.submit(self.export, trace_id)

    def close(self) -> None:
        """Wait for queued exports before closing local storage."""
        self._executor.shutdown(wait=True)

    def export(self, trace_id: str) -> bool:
        """Best-effort export one pending trace and persist its outcome."""
        if not self.enabled:
            with suppress(Exception):
                self.store.execute(
                    """
                    UPDATE traces SET langsmith_export_status = 'disabled'
                    WHERE trace_id = ? AND langsmith_export_status = 'pending'
                    """,
                    (trace_id,),
                )
            return False
        run_id = str(uuid.uuid4())
        try:
            row = self.store.query_one("SELECT * FROM traces WHERE trace_id = ?", (trace_id,))
            if row is None or row["langsmith_export_status"] == "exported":
                return row is not None
            payload = self.build_payload(trace_id)
            client = self.client_factory()
            client.create_run(
                "coding-agent.metrics",
                payload,
                "chain",
                id=run_id,
                project_name=self.project,
                start_time=utc_now(),
                end_time=utc_now(),
                outputs={"status": payload["status"]},
                tags=["coding-agent", "metrics-only"],
            )
            client.flush(timeout=5)
        except Exception as exc:
            message = str(redact(str(exc)))[:1024]
            with suppress(Exception):
                self.store.execute(
                    """
                    UPDATE traces
                    SET langsmith_export_status = 'failed',
                        langsmith_export_attempts = langsmith_export_attempts + 1,
                        langsmith_export_last_error = ?
                    WHERE trace_id = ?
                    """,
                    (message, trace_id),
                )
            _LOGGER.debug("LangSmith metrics export failed: %s", type(exc).__name__)
            return False
        with suppress(Exception):
            self.store.execute(
                """
                UPDATE traces
                SET langsmith_run_id = ?, langsmith_export_status = 'exported',
                    langsmith_export_attempts = langsmith_export_attempts + 1,
                    langsmith_export_last_error = NULL
                WHERE trace_id = ?
                """,
                (run_id, trace_id),
            )
        return True

    def retry_pending(self, limit: int = 20) -> int:
        """Retry a bounded number of pending or failed metrics exports."""
        if not self.enabled:
            return 0
        try:
            rows = self.store.query_all(
                """
                SELECT trace_id FROM traces
                WHERE status != 'running'
                  AND langsmith_export_status IN ('pending', 'failed')
                ORDER BY started_at LIMIT ?
                """,
                (limit,),
            )
        except Exception as exc:
            _LOGGER.debug("Cannot load pending LangSmith metrics: %s", type(exc).__name__)
            return 0
        for row in rows:
            self.submit(str(row["trace_id"]))
        return len(rows)

    def build_payload(self, trace_id: str) -> dict[str, Any]:
        """Build the only schema allowed to cross the LangSmith boundary."""
        trace = self.store.query_one("SELECT * FROM traces WHERE trace_id = ?", (trace_id,))
        if trace is None:
            raise ValueError(f"Unknown trace: {trace_id}")
        spans = self.store.query_all(
            "SELECT kind, name, status, started_at, ended_at, "
            "input_artifact_id, output_artifact_id, "
            "attributes_json FROM spans WHERE trace_id = ?",
            (trace_id,),
        )
        model_calls = sum(row["kind"] == "model" for row in spans)
        tool_calls = sum(row["kind"] == "tool" for row in spans)
        commands = [row for row in spans if row["kind"] == "tool" and row["name"] == "run_command"]
        approvals = sum(row["kind"] == "approval" for row in spans)
        compressions = sum(row["name"] == "context.compression" for row in spans)
        tool_runs = [row for row in spans if row["kind"] == "tool_run"]
        tool_groups = [row for row in spans if row["name"] == "tool.group"]
        scheduler_wakes = sum(
            row["name"] in {"scheduler.wake", "scheduler.wait"} for row in spans
        )
        changed_files = added_lines = deleted_lines = 0
        peak_parallelism = 0
        background_commands = []
        artifact_ids: set[str] = set()
        for row in spans:
            attributes = json.loads(row["attributes_json"])
            if row["name"] == "workspace.snapshot":
                changed_files += int(attributes.get("changed_file_count", 0))
                added_lines += int(attributes.get("added_lines", 0))
                deleted_lines += int(attributes.get("deleted_lines", 0))
            if row["name"] in {"tool.group", "tool.scheduler.summary"}:
                peak_parallelism = max(
                    peak_parallelism,
                    int(attributes.get("peak_parallelism", 0)),
                )
            tool_name = str(attributes.get("tool_name", ""))
            if row["kind"] == "tool_run" and (
                tool_name == "run_command" or tool_name.startswith("skill:")
            ):
                background_commands.append(row)
            for column in ("input_artifact_id", "output_artifact_id"):
                if row[column]:
                    artifact_ids.add(str(row[column]))
            traceback_id = attributes.get("traceback_artifact_id")
            if isinstance(traceback_id, str):
                artifact_ids.add(traceback_id)
        linked_artifacts = self.store.query_all(
            "SELECT artifact_id FROM trace_artifacts WHERE trace_id = ?",
            (trace_id,),
        )
        artifact_ids.update(str(row["artifact_id"]) for row in linked_artifacts)
        manifest = hashlib.sha256("\n".join(sorted(artifact_ids)).encode()).hexdigest()
        return {
            "session_id_hash": self._hash(str(trace["session_id"])),
            "timeline_id_hash": self._hash(str(trace["timeline_id"])),
            "turn_id": str(trace["turn_id"]),
            "trace_id": str(trace["trace_id"]),
            "logical_run_id": str(trace["logical_run_id"]),
            "status": str(trace["status"]),
            "model_id": str(trace["model_id"]),
            "input_tokens": int(trace["input_tokens"]),
            "output_tokens": int(trace["output_tokens"]),
            "model_call_count": model_calls,
            "tool_call_count": tool_calls,
            "tool_run_count": len(tool_runs),
            "tool_run_cancelled_count": sum(row["status"] == "cancelled" for row in tool_runs),
            "tool_run_failed_count": sum(row["status"] == "error" for row in tool_runs),
            "tool_compute_time_ms": sum(
                duration_ms(row["started_at"], row["ended_at"]) for row in tool_runs
            ),
            "tool_group_count": len(tool_groups),
            "peak_tool_parallelism": peak_parallelism,
            "scheduler_wake_count": scheduler_wakes,
            "command_count": (
                len(background_commands) if background_commands else len(commands)
            ),
            "command_failure_count": (
                sum(row["status"] != "ok" for row in background_commands)
                if background_commands
                else sum(row["status"] != "ok" for row in commands)
            ),
            "approval_count": approvals,
            "changed_file_count": changed_files,
            "added_lines": added_lines,
            "deleted_lines": deleted_lines,
            "context_compression_count": compressions,
            "duration_ms": duration_ms(trace["started_at"], trace["ended_at"]),
            "error_code": None,
            "local_artifact_manifest_hash": manifest,
        }

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()
