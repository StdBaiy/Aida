"""FastAPI adapter for the local Coding Agent host."""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from coding_agent.api.schemas import (
    ApprovalDecision,
    CompactContextRequest,
    RestoreRequest,
    SettingsUpdate,
    TurnRequest,
    WorkspacePathRequest,
)
from coding_agent.application.service import CodingAgentHost, WorkspaceManager
from coding_agent.errors import CodingAgentError


def create_app(
    host: CodingAgentHost | WorkspaceManager,
    static_dir: Path | None = None,
) -> FastAPI:
    """Create a single-workspace, local-only HTTP application."""
    session_token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        await host.start()
        yield
        await host.close()

    app = FastAPI(title="Coding Agent", version="1.0.0", lifespan=lifespan)
    app.state.host = host

    @app.middleware("http")
    async def local_security(request: Request, call_next: Any) -> Response:
        hostname = request.url.hostname
        if hostname not in {"127.0.0.1", "localhost", "testserver"}:
            return _error("INVALID_HOST", "Only local browser access is allowed.", 403)
        origin = request.headers.get("origin")
        if origin and origin not in {
            f"http://{request.url.netloc}",
            f"https://{request.url.netloc}",
        }:
            return _error("INVALID_ORIGIN", "Cross-origin requests are forbidden.", 403)
        protected_api = (
            request.url.path.startswith("/api/v1") and request.url.path != "/api/v1/status"
        )
        if protected_api and request.cookies.get("coding_agent_session") != session_token:
            return _error("UNAUTHORIZED", "Open the application status endpoint first.", 401)
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.headers.get("x-csrf-token") != csrf_token
        ):
            return _error("CSRF_FAILED", "The request token is invalid.", 403)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return cast("Response", response)

    @app.exception_handler(CodingAgentError)
    async def coding_agent_error(_request: Request, exc: CodingAgentError) -> JSONResponse:
        status = (
            404
            if exc.code.endswith("NOT_FOUND")
            else 409
            if exc.code
            in {
                "WORKSPACE_BUSY",
                "SESSION_BUSY",
                "TIMELINE_CHANGED",
                "SESSION_NOT_ACTIVE",
                "APPROVAL_EXPIRED",
                "APPROVAL_MISMATCH",
                "WORKSPACE_ACTIVE",
                "TURN_ACTIVE",
            }
            else 400
        )
        return _error(exc.code, exc.user_message, status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return _error("INVALID_REQUEST", str(exc), 422)

    @app.get("/api/v1/status")
    async def status() -> JSONResponse:
        response = JSONResponse({**host.status(), "csrf_token": csrf_token})
        response.set_cookie(
            "coding_agent_session",
            session_token,
            httponly=True,
            samesite="strict",
            secure=False,
        )
        return response

    @app.get("/api/v1/sessions")
    async def sessions(
        limit: int = Query(default=30, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        return host.sessions(limit=limit, offset=offset)

    @app.get("/api/v1/workspaces")
    async def workspaces() -> dict[str, Any]:
        if not isinstance(host, WorkspaceManager):
            workspace = host.status()["workspace"]
            return {
                "active": workspace,
                "recent": [
                    {
                        "path": workspace,
                        "name": Path(workspace).name,
                        "active": True,
                        "available": True,
                    }
                ],
            }
        return host.workspaces()

    @app.post("/api/v1/workspaces/open")
    async def open_workspace(body: WorkspacePathRequest) -> dict[str, Any]:
        if not isinstance(host, WorkspaceManager):
            raise CodingAgentError("WORKSPACE_FIXED", "This Host uses a fixed workspace.")
        return await host.open_workspace(body.path)

    @app.post("/api/v1/workspaces/remove")
    async def remove_workspace(body: WorkspacePathRequest) -> dict[str, Any]:
        if not isinstance(host, WorkspaceManager):
            raise CodingAgentError("WORKSPACE_FIXED", "This Host uses a fixed workspace.")
        return host.remove_recent_workspace(body.path)

    @app.get("/api/v1/settings")
    async def settings() -> dict[str, Any]:
        return host.settings()

    @app.post("/api/v1/settings")
    async def update_settings(body: SettingsUpdate) -> dict[str, Any]:
        return await host.update_settings(body.model_dump())

    @app.post("/api/v1/sessions", status_code=201)
    async def create_session() -> dict[str, Any]:
        return await host.create_session()

    @app.post("/api/v1/sessions/{session_id}/select")
    async def select_session(session_id: str) -> dict[str, Any]:
        return await host.select_session(session_id)

    @app.get("/api/v1/sessions/{session_id}")
    async def session(
        session_id: str,
        limit: int = Query(default=30, ge=1, le=100),
        before_turn_number: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        return host.turns(
            session_id,
            limit=limit,
            before_turn_number=before_turn_number,
        )

    @app.get("/api/v1/sessions/{session_id}/turns")
    async def turns(
        session_id: str,
        limit: int = Query(default=30, ge=1, le=100),
        before_turn_number: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        return host.turns(
            session_id,
            limit=limit,
            before_turn_number=before_turn_number,
        )

    @app.get("/api/v1/sessions/{session_id}/context")
    async def context(session_id: str) -> dict[str, Any]:
        return host.context(session_id)

    @app.get("/api/v1/sessions/{session_id}/turns/{turn_number}/context")
    async def turn_context(session_id: str, turn_number: int) -> dict[str, Any]:
        return host.turn_context(session_id, turn_number)

    @app.post("/api/v1/sessions/{session_id}/context/compact", status_code=202)
    async def compact_context(
        session_id: str,
        body: CompactContextRequest,
    ) -> dict[str, Any]:
        return host.submit_context_compaction(
            session_id=session_id,
            **body.model_dump(),
        )

    @app.post("/api/v1/sessions/{session_id}/turns", status_code=202)
    async def create_turn(session_id: str, body: TurnRequest) -> dict[str, Any]:
        return host.submit_turn(session_id=session_id, **body.model_dump())

    @app.get("/api/v1/sessions/{session_id}/subagent-runs")
    async def subagent_runs(session_id: str) -> dict[str, Any]:
        return {"runs": host.subagent_runs(session_id)}

    @app.post("/api/v1/subagent-tasks/{task_id}/cancel")
    async def cancel_subagent_task(task_id: str) -> dict[str, Any]:
        return host.cancel_subagent_task(task_id)

    @app.post("/api/v1/sessions/{session_id}/restore", status_code=202)
    async def restore(session_id: str, body: RestoreRequest) -> dict[str, Any]:
        return host.submit_restore(session_id=session_id, **body.model_dump())

    @app.post("/api/v1/approvals/{approval_id}/decision")
    async def approval(approval_id: str, body: ApprovalDecision) -> dict[str, Any]:
        return host.resolve_approval(approval_id, body.model_dump())

    @app.get("/api/v1/operations/{operation_id}/events")
    async def operation_events(
        operation_id: str,
        request: Request,
        last_event_id: str | None = Header(default=None),
    ) -> StreamingResponse:
        if host.journal.operation(operation_id) is None:
            return StreamingResponse(
                iter([_sse(0, "operation.failed", {"error": {"code": "NOT_FOUND"}})]),
                media_type="text/event-stream",
                status_code=404,
            )
        try:
            cursor = max(
                0,
                int(last_event_id or request.query_params.get("after", "0")),
            )
        except ValueError:
            cursor = 0

        async def generate() -> AsyncIterator[str]:
            nonlocal cursor
            quiet_ticks = 0
            while True:
                if await request.is_disconnected():
                    return
                events = host.journal.events_after(operation_id, cursor)
                for event in events:
                    cursor = event["sequence"]
                    yield _sse(cursor, event["event_type"], event["payload"])
                    quiet_ticks = 0
                operation = host.journal.operation(operation_id)
                if operation and operation["status"] in {
                    "completed",
                    "failed",
                    "cancelled",
                    "recovery_required",
                }:
                    return
                quiet_ticks += 1
                if quiet_ticks >= 60:
                    yield ": heartbeat\n\n"
                    quiet_ticks = 0
                await asyncio.sleep(0.25)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v1/operations/{operation_id}/cancel")
    async def cancel_operation(operation_id: str) -> dict[str, Any]:
        return host.cancel_operation(operation_id)

    @app.get("/api/v1/workspace/status")
    async def workspace_status() -> dict[str, Any]:
        return host.workspace_status()

    @app.get("/api/v1/workspace/diff")
    async def workspace_diff(path: str | None = None) -> dict[str, Any]:
        return host.diff(path)

    @app.get("/api/v1/workspace/files")
    async def workspace_files() -> dict[str, Any]:
        return {"files": host.files()}

    @app.get("/api/v1/workspace/files/content")
    async def workspace_file(path: str) -> dict[str, Any]:
        return host.file_content(path)

    @app.get("/api/v1/turns/{turn_id}/trace")
    async def trace(turn_id: str) -> dict[str, Any]:
        return host.trace(turn_id)

    @app.get("/api/v1/artifacts/{artifact_id}")
    async def artifact(artifact_id: str) -> FileResponse:
        path, media_type = host.artifact(artifact_id)
        return FileResponse(path, media_type=media_type, filename=artifact_id)

    if static_dir and static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")

    return app


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "request_id": secrets.token_hex(8)}},
        status_code=status,
    )


def _sse(sequence: int, event_type: str, payload: dict[str, Any]) -> str:
    return (
        f"id: {sequence}\nevent: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )
