import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.messages import ToolMessage

from coding_agent.errors import CodingAgentError
from coding_agent.tool_outputs import ToolOutputArchiveMiddleware, ToolOutputArchiveService


def test_tool_output_archive_deduplicates_blob_but_keeps_logical_ownership(
    tmp_path: Path,
) -> None:
    service = ToolOutputArchiveService(tmp_path / "agent.db", tmp_path / "artifacts")

    first = service.archive(
        session_id="session-a",
        invocation_id="call-a",
        output={"value": "same"},
    )
    second = service.archive(
        session_id="session-a",
        invocation_id="call-b",
        output={"value": "same"},
    )

    assert first["artifact_id"] == second["artifact_id"]
    assert first["tool_output_id"] != second["tool_output_id"]
    assert len(service.list_outputs("session-a")) == 2
    matches = service.search(session_id="session-a", query="same")
    assert {match["tool_output_id"] for match in matches} == {
        first["tool_output_id"],
        second["tool_output_id"],
    }
    service.close()


def test_tool_output_middleware_persists_before_returning_reference(
    tmp_path: Path,
) -> None:
    service = ToolOutputArchiveService(tmp_path / "agent.db", tmp_path / "artifacts")
    middleware = ToolOutputArchiveMiddleware(
        service,
        "session-a",
        preview_chars=8,
    )
    request = SimpleNamespace(tool_call={"id": "call-a"})

    result = middleware.wrap_tool_call(
        cast(Any, request),
        lambda _request: ToolMessage(
            content="complete output",
            tool_call_id="call-a",
            status="success",
        ),
    )

    assert isinstance(result, ToolMessage)
    envelope = json.loads(cast(str, result.content))
    assert envelope["persisted"] is True
    assert envelope["preview"] == "complete"
    assert envelope["preview_truncated"] is True
    archived = service.read(
        session_id="session-a",
        tool_output_id=envelope["tool_output_ref"],
    )
    assert "complete output" in archived["content"]
    with pytest.raises(CodingAgentError, match="does not exist"):
        service.read(
            session_id="session-b",
            tool_output_id=envelope["tool_output_ref"],
        )
    service.close()
