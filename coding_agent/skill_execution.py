"""Session-scoped execution for manifest-declared Skill capabilities."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, cast

from langchain_core.tools import BaseTool, tool

from coding_agent.config import AgentConfig
from coding_agent.errors import CodingAgentError, fail
from coding_agent.execution import CommandPolicy, ToolRunManager
from coding_agent.mcp import MCPToolProvider
from coding_agent.models import CommandRequest
from coding_agent.sandbox import SandboxExecutionService
from coding_agent.skills import Skill, SkillRegistry
from coding_agent.tracing.context import active_recorder

_active_thread_id: ContextVar[str | None] = ContextVar("active_skill_thread_id", default=None)


@contextmanager
def activate_skill_thread(thread_id: str) -> Generator[None]:
    """Expose the current LangGraph thread to Skill tools without model input."""
    token = _active_thread_id.set(thread_id)
    try:
        yield
    finally:
        _active_thread_id.reset(token)


class SkillExecutionManager:
    """Authorize and execute one Skill's declared capabilities per thread."""

    def __init__(
        self,
        registry: SkillRegistry,
        *,
        workspace_root: Path,
        config: AgentConfig,
        execution_service: SandboxExecutionService,
        tool_runs: ToolRunManager | None = None,
    ) -> None:
        self.registry = registry
        self.workspace_root = workspace_root.resolve(strict=True)
        self._policy = CommandPolicy()
        self._execution_service = execution_service
        self._tool_runs = tool_runs or ToolRunManager(
            max_output_bytes=config.max_command_output_bytes
        )
        self._owns_tool_runs = tool_runs is None
        self._authorized: dict[str, set[str]] = {}
        self._mcp_providers: dict[tuple[str, str], MCPToolProvider] = {}
        self._lock = threading.RLock()

        @tool
        def activate_skill(name: str) -> dict[str, object]:
            """Enable an executable Skill after the host approves it for this session."""
            return self._tool_result(lambda: self.activate(name))

        @tool
        def run_skill_command(
            skill_name: str,
            command_name: str,
            extra_args: list[str] | None = None,
            execution_group_id: str | None = None,
        ) -> dict[str, object]:
            """Start a Skill command in the background and return its run ID."""
            return self._tool_result(
                lambda: self.start_command(
                    skill_name,
                    command_name,
                    extra_args or [],
                    execution_group_id=execution_group_id,
                )
            )

        @tool
        def call_skill_mcp(
            skill_name: str,
            tool_name: str,
            arguments: dict[str, Any] | None = None,
            execution_group_id: str | None = None,
        ) -> dict[str, object]:
            """Start one activated Skill MCP call in the background."""
            return self._tool_result(
                lambda: self.start_mcp(
                    skill_name,
                    tool_name,
                    arguments or {},
                    execution_group_id=execution_group_id,
                )
            )

        self.tools: list[BaseTool] = [
            activate_skill,
            run_skill_command,
            call_skill_mcp,
        ]

    def authorize(self, thread_id: str, skill_name: str) -> None:
        """Remember user approval for one Skill in one LangGraph thread."""
        skill = self.registry.get(skill_name)
        if not skill.has_executable_capabilities:
            raise fail(
                "SKILL_NOT_EXECUTABLE",
                f"Skill has no executable capabilities: {skill_name}",
            )
        with self._lock:
            self._authorized.setdefault(thread_id, set()).add(skill_name)

    def is_authorized(self, thread_id: str, skill_name: str) -> bool:
        with self._lock:
            return skill_name in self._authorized.get(thread_id, set())

    def activation_summary(self, skill_name: str) -> dict[str, object]:
        """Describe executable capabilities for an informed approval prompt."""
        skill = self.registry.get(skill_name)
        commands = []
        for name, command in skill.manifest.commands.items():
            command_spec = (
                {
                    "script": command.script,
                    "interpreter": command.interpreter,
                }
                if command.script is not None
                else {"argv": command.argv}
            )
            commands.append(
                {
                    "name": name,
                    "description": command.description,
                    **command_spec,
                    "allow_args": command.allow_args,
                    "environment": sorted(command.env_from_env.values()),
                }
            )
        mcp_servers = [
            {
                "name": name,
                "url": str(server.url),
                "environment": sorted(server.headers_from_env.values()),
            }
            for name, server in skill.manifest.mcp_servers.items()
        ]
        return {
            "skill": skill.name,
            "source": skill.source,
            "commands": commands,
            "mcp_servers": mcp_servers,
        }

    def activate(self, skill_name: str) -> dict[str, object]:
        """Connect declared MCP servers and describe available executable capabilities."""
        thread_id = self._require_thread()
        skill = self.registry.get(skill_name)
        self._require_authorized(thread_id, skill)
        key = (thread_id, skill.name)
        with self._lock:
            if skill.manifest.mcp_servers and key not in self._mcp_providers:
                self._mcp_providers[key] = MCPToolProvider(skill.manifest.mcp_servers)
            provider = self._mcp_providers.get(key)
        return {
            "ok": True,
            "skill": skill.name,
            "commands": {
                name: command.description
                for name, command in skill.manifest.commands.items()
            },
            "mcp_tools": self._describe_mcp_tools(provider),
        }

    def run_command(
        self,
        skill_name: str,
        command_name: str,
        args: list[str],
    ) -> dict[str, object]:
        """Execute one approved manifest command without a shell."""
        thread_id = self._require_thread()
        request, environment = self._prepare_command(thread_id, skill_name, command_name, args)
        result = self._execution_service.run(request, extra_env=environment)
        return {
            "ok": True,
            "skill": skill_name,
            "command": command_name,
            **result.model_dump(),
        }

    def start_command(
        self,
        skill_name: str,
        command_name: str,
        args: list[str],
        *,
        execution_group_id: str | None,
    ) -> dict[str, object]:
        """Validate and submit one Skill command to the turn-scoped runner."""
        thread_id = self._require_thread()
        request, environment = self._prepare_command(thread_id, skill_name, command_name, args)
        recorder = active_recorder()
        return self._tool_runs.start_current(
            name=f"skill:{skill_name}.{command_name}",
            effect="workspace_write",
            execution_group_id=execution_group_id,
            work=lambda cancel_event, on_output: {
                "ok": True,
                "skill": skill_name,
                "command": command_name,
                **self._execution_service.run(
                    request,
                    extra_env=environment,
                    cancel_event=cancel_event,
                    on_output=on_output,
                    recorder=recorder,
                ).model_dump(),
            },
        )

    def _prepare_command(
        self,
        thread_id: str,
        skill_name: str,
        command_name: str,
        args: list[str],
    ) -> tuple[CommandRequest, dict[str, str]]:
        skill = self.registry.get(skill_name)
        self._require_authorized(thread_id, skill)
        command = skill.manifest.commands.get(command_name)
        if command is None:
            raise fail(
                "SKILL_COMMAND_NOT_FOUND",
                f"Skill {skill_name} does not declare command: {command_name}",
            )
        if args and not command.allow_args:
            raise fail(
                "SKILL_ARGUMENTS_DENIED",
                f"Skill command does not accept additional arguments: {command_name}",
            )
        prefix = self._command_prefix(skill, command_name)
        request = CommandRequest(
            argv=[*prefix, *args],
            cwd=".",
            timeout_seconds=command.timeout_seconds,
        )
        self._policy.validate(request)
        environment = self._resolve_environment(
            command.env_from_env,
            capability=f"{skill_name}.{command_name}",
        )
        return request, environment

    def call_mcp(
        self,
        skill_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, object]:
        """Invoke an MCP tool from an activated Skill provider."""
        thread_id = self._require_thread()
        skill = self.registry.get(skill_name)
        self._require_authorized(thread_id, skill)
        with self._lock:
            provider = self._mcp_providers.get((thread_id, skill_name))
        if provider is None:
            raise fail(
                "SKILL_NOT_ACTIVE",
                f"Activate Skill {skill_name} before calling its MCP tools.",
            )
        remote_tool = next((item for item in provider.tools if item.name == tool_name), None)
        if remote_tool is None:
            raise fail(
                "SKILL_MCP_TOOL_NOT_FOUND",
                f"Skill {skill_name} does not expose MCP tool: {tool_name}",
            )
        return {
            "ok": True,
            "skill": skill_name,
            "tool": tool_name,
            "result": remote_tool.invoke(arguments),
        }

    def start_mcp(
        self,
        skill_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        execution_group_id: str | None,
    ) -> dict[str, object]:
        """Submit an MCP call while restoring its thread context in the worker."""
        thread_id = self._require_thread()
        skill = self.registry.get(skill_name)
        self._require_authorized(thread_id, skill)
        return self._tool_runs.start_current(
            name=f"skill-mcp:{skill_name}.{tool_name}",
            effect="external",
            execution_group_id=execution_group_id,
            work=lambda _cancel_event, _on_output: self._call_mcp_in_thread(
                thread_id,
                skill_name,
                tool_name,
                arguments,
            ),
        )

    def close(self) -> None:
        """Close all lazily-created MCP providers."""
        with self._lock:
            providers = list(self._mcp_providers.values())
            self._mcp_providers.clear()
            self._authorized.clear()
        for provider in providers:
            provider.close()
        if self._owns_tool_runs:
            self._tool_runs.close()

    def _call_mcp_in_thread(
        self,
        thread_id: str,
        skill_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, object]:
        with activate_skill_thread(thread_id):
            return self.call_mcp(skill_name, tool_name, arguments)

    def _command_prefix(self, skill: Skill, command_name: str) -> list[str]:
        command = skill.manifest.commands[command_name]
        if command.script is not None:
            script_root = skill.path.parent.resolve(strict=True)
            try:
                script_path = (script_root / command.script).resolve(strict=True)
            except OSError as exc:
                raise fail(
                    "SKILL_SCRIPT_NOT_FOUND",
                    f"Cannot resolve script for {skill.name}.{command_name}: {exc}",
                ) from exc
            if not script_path.is_file() or not script_path.is_relative_to(script_root):
                raise fail(
                    "SKILL_SCRIPT_OUTSIDE_ROOT",
                    f"Script must stay inside the Skill directory: {command.script}",
                )
            return [command.interpreter or "", str(script_path)]
        if command.argv is None:
            raise RuntimeError("Validated command has neither argv nor script.")
        replacements = {
            "{skill_dir}": str(skill.path.parent),
            "{workspace}": str(self.workspace_root),
        }
        return [
            _replace_placeholders(argument, replacements)
            for argument in command.argv
        ]

    @staticmethod
    def _resolve_environment(mapping: dict[str, str], *, capability: str) -> dict[str, str]:
        missing = [source for source in mapping.values() if source not in os.environ]
        if missing:
            raise fail(
                "SKILL_CREDENTIAL_MISSING",
                f"Skill capability {capability} requires environment variables: "
                f"{', '.join(sorted(missing))}",
            )
        return {target: os.environ[source] for target, source in mapping.items()}

    @staticmethod
    def _describe_mcp_tools(provider: MCPToolProvider | None) -> list[dict[str, object]]:
        if provider is None:
            return []
        descriptions: list[dict[str, object]] = []
        for remote_tool in provider.tools:
            tool_schema = remote_tool.tool_call_schema
            schema = (
                tool_schema
                if isinstance(tool_schema, dict)
                else cast("Any", tool_schema).model_json_schema()
            )
            descriptions.append(
                {
                    "name": remote_tool.name,
                    "description": remote_tool.description,
                    "input_schema": schema,
                }
            )
        return descriptions

    def _require_authorized(self, thread_id: str, skill: Skill) -> None:
        if not self.is_authorized(thread_id, skill.name):
            raise fail(
                "SKILL_APPROVAL_REQUIRED",
                f"Skill {skill.name} must be activated with user approval first.",
            )

    @staticmethod
    def _require_thread() -> str:
        thread_id = _active_thread_id.get()
        if thread_id is None:
            raise fail("SKILL_CONTEXT_MISSING", "Skill execution requires an active session.")
        return thread_id

    @staticmethod
    def _tool_result(function: Callable[[], dict[str, object]]) -> dict[str, object]:
        try:
            return function()
        except CodingAgentError as exc:
            return {"ok": False, "error_code": exc.code, "message": exc.user_message}
        except Exception as exc:
            return {"ok": False, "error_code": "TOOL_ERROR", "message": str(exc)}


def _replace_placeholders(value: str, replacements: dict[str, str]) -> str:
    result = value
    for marker, replacement in replacements.items():
        result = result.replace(marker, replacement)
    return result
