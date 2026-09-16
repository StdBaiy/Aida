"""Structured child-Agent result envelopes."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError


class ResultCheck(BaseModel):
    """One reproducible verification result."""

    command: str
    exit_code: int | None = None
    artifact_ref: str | None = None


class ResultEvidence(BaseModel):
    """One evidence reference without embedding its full payload."""

    kind: str
    title: str
    source_ref: str | None = None
    tool: str | None = None


class ResultProvenance(BaseModel):
    """Machine-readable source metadata."""

    provider: str
    tool: str | None = None
    artifact_ref: str | None = None
    sandbox_id: str | None = None
    container_id: str | None = None
    image_digest: str | None = None
    isolation_level: str | None = None
    resource_usage: dict[str, Any] | None = None
    sandbox_policy_digest: str | None = None


class ResultEnvelope(BaseModel):
    """Validated result consumed by review and UI layers."""

    schema_version: int = 1
    result_kind: Literal["analysis", "patch"]
    status: Literal["completed"] = "completed"
    summary: str = Field(min_length=1, max_length=1000)
    changed_files: list[str] = Field(default_factory=list)
    result_commit: str | None = None
    checks: list[ResultCheck] = Field(default_factory=list)
    evidence: list[ResultEvidence] = Field(default_factory=list)
    provenance: list[ResultProvenance] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    legacy_raw_output: str | None = None


def build_result_envelope(
    response: str,
    *,
    result_kind: Literal["analysis", "patch"],
    changed_files: list[str],
    result_commit: str | None,
) -> ResultEnvelope:
    """Parse exact JSON output or safely retain an unstructured legacy response."""
    try:
        value: Any = json.loads(response)
        envelope = ResultEnvelope.model_validate(value)
    except (json.JSONDecodeError, ValidationError):
        envelope = ResultEnvelope(
            result_kind=result_kind,
            summary="子 Agent 已完成任务，等待主 Agent review。",
            legacy_raw_output=response,
        )
    update: dict[str, Any] = {
        "result_kind": result_kind,
        "changed_files": changed_files,
        "result_commit": result_commit,
    }
    return envelope.model_copy(update=update)
