"""Workspace-scoped LangChain tools."""

from __future__ import annotations

import hashlib
import os
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool

from coding_agent.config import AgentConfig
from coding_agent.errors import CodingAgentError
from coding_agent.execution import ToolRunManager
from coding_agent.models import CommandRequest
from coding_agent.sandbox import SandboxExecutionService
from coding_agent.tracing.context import active_recorder
from coding_agent.workspace.mutation import acquire_workspace_mutation
from coding_agent.workspace.patch import ApplyPatchInput, Operation, PatchService
from coding_agent.workspace.paths import PathGuard


def _error(exc: Exception) -> dict[str, object]:
    if isinstance(exc, CodingAgentError):
        return {"ok": False, "error_code": exc.code, "message": exc.user_message}
    return {"ok": False, "error_code": "TOOL_ERROR", "message": str(exc)}


def build_tools(
    workspace_root: Path,
    repo_root: Path,
    config: AgentConfig,
    tool_runs: ToolRunManager,
    execution_service: SandboxExecutionService,
) -> list[BaseTool]:
    """Create tools closed over one immutable workspace."""
    guard = PathGuard(workspace_root)
    patcher = PatchService(guard)

    @tool
    def list_files(path: str = ".", depth: int = 2, limit: int = 500) -> dict[str, object]:
        """List files and directories under a workspace-relative path."""
        try:
            root = guard.resolve_for_read(path)
            if not root.is_dir():
                raise ValueError(f"Not a directory: {path}")
            base_depth = len(root.parts)
            items: list[dict[str, object]] = []
            for current, directories, files in os.walk(root):
                current_path = Path(current)
                current_depth = len(current_path.parts) - base_depth
                directories[:] = [
                    name for name in directories if name != ".git" and current_depth < depth
                ]
                for name in [*directories, *files]:
                    candidate = current_path / name
                    relative = guard.relative(candidate)
                    try:
                        resolved = candidate.resolve(strict=True)
                        if not resolved.is_relative_to(workspace_root):
                            continue
                        size = candidate.stat().st_size if candidate.is_file() else None
                    except OSError:
                        continue
                    items.append(
                        {
                            "path": relative,
                            "type": "directory" if candidate.is_dir() else "file",
                            "size": size,
                        }
                    )
                    if len(items) >= min(max(limit, 1), 2000):
                        return {"ok": True, "items": items, "truncated": True}
            return {"ok": True, "items": items, "truncated": False}
        except Exception as exc:
            return _error(exc)

    @tool
    def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> dict[str, object]:
        """Read at most 500 numbered lines from a UTF-8 workspace file."""
        try:
            target = guard.resolve_for_read(path)
            if not target.is_file():
                raise ValueError(f"Not a file: {path}")
            content = target.read_bytes()
            if len(content) > config.max_read_bytes:
                raise CodingAgentError("FILE_TOO_LARGE", f"File exceeds read limit: {path}")
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CodingAgentError(
                    "BINARY_OR_NON_UTF8_FILE", f"File is not UTF-8 text: {path}"
                ) from exc
            lines = text.splitlines()
            final_line = min(end_line or start_line + 499, start_line + 499, len(lines))
            selected = [
                {"line": number, "content": lines[number - 1]}
                for number in range(start_line, final_line + 1)
            ]
            return {
                "ok": True,
                "path": path,
                "lines": selected,
                "total_lines": len(lines),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        except Exception as exc:
            return _error(exc)

    @tool
    def glob_files(pattern: str, limit: int = 500) -> dict[str, object]:
        """Find workspace-relative paths matching a glob pattern."""
        try:
            matches: list[str] = []
            for candidate in workspace_root.glob(pattern):
                if ".git" in candidate.parts:
                    continue
                resolved = candidate.resolve(strict=True)
                if resolved.is_relative_to(workspace_root):
                    matches.append(guard.relative(candidate))
                if len(matches) >= min(max(limit, 1), 2000):
                    break
            return {"ok": True, "paths": sorted(matches), "truncated": len(matches) >= limit}
        except Exception as exc:
            return _error(exc)

    @tool
    def search_code(
        query: str,
        path: str = ".",
        include: str | None = None,
        regex: bool = False,
        max_results: int = 200,
    ) -> dict[str, object]:
        """Search code with ripgrep and return bounded matching lines."""
        try:
            target = guard.resolve_for_read(path)
            argv = ["rg", "--line-number", "--no-heading", "--color=never"]
            for excluded in (
                ".env*",
                "**/.env*",
                "*.pem",
                "**/*.pem",
                "*.key",
                "**/*.key",
                "*credentials*",
                "**/*credentials*",
            ):
                argv.extend(["--glob", f"!{excluded}"])
            if not regex:
                argv.append("--fixed-strings")
            if include:
                argv.extend(["--glob", include])
            argv.extend(["--", query, str(target)])
            result = subprocess.run(argv, capture_output=True, check=False)
            if result.returncode not in {0, 1}:
                raise ValueError(result.stderr.decode(errors="replace"))
            lines = result.stdout.decode(errors="replace").splitlines()[:max_results]
            normalized = [line.replace(str(workspace_root) + "/", "", 1) for line in lines]
            return {"ok": True, "matches": normalized, "truncated": len(lines) >= max_results}
        except Exception as exc:
            return _error(exc)

    @tool(args_schema=ApplyPatchInput)
    def apply_patch(operations: list[Operation]) -> dict[str, object]:
        """Apply a small text patch using expected hashes.

        Keep one call focused on one file and preferably below 12,000 argument
        characters. Calls above 32,000 characters or adding multiple files are
        rejected. Create large files incrementally with sequential calls.
        """
        try:
            acquire_workspace_mutation()
            return {"ok": True, **patcher.apply(ApplyPatchInput(operations=operations))}
        except Exception as exc:
            return _error(exc)

    @tool
    def workspace_status() -> dict[str, object]:
        """Show concise Git status for the repository."""
        result = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--short", "--", str(workspace_root)],
            capture_output=True,
            check=False,
        )
        artifact_id = None
        if recorder := active_recorder():
            with suppress(Exception):
                artifact_id = recorder.capture_text_artifact(result.stdout)
        return {
            "ok": result.returncode == 0,
            "status": result.stdout.decode(errors="replace")[:200_000],
            "artifact_id": artifact_id,
        }

    @tool
    def show_diff() -> dict[str, object]:
        """Show the current unstaged Git diff for the workspace."""
        result = subprocess.run(
            ["git", "-C", str(repo_root), "diff", "--no-ext-diff", "--", str(workspace_root)],
            capture_output=True,
            check=False,
        )
        value = result.stdout[:200_000]
        artifact_id = None
        if recorder := active_recorder():
            with suppress(Exception):
                artifact_id = recorder.capture_text_artifact(result.stdout, "text/x-diff")
        return {
            "ok": result.returncode == 0,
            "diff": value.decode(errors="replace"),
            "truncated": len(result.stdout) > len(value),
            "artifact_id": artifact_id,
        }

    @tool
    def run_command(
        argv: list[str],
        cwd: str = ".",
        timeout_seconds: int | None = None,
        execution_group_id: str | None = None,
    ) -> dict[str, Any]:
        """Start an approved command in the background and return its run ID."""
        try:
            request = CommandRequest(
                argv=argv,
                cwd=cwd,
                timeout_seconds=timeout_seconds or config.command_timeout_seconds,
            )
            recorder = active_recorder()
            acquire_workspace_mutation()
            return tool_runs.start_current(
                name="run_command",
                effect="workspace_write",
                execution_group_id=execution_group_id,
                work=lambda cancel_event, on_output: execution_service.run(
                    request,
                    cancel_event=cancel_event,
                    on_output=on_output,
                    recorder=recorder,
                ).model_dump(),
            )
        except Exception as exc:
            return _error(exc)

    return [
        list_files,
        read_file,
        glob_files,
        search_code,
        apply_patch,
        workspace_status,
        show_diff,
        run_command,
    ]
