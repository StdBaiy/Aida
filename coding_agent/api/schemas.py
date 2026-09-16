"""Stable HTTP request schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class TurnRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    client_request_id: str = Field(min_length=8, max_length=128)
    expected_timeline_id: str


class RestoreRequest(BaseModel):
    turn_number: int = Field(ge=0)
    expected_timeline_id: str


class ApprovalDecision(BaseModel):
    operation_id: str
    request_hash: str
    decision: Literal["approve", "reject"]


class SettingsUpdate(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=2048)
    langsmith_enabled: bool
    langsmith_project: str = Field(min_length=1, max_length=200)
    model_timeout_seconds: int = Field(ge=1, le=1800)
    command_timeout_seconds: int = Field(ge=1, le=1800)
    max_parallel_sessions: int = Field(ge=1, le=16)

    @field_validator("model", "langsmith_project")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value must not be blank")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url must use http:// or https://")
        return value


class WorkspacePathRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)

    @field_validator("path")
    @classmethod
    def strip_path(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("path must not be blank")
        return value
