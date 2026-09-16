import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from coding_agent.cli import _approve_request, _StreamingOutput, build_parser, build_web_parser
from coding_agent.config import AgentConfig, load_config, save_non_secret_config
from coding_agent.runtime import AgentRuntime, approve_by_default


class FakeStreamingGraph:
    def stream(self, *_args: Any, **_kwargs: Any) -> Any:
        assert _kwargs["stream_mode"] == ["messages", "values"]
        yield "messages", (AIMessageChunk(content="hello "), {})
        yield "messages", (AIMessageChunk(content="world"), {})
        yield "values", {"messages": [AIMessage(content="hello world")]}


def test_langsmith_is_disabled_without_credentials(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "model-key")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)
    monkeypatch.delenv("CODING_AGENT_LANGSMITH_ENABLED", raising=False)

    config = load_config(
        model="test-model",
        base_url=None,
        config_path=tmp_path / "missing.json",
    )

    assert not config.langsmith_enabled
    assert config.max_parallel_sessions == 4
    assert config.sandbox_provider == "seatbelt"
    assert config.sandbox_image is None


def test_docker_provider_requires_an_image() -> None:
    try:
        AgentConfig(
            model="test-model",
            api_key="model-key",
            sandbox_provider="docker",
        )
    except ValueError as exc:
        assert "sandbox_image is required" in str(exc)
    else:
        raise AssertionError("Docker configuration without an image must fail")


def test_docker_provider_remains_available_when_explicitly_configured() -> None:
    config = AgentConfig(
        model="test-model",
        api_key="model-key",
        sandbox_provider="docker",
        sandbox_image="coding-agent-sandbox:local",
    )

    assert config.sandbox_provider == "docker"
    assert config.sandbox_image == "coding-agent-sandbox:local"


def test_parallel_session_limit_can_be_configured_from_environment(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "model-key")
    monkeypatch.setenv("CODING_AGENT_MAX_PARALLEL_SESSIONS", "7")

    config = load_config(
        model="test-model",
        base_url=None,
        config_path=tmp_path / "missing.json",
    )

    assert config.max_parallel_sessions == 7


def test_langsmith_can_be_explicitly_enabled(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "model-key")
    monkeypatch.setenv("CODING_AGENT_LANGSMITH_ENABLED", "true")

    config = load_config(
        model="test-model",
        base_url=None,
        config_path=tmp_path / "missing.json",
    )

    assert config.langsmith_enabled


def test_langsmith_cli_switch_has_highest_priority(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "model-key")
    monkeypatch.setenv("CODING_AGENT_LANGSMITH_ENABLED", "false")

    enabled = load_config(
        model="test-model",
        base_url=None,
        config_path=tmp_path / "missing.json",
        langsmith_enabled=True,
    )
    disabled = load_config(
        model="test-model",
        base_url=None,
        config_path=tmp_path / "missing.json",
        langsmith_enabled=False,
    )

    assert enabled.langsmith_enabled
    assert not disabled.langsmith_enabled


def test_runtime_streams_message_chunks_and_keeps_final_state() -> None:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.graph = cast(Any, FakeStreamingGraph())
    chunks: list[str] = []

    result = runtime._stream_graph(
        {"messages": [{"role": "user", "content": "hi"}]},
        {"configurable": {"thread_id": "thread"}},
        chunks.append,
    )

    assert chunks == ["hello ", "world"]
    assert result["messages"][-1].content == "hello world"


def test_runtime_replies_to_invalid_tool_call_and_retries_model() -> None:
    class InvalidThenValidGraph:
        def __init__(self) -> None:
            self.values: list[Any] = []

        def stream(self, value: Any, *_args: Any, **_kwargs: Any) -> Any:
            self.values.append(value)
            if len(self.values) == 1:
                message = AIMessage(
                    content="partial",
                    invalid_tool_calls=[
                        {
                            "id": "invalid-call",
                            "name": "apply_patch",
                            "args": "{broken",
                            "error": None,
                        }
                    ],
                )
            else:
                message = AIMessage(content="recovered")
            yield "values", {"messages": [message]}

    runtime = AgentRuntime.__new__(AgentRuntime)
    graph = InvalidThenValidGraph()
    runtime.graph = cast(Any, graph)

    result = runtime._run_with_approvals(
        {"messages": [{"role": "user", "content": "change files"}]},
        {"configurable": {"thread_id": "thread"}},
        "thread",
        lambda _request: True,
        None,
        None,
    )

    retry_message = graph.values[1]["messages"][0]
    assert isinstance(retry_message, ToolMessage)
    assert retry_message.tool_call_id == "invalid-call"
    assert retry_message.status == "error"
    assert result["messages"][-1].content == "recovered"


def test_runtime_repairs_legacy_unanswered_invalid_tool_call() -> None:
    invalid = AIMessage(
        id="invalid-message",
        content="partial",
        invalid_tool_calls=[
            {
                "id": "invalid-call",
                "name": "apply_patch",
                "args": "{broken",
                "error": None,
            }
        ],
    )

    class LegacyGraph:
        def __init__(self) -> None:
            self.updates: list[dict[str, Any]] = []

        def get_state(self, _config: Any) -> Any:
            return SimpleNamespace(
                values={"messages": [invalid, HumanMessage(content="next turn")]}
            )

        def update_state(self, _config: Any, values: dict[str, Any]) -> None:
            self.updates.append(values)

    runtime = AgentRuntime.__new__(AgentRuntime)
    graph = LegacyGraph()
    runtime.graph = cast(Any, graph)

    runtime._repair_unresolved_invalid_tool_calls(
        {"configurable": {"thread_id": "thread"}}
    )

    replacement = graph.updates[0]["messages"][0]
    assert replacement.id == "invalid-message"
    assert replacement.invalid_tool_calls == []


def test_runtime_repairs_tool_calls_left_unanswered_by_cancellation() -> None:
    tool_call_message = AIMessage(
        content="",
        tool_calls=[
            {"id": "completed-call", "name": "read_file", "args": {"path": "README.md"}},
            {"id": "cancelled-call", "name": "run_command", "args": {"argv": ["pytest"]}},
        ],
    )
    completed = ToolMessage(content='{"ok": true}', tool_call_id="completed-call")

    class InterruptedGraph:
        def __init__(self) -> None:
            self.updates: list[dict[str, Any]] = []

        def get_state(self, _config: Any) -> Any:
            return SimpleNamespace(values={"messages": [tool_call_message, completed]})

        def update_state(self, _config: Any, values: dict[str, Any]) -> None:
            self.updates.append(values)

    runtime = AgentRuntime.__new__(AgentRuntime)
    graph = InterruptedGraph()
    runtime.graph = cast(Any, graph)

    runtime._repair_unresolved_tool_calls(
        {"configurable": {"thread_id": "thread"}}
    )

    responses = graph.updates[0]["messages"]
    assert len(responses) == 1
    assert responses[0].tool_call_id == "cancelled-call"
    assert responses[0].status == "error"
    assert "TOOL_CALL_INTERRUPTED" in responses[0].content


def test_runtime_removes_historical_unanswered_call_after_failed_retry() -> None:
    interrupted = AIMessage(
        id="interrupted-message",
        content="",
        tool_calls=[
            {"id": "cancelled-call", "name": "run_command", "args": {"argv": ["pytest"]}}
        ],
    )

    class FailedRetryGraph:
        def __init__(self) -> None:
            self.updates: list[dict[str, Any]] = []

        def get_state(self, _config: Any) -> Any:
            return SimpleNamespace(
                values={
                    "messages": [
                        interrupted,
                        HumanMessage(content="continue after cancellation"),
                    ]
                }
            )

        def update_state(self, _config: Any, values: dict[str, Any]) -> None:
            self.updates.append(values)

    runtime = AgentRuntime.__new__(AgentRuntime)
    graph = FailedRetryGraph()
    runtime.graph = cast(Any, graph)

    runtime._repair_unresolved_tool_calls(
        {"configurable": {"thread_id": "thread"}}
    )

    replacement = graph.updates[0]["messages"][0]
    assert replacement.id == "interrupted-message"
    assert replacement.tool_calls == []


def test_commands_are_approved_by_default() -> None:
    assert approve_by_default({"name": "run_command", "args": {"argv": ["pytest"]}})


def test_cli_prompts_for_skill_activation(capsys: Any) -> None:
    class Reader:
        def read(self, prompt: str) -> str:
            assert "Skill alpha" in prompt
            return "yes"

    request = {
        "name": "activate_skill",
        "args": {"name": "alpha"},
        "skill_capabilities": {"commands": [], "mcp_servers": []},
    }

    assert _approve_request(Reader(), request)  # type: ignore[arg-type]
    assert '"commands": []' in capsys.readouterr().out


def test_streaming_output_does_not_repeat_final_response(capsys: Any) -> None:
    output = _StreamingOutput()
    output.write("hello ")
    output.write("world")
    output.finish("hello world")

    assert capsys.readouterr().out == "\nagent> hello world\n"


def test_langsmith_cli_flags() -> None:
    parser = build_parser()

    assert parser.parse_args(["--workspace", ".", "--langsmith"]).langsmith is True
    assert parser.parse_args(["--workspace", ".", "--no-langsmith"]).langsmith is False
    assert build_web_parser().parse_args([]).workspace is None


def test_browser_settings_persist_without_api_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"api_key": "must-not-survive", "other": "preserved"}')
    config = AgentConfig(
        model="next-model",
        base_url="https://api.example.com/v1",
        api_key="runtime-only",
        command_timeout_seconds=300,
        langsmith_enabled=True,
        langsmith_project="next-project",
    )

    save_non_secret_config(config, path)

    saved = json.loads(path.read_text())
    assert saved == {
        "base_url": "https://api.example.com/v1",
        "command_timeout_seconds": 300,
        "langsmith_enabled": True,
        "langsmith_project": "next-project",
        "max_parallel_sessions": 4,
        "model": "next-model",
        "model_timeout_seconds": 180,
        "other": "preserved",
    }
    assert path.stat().st_mode & 0o777 == 0o600
