from pathlib import Path
from typing import Annotated, Any, TypedDict, cast

import pytest
from fastmcp import FastMCP
from fastmcp.utilities.tests import run_server_in_process
from langchain_core.messages import AIMessage, AnyMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import ValidationError

from coding_agent.config import AgentConfig, MCPServerConfig, load_config
from coding_agent.errors import CodingAgentError
from coding_agent.execution import ToolRunManager, activate_tool_turn
from coding_agent.mcp import LazyMCPToolProvider, MCPToolProvider


class _ToolState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def _run_calculator_server(host: str, port: int) -> None:
    server: FastMCP[None] = FastMCP("calculator")

    @server.tool
    def add(left: int, right: int) -> int:
        """Add two integers."""
        return left + right

    server.run(
        transport="http",
        host=host,
        port=port,
        show_banner=False,
        log_level="warning",
    )


def test_load_config_accepts_static_http_mcp_servers(tmp_path: Path, monkeypatch: Any) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"model":"test","mcp_servers":{"docs":{"url":"https://example.com/mcp"}}}'
    )
    monkeypatch.setenv("OPENAI_API_KEY", "model-key")

    config = load_config(model=None, base_url=None, config_path=config_path)

    assert str(config.mcp_servers["docs"].url) == "https://example.com/mcp"
    assert config.mcp_servers["docs"].timeout_seconds == 30


@pytest.mark.parametrize(
    "servers",
    [
        {"bad name": {"url": "https://example.com/mcp"}},
        {"docs": {"url": "file:///tmp/server.py"}},
        {"docs": {"url": "https://example.com/mcp", "timeout_seconds": 0}},
        {
            "docs": {
                "url": "https://example.com/mcp",
                "headers_from_env": {"Bad Header": "TOKEN"},
            }
        },
        {
            "docs": {
                "url": "https://example.com/mcp",
                "headers_from_env": {"Authorization": "bad-name"},
            }
        },
    ],
)
def test_mcp_config_rejects_unsafe_server_definitions(servers: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AgentConfig(model="test", api_key="model-key", mcp_servers=servers)


def test_http_mcp_tools_are_namespaced_and_support_sync_invocation() -> None:
    with run_server_in_process(_run_calculator_server) as url:
        server = MCPServerConfig.model_validate({"url": f"{url}/mcp"})
        provider = MCPToolProvider({"calc": server})
        try:
            [tool] = provider.tools
            builder = StateGraph(_ToolState)
            builder.add_node("tools", ToolNode([tool]))
            builder.add_edge(START, "tools")
            builder.add_edge("tools", END)
            graph = builder.compile()

            result = graph.invoke(
                {
                    "messages": [
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "calc_add",
                                    "args": {"left": 20, "right": 22},
                                    "id": "call-1",
                                    "type": "tool_call",
                                }
                            ],
                        )
                    ]
                }
            )
            message = result["messages"][-1]

            assert tool.name == "calc_add"
            assert message.content[0]["type"] == "text"
            assert message.content[0]["text"] == "42"
            assert message.artifact == {"structured_content": {"result": 42}}
            assert tool.metadata is not None
            assert tool.metadata["mcp"]["server"]["name"] == "calculator"
        finally:
            provider.close()


def test_empty_mcp_config_does_not_start_a_bridge() -> None:
    provider = MCPToolProvider({})

    assert provider.tools == []
    provider.close()


def test_configured_mcp_connects_only_when_activated() -> None:
    with run_server_in_process(_run_calculator_server) as url:
        server = MCPServerConfig.model_validate({"url": f"{url}/mcp"})
        runs = ToolRunManager(max_workers=1)
        provider = LazyMCPToolProvider({"calc": server}, runs)
        try:
            assert [item.name for item in provider.tools] == [
                "activate_configured_mcp",
                "call_configured_mcp",
            ]
            activated = provider.activate()
            tools = cast("list[dict[str, object]]", activated["tools"])
            assert tools[0]["name"] == "calc_add"

            runs.begin_turn("thread")
            with activate_tool_turn("thread", None):
                started = provider.tools[1].invoke(
                    {
                        "tool_name": "calc_add",
                        "arguments": {"left": 20, "right": 22},
                    }
                )
            assert runs.drain_thread("thread", timeout_seconds=3)
            result = runs.inspect("thread", str(started["run_id"]))
            assert result["status"] == "completed"
            assert result["result"]["result"][0]["text"] == "42"
        finally:
            runs.close()
            provider.close()


def test_lazy_mcp_enforces_exact_tool_grants() -> None:
    with run_server_in_process(_run_calculator_server) as url:
        server = MCPServerConfig.model_validate({"url": f"{url}/mcp"})
        runs = ToolRunManager(max_workers=1)
        provider = LazyMCPToolProvider(
            {"calc": server},
            runs,
            allowed_tool_names=frozenset(),
        )
        try:
            assert provider.activate()["tools"] == []
            with pytest.raises(CodingAgentError) as exc_info:
                provider.call("calc_add", {"left": 20, "right": 22})
            assert exc_info.value.code == "MCP_TOOL_FORBIDDEN"
        finally:
            runs.close()
            provider.close()


def test_mcp_provider_requires_declared_header_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_MCP_TOKEN", raising=False)
    server = MCPServerConfig.model_validate(
        {
            "url": "https://example.com/mcp",
            "headers_from_env": {"Authorization": "MISSING_MCP_TOKEN"},
        }
    )

    with pytest.raises(CodingAgentError) as exc_info:
        MCPToolProvider({"docs": server})

    assert exc_info.value.code == "MCP_CREDENTIAL_MISSING"
