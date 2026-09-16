"""Synchronous tool bridge for configured HTTP MCP servers."""

from __future__ import annotations

import asyncio
import os
import threading
import warnings
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, TypeVar, cast

from langchain_core._api import LangChainBetaWarning
from langchain_core.tools import BaseTool, StructuredTool, tool

from coding_agent.config import MCPServerConfig
from coding_agent.errors import CodingAgentError, fail

if TYPE_CHECKING:
    from coding_agent.execution.tool_runs import ToolRunManager

_ResultT = TypeVar("_ResultT")


async def _reject_elicitation(*_args: Any, **_kwargs: Any) -> Any:
    """Reject MCP features deliberately excluded from the tools-only MVP."""
    raise NotImplementedError("MCP elicitation is not supported by this client.")


class _AsyncBridge:
    """Run MCP coroutines on one private event loop from synchronous tools."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = threading.Event()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run_loop,
            name="coding-agent-mcp",
            daemon=True,
        )
        self._thread.start()
        self._started.wait()

    def call(self, coroutine: Coroutine[Any, Any, _ResultT]) -> _ResultT:
        """Wait synchronously for one coroutine on the bridge loop."""
        if self._closed or self._loop is None:
            coroutine.close()
            raise RuntimeError("MCP bridge is closed.")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result()

    def close(self) -> None:
        """Stop the private event loop after all synchronous calls finish."""
        if self._closed:
            return
        self._closed = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join()

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._started.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()


class MCPToolProvider:
    """Discover configured MCP tools and expose synchronous LangChain wrappers."""

    def __init__(self, servers: dict[str, MCPServerConfig]) -> None:
        self._bridge: _AsyncBridge | None = None
        self.tools: list[BaseTool] = []
        if not servers:
            return

        self._bridge = _AsyncBridge()
        try:
            self.tools = self._load_tools(servers)
        except CodingAgentError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise fail("MCP_CONNECTION_ERROR", f"Cannot load configured MCP tools: {exc}") from exc

    def _load_tools(self, servers: dict[str, MCPServerConfig]) -> list[BaseTool]:
        """Connect once for discovery and wrap every asynchronous MCP tool."""
        from fastmcp.client import Client
        from fastmcp.client.group import ClientGroup
        from fastmcp.client.transports import StreamableHttpTransport

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=LangChainBetaWarning)
            from langchain.mcp import MCPAdapter

        clients = {}
        for name, server in servers.items():
            missing = [
                environment_name
                for environment_name in server.headers_from_env.values()
                if environment_name not in os.environ
            ]
            if missing:
                raise fail(
                    "MCP_CREDENTIAL_MISSING",
                    f"MCP server {name} requires environment variables: "
                    f"{', '.join(sorted(missing))}",
                )
            headers = {
                header: os.environ[environment_name]
                for header, environment_name in server.headers_from_env.items()
            }
            transport = StreamableHttpTransport(str(server.url), headers=headers or None)
            clients[name] = Client(
                transport,
                elicitation_handler=_reject_elicitation,
                timeout=server.timeout_seconds,
                init_timeout=server.timeout_seconds,
            )
        adapter = MCPAdapter(ClientGroup(clients))
        if self._bridge is None:
            raise RuntimeError("MCP bridge was not initialized.")
        remote_tools = self._bridge.call(adapter.list_tools())
        return [self._synchronous_tool(tool) for tool in remote_tools]

    def _synchronous_tool(self, remote_tool: BaseTool) -> BaseTool:
        """Preserve an MCP tool schema and execute its coroutine through the bridge."""
        if not isinstance(remote_tool, StructuredTool) or remote_tool.coroutine is None:
            raise fail(
                "MCP_TOOL_ERROR",
                f"MCP tool {remote_tool.name!r} does not expose an asynchronous implementation.",
            )
        coroutine = cast("Callable[..., Coroutine[Any, Any, Any]]", remote_tool.coroutine)
        bridge = self._bridge
        if bridge is None:
            raise RuntimeError("MCP bridge was not initialized.")

        def invoke(**arguments: Any) -> Any:
            return bridge.call(coroutine(**arguments))

        return StructuredTool(
            name=remote_tool.name,
            description=remote_tool.description,
            args_schema=remote_tool.args_schema,
            func=invoke,
            response_format=remote_tool.response_format,
            metadata=remote_tool.metadata,
            tags=remote_tool.tags,
            handle_tool_error=remote_tool.handle_tool_error,
            handle_validation_error=remote_tool.handle_validation_error,
        )

    def close(self) -> None:
        """Release the MCP bridge; individual tool calls close their connections."""
        if self._bridge is not None:
            self._bridge.close()
            self._bridge = None


class LazyMCPToolProvider:
    """Expose configured MCP through generic tools and connect only on demand."""

    def __init__(
        self,
        servers: dict[str, MCPServerConfig],
        tool_runs: ToolRunManager,
        allowed_tool_names: frozenset[str] | None = None,
    ) -> None:
        self._servers = dict(servers)
        self._tool_runs = tool_runs
        self._allowed_tool_names = allowed_tool_names
        self._provider: MCPToolProvider | None = None
        self._lock = threading.RLock()

        @tool
        def activate_configured_mcp() -> dict[str, object]:
            """Connect configured MCP servers and list their available tools."""
            try:
                return self.activate()
            except CodingAgentError as exc:
                return {"ok": False, "error_code": exc.code, "message": exc.user_message}
            except Exception as exc:
                return {"ok": False, "error_code": "MCP_ERROR", "message": str(exc)}

        @tool
        def call_configured_mcp(
            tool_name: str,
            arguments: dict[str, Any] | None = None,
            execution_group_id: str | None = None,
        ) -> dict[str, object]:
            """Start one activated configured MCP tool in the background."""
            try:
                return self._tool_runs.start_current(
                    name=f"mcp:{tool_name}",
                    effect="external",
                    execution_group_id=execution_group_id,
                    work=lambda _cancel_event, _on_output: self.call(
                        tool_name,
                        arguments or {},
                    ),
                )
            except CodingAgentError as exc:
                return {"ok": False, "error_code": exc.code, "message": exc.user_message}
            except Exception as exc:
                return {"ok": False, "error_code": "MCP_ERROR", "message": str(exc)}

        self.tools: list[BaseTool] = (
            [activate_configured_mcp, call_configured_mcp] if self._servers else []
        )

    def activate(self) -> dict[str, object]:
        """Create the provider once and return stable tool descriptions."""
        with self._lock:
            if self._provider is None:
                self._provider = MCPToolProvider(self._servers)
            provider = self._provider
        return {
            "ok": True,
            "tools": [
                self._describe_tool(remote_tool)
                for remote_tool in provider.tools
                if self._is_allowed(remote_tool.name)
            ],
        }

    def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, object]:
        """Invoke one previously discovered remote tool."""
        with self._lock:
            provider = self._provider
        if provider is None:
            raise fail(
                "MCP_NOT_ACTIVE",
                "Call activate_configured_mcp before using configured MCP tools.",
            )
        if not self._is_allowed(tool_name):
            raise fail(
                "MCP_TOOL_FORBIDDEN",
                f"Configured MCP tool is not granted to this Agent: {tool_name}",
            )
        remote_tool = next((item for item in provider.tools if item.name == tool_name), None)
        if remote_tool is None:
            raise fail("MCP_TOOL_NOT_FOUND", f"Configured MCP tool does not exist: {tool_name}")
        return {
            "ok": True,
            "tool": tool_name,
            "result": remote_tool.invoke(arguments),
        }

    def close(self) -> None:
        """Close the lazily-created provider when it exists."""
        with self._lock:
            provider = self._provider
            self._provider = None
        if provider is not None:
            provider.close()

    def _is_allowed(self, tool_name: str) -> bool:
        return self._allowed_tool_names is None or tool_name in self._allowed_tool_names

    @staticmethod
    def _describe_tool(remote_tool: BaseTool) -> dict[str, object]:
        schema = remote_tool.tool_call_schema
        return {
            "name": remote_tool.name,
            "description": remote_tool.description,
            "input_schema": (
                schema if isinstance(schema, dict) else cast("Any", schema).model_json_schema()
            ),
        }
