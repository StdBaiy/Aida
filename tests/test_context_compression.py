import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.tools import tool
from langgraph.graph.message import add_messages

from coding_agent.context import CompressUrgency, ContextAccountant, ContextCompressor
from coding_agent.context.cache_stats import CacheStatsTracker
from coding_agent.context.callbacks import ContextUsageCallbackHandler
from coding_agent.context.config import ContextWindowConfig
from coding_agent.runtime import AgentRuntime
from coding_agent.tracing import TraceRecorder, TraceStore


def context_config() -> ContextWindowConfig:
    return ContextWindowConfig(
        model="test",
        hard_limit=200_000,
        soft_limit=100,
        emergency_threshold=150_000,
        min_messages_before_compress=1,
    )


def test_graph_config_always_includes_root_checkpoint_namespace() -> None:
    assert AgentRuntime.graph_config("thread") == {
        "configurable": {
            "thread_id": "thread",
            "checkpoint_ns": "",
        }
    }
    assert AgentRuntime.graph_config("thread", "checkpoint") == {
        "configurable": {
            "thread_id": "thread",
            "checkpoint_ns": "",
            "checkpoint_id": "checkpoint",
        }
    }


def test_checkpoint_context_resolves_the_historical_thread_and_serializes_messages() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE checkpoints (thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT)"
    )
    connection.execute(
        "INSERT INTO checkpoints VALUES ('historical-thread', '', 'checkpoint-1')"
    )

    class Graph:
        def get_state(self, config: dict[str, Any]) -> Any:
            assert config["configurable"] == {
                "thread_id": "historical-thread",
                "checkpoint_ns": "",
                "checkpoint_id": "checkpoint-1",
            }
            return SimpleNamespace(
                values={
                    "messages": [
                        HumanMessage(
                            content="inspect me",
                            additional_kwargs={"origin": "user"},
                        )
                    ]
                }
            )

    @tool
    def read_file(path: str) -> str:
        """Read a file."""
        return path

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.connection = connection
    runtime.saver = SimpleNamespace(lock=threading.Lock())
    runtime.graph = cast(Any, Graph())
    runtime.context_config = context_config()
    runtime.accountant = ContextAccountant(runtime.context_config)
    runtime._system_prompt = "system prompt"
    runtime._context_tools = [read_file]

    result = runtime.inspect_checkpoint_context("checkpoint-1")

    assert result["thread_id"] == "historical-thread"
    assert result["message_count"] == 1
    assert result["messages"][0]["content"] == "inspect me"
    assert result["messages"][0]["additional_kwargs"]["origin"] == "user"
    assert result["static_context"]["system_prompt"] == "system prompt"
    assert result["static_context"]["tools"][0]["name"] == "read_file"
    connection.close()


def test_compressor_is_immutable_and_requires_artifacts_before_compacting() -> None:
    messages = [
        {"role": "tool", "content": f"{index}-" + ("x" * 60_000)}
        for index in range(6)
    ]
    original = [dict(message) for message in messages]
    accountant = ContextAccountant(context_config())
    compressor = ContextCompressor(context_config(), accountant)

    without_store = compressor.compress(messages, CompressUrgency.NORMAL)
    assert not without_store.changed
    assert messages == original

    artifacts: dict[str, str] = {}

    def persist(content: str) -> str:
        artifact_id = f"artifact-{len(artifacts)}"
        artifacts[artifact_id] = content
        return artifact_id

    result = compressor.compress(
        messages,
        CompressUrgency.NORMAL,
        persist_artifact=persist,
    )

    assert result.changed
    assert result.saved_tokens > 0
    assert messages == original
    assert result.artifact_ids
    for index in result.changed_indices:
        artifact_id = result.messages[index]["_compression_artifact_id"]
        assert artifacts[artifact_id] == original[index]["content"]
        assert artifact_id in result.messages[index]["content"]


def test_emergency_compression_handles_conversation_only_history() -> None:
    messages = [
        {"role": "user" if index % 2 == 0 else "assistant", "content": "中" * 5_000}
        for index in range(12)
    ]
    artifacts: dict[str, str] = {}

    def persist(content: str) -> str:
        artifact_id = f"artifact-{len(artifacts)}"
        artifacts[artifact_id] = content
        return artifact_id

    result = ContextCompressor(
        context_config(),
        ContextAccountant(context_config()),
    ).compress(
        messages,
        CompressUrgency.EMERGENCY,
        persist_artifact=persist,
    )

    assert result.changed
    assert "emergency_conversation_compact" in result.strategies
    assert artifacts
    assert messages[0]["content"] == "中" * 5_000


def test_usage_callback_corrects_accounting_and_cache_details() -> None:
    accountant = ContextAccountant(context_config())
    callback = ContextUsageCallbackHandler(accountant)
    callback.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content="answer",
                            usage_metadata={
                                "input_tokens": 90,
                                "output_tokens": 10,
                                "total_tokens": 100,
                                "input_token_details": {"cache_read": 70},
                            },
                        )
                    )
                ]
            ]
        ),
        run_id=uuid4(),
    )

    assert accountant.has_exact_usage
    assert accountant.used_tokens == 90
    assert accountant.total_tokens == 90
    assert accountant.completion_tokens == 10
    assert accountant.cumulative_prompt_tokens == 90
    assert accountant.cumulative_completion_tokens == 10
    assert accountant.cache_hit_tokens == 70
    assert accountant.cache_miss_tokens == 20
    assert accountant.estimate_text("中文") == 2


def test_accountant_snapshot_round_trips_window_and_cumulative_usage() -> None:
    accountant = ContextAccountant(context_config())
    accountant.record_usage(
        {
            "prompt_tokens": 80,
            "completion_tokens": 20,
            "prompt_cache_hit_tokens": 50,
            "prompt_cache_miss_tokens": 30,
        }
    )
    accountant.mark_compressed("handoff_summary", 45)

    restored = ContextAccountant(context_config())
    restored.restore(accountant.snapshot())

    assert restored.used_tokens == 45
    assert restored.total_prompt_tokens == 80
    assert restored.completion_tokens == 20
    assert restored.cumulative_prompt_tokens == 80
    assert restored.cumulative_completion_tokens == 20
    assert restored.compression_count == 1
    assert restored.last_strategy == "handoff_summary"


def test_blended_cache_price_keeps_per_million_units() -> None:
    tracker = CacheStatsTracker("session")
    tracker.record_usage(100, 50, 50, 0)

    assert tracker.blended_cost_per_1m == 0.07150000000000001


def test_recent_user_inputs_keep_chronological_order_and_skip_internal_messages() -> None:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.context_config = context_config()
    runtime.accountant = ContextAccountant(runtime.context_config)
    runtime._context_tools = []
    runtime._system_prompt = "system"
    messages = [
        HumanMessage(content="first", additional_kwargs={"origin": "user"}),
        HumanMessage(content="scheduler", additional_kwargs={"origin": "tool_scheduler"}),
        AIMessage(content="answer"),
        HumanMessage(content="second", additional_kwargs={"origin": "user"}),
    ]

    selected = runtime._recent_user_inputs(messages, token_budget=100)

    assert [message.content for message in selected] == ["first", "second"]


def test_recent_user_inputs_truncate_only_the_oldest_selected_message() -> None:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.context_config = context_config()
    runtime.accountant = ContextAccountant(runtime.context_config)
    runtime._context_tools = []
    runtime._system_prompt = "system"
    newest = HumanMessage(content="newest", additional_kwargs={"origin": "user"})
    messages = [
        HumanMessage(content="a" * 200, additional_kwargs={"origin": "user"}),
        newest,
    ]

    selected = runtime._recent_user_inputs(messages, token_budget=15)

    assert selected[-1].content == "newest"
    assert sum(
        runtime._estimate_context([message], include_static=False)
        for message in selected
    ) <= 15


def test_compaction_rebuilds_summary_manifest_and_recent_input_order() -> None:
    messages = [
        HumanMessage(content="old request", additional_kwargs={"origin": "user"}),
        AIMessage(content="old answer"),
        HumanMessage(content="latest request", additional_kwargs={"origin": "user"}),
    ]

    class Graph:
        def __init__(self) -> None:
            self.update: list[Any] = []

        def get_state(self, _config: Any) -> Any:
            return SimpleNamespace(values={"messages": messages})

        def update_state(self, _config: Any, values: dict[str, Any]) -> dict[str, Any]:
            self.update = values["messages"]
            return {"configurable": {"checkpoint_id": "compressed-checkpoint"}}

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.graph = cast(Any, Graph())
    runtime._model = SimpleNamespace(
        invoke=lambda *_args, **_kwargs: AIMessage(content="new summary")
    )
    runtime._system_prompt = "system"
    runtime.context_config = context_config()
    runtime.accountant = ContextAccountant(runtime.context_config)
    runtime._context_tools = []
    runtime.context_summary_max_tokens = 100
    runtime.context_recent_user_inputs_max_tokens = 100
    runtime._session_id = "session"
    runtime.tool_output_archive = SimpleNamespace(
        list_outputs=lambda _session_id: [
            {
                "tool_output_id": "tool-output",
                "invocation_id": "call",
                "byte_size": 12,
            }
        ]
    )

    result = runtime.compact_context(
        thread_id="thread",
        checkpoint_id="checkpoint",
        summaries=[{"summary_id": "summary_000", "content": "old summary"}],
    )

    update = runtime.graph.update
    assert isinstance(update[0], RemoveMessage)
    assert isinstance(update[1], SystemMessage)
    assert update[1].additional_kwargs["summary_id"] == "summary_000"
    assert isinstance(update[2], SystemMessage)
    assert update[2].additional_kwargs["summary_id"] == "summary_001"
    assert isinstance(update[3], SystemMessage)
    assert update[3].additional_kwargs["origin"] == "tool_evidence_manifest"
    assert [message.content for message in update[4:]] == [
        "old request",
        "latest request",
    ]
    assert result["summary_id"] == "summary_001"
    assert result["checkpoint_id"] == "compressed-checkpoint"


def test_budget_guard_commits_pending_input_without_sampling() -> None:
    class Graph:
        def __init__(self) -> None:
            self.messages: list[Any] = [HumanMessage(content="x" * 400)]

        def get_state(self, _config: Any) -> Any:
            return SimpleNamespace(values={"messages": self.messages})

        def update_state(self, _config: Any, values: dict[str, Any]) -> None:
            self.messages.extend(values["messages"])

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.graph = cast(Any, Graph())
    runtime.context_config = ContextWindowConfig(
        model="test",
        hard_limit=100,
        soft_limit=80,
        emergency_threshold=90,
        max_output_tokens=20,
        min_reserved_tokens=20,
    )
    runtime.accountant = ContextAccountant(runtime.context_config)
    runtime._context_tools = []
    runtime._system_prompt = "system"

    result = runtime._guard_context_budget(
        {"configurable": {"thread_id": "thread"}},
        {
            "messages": [
                HumanMessage(
                    content="continue",
                    additional_kwargs={"origin": "user"},
                )
            ]
        },
    )

    assert result is not None
    assert isinstance(runtime.graph.messages[-2], HumanMessage)
    assert isinstance(runtime.graph.messages[-1], AIMessage)
    assert (
        runtime.graph.messages[-1].additional_kwargs["error_code"]
        == "CONTEXT_BUDGET_EXHAUSTED"
    )


def test_runtime_compression_preserves_tool_metadata_and_records_audit(
    tmp_path: Path,
) -> None:
    messages = []
    for index in range(4):
        call_id = f"call-{index}"
        messages.extend(
            [
                AIMessage(
                    id=f"ai-{index}",
                    content="",
                    tool_calls=[
                        {
                            "id": call_id,
                            "name": "read_file",
                            "args": {"path": f"file-{index}.py"},
                        }
                    ],
                ),
                ToolMessage(
                    id=f"tool-{index}",
                    content="x" * 60_000,
                    tool_call_id=call_id,
                    name="read_file",
                    status="success",
                ),
            ]
        )
    messages.append(HumanMessage(id="human-final", content="continue"))

    class Graph:
        def __init__(self) -> None:
            self.messages = messages
            self.updates: list[list[Any]] = []

        def get_state(self, _config: Any) -> Any:
            return SimpleNamespace(values={"messages": self.messages})

        def update_state(self, _config: Any, values: dict[str, Any]) -> None:
            update = values["messages"]
            self.updates.append(update)
            self.messages = add_messages(self.messages, update)

    store = TraceStore(tmp_path / "agent.db", tmp_path / "artifacts")
    recorder = TraceRecorder(
        store=store,
        trace_id="trace",
        turn_id="turn",
        session_id="session",
        timeline_id="timeline",
        logical_run_id="logical",
        model_id="test",
        turn_number=7,
    )
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.graph = cast(Any, Graph())
    runtime.context_config = context_config()
    runtime.accountant = ContextAccountant(runtime.context_config)
    runtime._context_tools = []
    runtime._system_prompt = "system"

    runtime._maybe_compress_context(
        {"configurable": {"thread_id": "thread"}},
        recorder,
        {"messages": []},
    )

    assert runtime.accountant.compression_count == 1
    assert isinstance(runtime.graph.updates[0][0], RemoveMessage)
    assert len(runtime.graph.messages) == len(messages)
    for index in range(4):
        ai_message = runtime.graph.messages[index * 2]
        tool_message = runtime.graph.messages[index * 2 + 1]
        assert ai_message.id == f"ai-{index}"
        assert ai_message.tool_calls[0]["id"] == f"call-{index}"
        assert tool_message.id == f"tool-{index}"
        assert tool_message.tool_call_id == f"call-{index}"
        assert tool_message.name == "read_file"
        assert tool_message.status == "success"

    compression = store.query_one(
        "SELECT * FROM compressions WHERE session_id = ?",
        ("session",),
    )
    assert compression is not None
    assert compression["turn_number"] == 7
    assert compression["original_token_count"] > compression["compressed_token_count"]
    assert compression["pre_compress_artifact_id"]
    assert store.query_one(
        "SELECT 1 FROM spans WHERE trace_id = ? AND name = 'context.compression'",
        ("trace",),
    )
    store.close()
