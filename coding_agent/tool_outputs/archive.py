"""Fail-closed, session-owned tool output storage."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import BaseMessage, ToolMessage
from langgraph.types import Command

from coding_agent.errors import fail
from coding_agent.models import utc_now
from coding_agent.repository import new_id
from coding_agent.tracing.artifacts import LocalArtifactStore

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_outputs (
    tool_output_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    part_name TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    encoding TEXT,
    byte_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    complete INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(invocation_id, sequence)
);
CREATE INDEX IF NOT EXISTS tool_outputs_session
ON tool_outputs(session_id, created_at);
"""


class ToolOutputArchiveService:
    """Store immutable tool results before LangChain commits their ToolMessage."""

    def __init__(self, database_path: Path, artifact_root: Path) -> None:
        self.artifacts = LocalArtifactStore(artifact_root)
        self.connection = sqlite3.connect(database_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(_SCHEMA)
        self.connection.commit()
        self._lock = threading.RLock()

    def archive(
        self,
        *,
        session_id: str,
        invocation_id: str,
        output: Any,
        sequence: int = 0,
        part_name: str = "result",
        complete: bool = True,
    ) -> dict[str, Any]:
        payload = self._serialize(output)
        return self.archive_bytes(
            session_id=session_id,
            invocation_id=invocation_id,
            payload=payload,
            media_type="application/json",
            sequence=sequence,
            part_name=part_name,
            complete=complete,
        )

    def archive_bytes(
        self,
        *,
        session_id: str,
        invocation_id: str,
        payload: bytes,
        media_type: str,
        sequence: int = 0,
        part_name: str = "result",
        complete: bool = True,
    ) -> dict[str, Any]:
        """Archive an already serialized output part."""
        try:
            artifact_id, relative_path = self.artifacts.put(payload)
            tool_output_id = new_id()
            created_at = utc_now().isoformat()
            with self._lock, self.connection:
                existing = self.connection.execute(
                    """
                    SELECT * FROM tool_outputs
                    WHERE invocation_id = ? AND sequence = ?
                    """,
                    (invocation_id, sequence),
                ).fetchone()
                if existing is not None:
                    return dict(existing)
                self.connection.execute(
                    """
                    INSERT INTO tool_outputs VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, 'utf-8',
                        ?, ?, ?, ?
                    )
                    """,
                    (
                        tool_output_id,
                        invocation_id,
                        session_id,
                        sequence,
                        part_name,
                        artifact_id,
                        relative_path,
                        media_type,
                        len(payload),
                        artifact_id,
                        int(complete),
                        created_at,
                    ),
                )
        except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
            raise fail(
                "TOOL_OUTPUT_PERSIST_FAILED",
                f"Tool output could not be persisted: {exc}",
            ) from exc
        return {
            "tool_output_id": tool_output_id,
            "invocation_id": invocation_id,
            "session_id": session_id,
            "sequence": sequence,
            "part_name": part_name,
            "artifact_id": artifact_id,
            "relative_path": relative_path,
            "media_type": media_type,
            "encoding": "utf-8",
            "byte_size": len(payload),
            "sha256": artifact_id,
            "complete": int(complete),
            "created_at": created_at,
        }

    def list_outputs(self, session_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT tool_output_id, invocation_id, part_name, media_type,
                       byte_size, sha256, complete, created_at
                FROM tool_outputs WHERE session_id = ? ORDER BY created_at
                """,
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def read(
        self,
        *,
        session_id: str,
        tool_output_id: str,
        offset: int = 0,
        limit: int = 32_768,
    ) -> dict[str, Any]:
        with self._lock:
            row = self.connection.execute(
                """
                SELECT * FROM tool_outputs
                WHERE session_id = ? AND tool_output_id = ?
                """,
                (session_id, tool_output_id),
            ).fetchone()
        if row is None:
            raise fail("TOOL_OUTPUT_NOT_FOUND", "Tool output does not exist.")
        data = self.artifacts.path(str(row["relative_path"])).read_bytes()
        chunk = data[max(offset, 0) : max(offset, 0) + max(limit, 1)]
        return {
            "tool_output_id": tool_output_id,
            "offset": max(offset, 0),
            "content": chunk.decode("utf-8", errors="replace"),
            "next_offset": offset + len(chunk) if offset + len(chunk) < len(data) else None,
            "byte_size": len(data),
            "sha256": str(row["sha256"]),
            "complete": bool(row["complete"]),
        }

    def search(
        self,
        *,
        session_id: str,
        query: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Search bounded textual previews without crossing Session ownership."""
        needle = query.casefold()
        matches: list[dict[str, Any]] = []
        with self._lock:
            rows = self.connection.execute(
                """
                SELECT * FROM tool_outputs
                WHERE session_id = ? ORDER BY created_at DESC
                """,
                (session_id,),
            ).fetchall()
        for row in rows:
            data = self.artifacts.path(str(row["relative_path"])).read_bytes()
            text = data.decode("utf-8", errors="replace")
            index = text.casefold().find(needle)
            if index < 0:
                continue
            start = max(0, index - 160)
            matches.append(
                {
                    "tool_output_id": str(row["tool_output_id"]),
                    "part_name": str(row["part_name"]),
                    "byte_size": int(row["byte_size"]),
                    "match_offset": index,
                    "preview": text[start : index + len(query) + 320],
                }
            )
            if len(matches) >= max(1, min(limit, 100)):
                break
        return matches

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    @staticmethod
    def _serialize(output: Any) -> bytes:
        if isinstance(output, BaseMessage) or hasattr(output, "model_dump"):
            value = output.model_dump(mode="json")
        else:
            value = output
        return json.dumps(
            value,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")


class ToolOutputArchiveMiddleware(AgentMiddleware[Any, None, Any]):
    """Persist full results before returning bounded references to the model."""

    def __init__(
        self,
        service: ToolOutputArchiveService,
        session_id: str,
        *,
        preview_chars: int = 4_800,
    ) -> None:
        self.service = service
        self.session_id = session_id
        self.preview_chars = preview_chars

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        invocation_id = str(request.tool_call["id"])
        try:
            result = handler(request)
        except BaseException as exc:
            self.service.archive(
                session_id=self.session_id,
                invocation_id=invocation_id,
                output={
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            raise
        if isinstance(result, ToolMessage):
            return self._archive_message(result, invocation_id, sequence=0)
        if isinstance(result, Command) and isinstance(result.update, dict):
            messages = result.update.get("messages")
            if isinstance(messages, list):
                archived = [
                    (
                        self._archive_message(message, invocation_id, sequence=index)
                        if isinstance(message, ToolMessage)
                        else message
                    )
                    for index, message in enumerate(messages)
                ]
                return replace(result, update={**result.update, "messages": archived})
        self.service.archive(
            session_id=self.session_id,
            invocation_id=invocation_id,
            output=result,
        )
        return result

    def _archive_message(
        self,
        message: ToolMessage,
        invocation_id: str,
        *,
        sequence: int,
    ) -> ToolMessage:
        archived = self.service.archive(
            session_id=self.session_id,
            invocation_id=invocation_id,
            output=message,
            sequence=sequence,
        )
        raw_preview = (
            message.content
            if isinstance(message.content, str)
            else json.dumps(message.content, ensure_ascii=False, default=str)
        )
        preview = raw_preview[: self.preview_chars]
        envelope = {
            "ok": message.status != "error",
            "tool_output_ref": archived["tool_output_id"],
            "parts": [
                {
                    "name": archived["part_name"],
                    "media_type": archived["media_type"],
                    "byte_size": archived["byte_size"],
                    "sha256": archived["sha256"],
                }
            ],
            "preview": preview,
            "preview_truncated": len(raw_preview) > len(preview),
            "persisted": True,
        }
        return message.model_copy(
            update={"content": json.dumps(envelope, ensure_ascii=False)}
        )
