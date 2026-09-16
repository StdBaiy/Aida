from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult
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
    assert accountant.total_tokens == 100
    assert accountant.cache_hit_tokens == 70
    assert accountant.cache_miss_tokens == 20
    assert accountant.estimate_text("中文") == 2


def test_blended_cache_price_keeps_per_million_units() -> None:
    tracker = CacheStatsTracker("session")
    tracker.record_usage(100, 50, 50, 0)

    assert tracker.blended_cost_per_1m == 0.07150000000000001


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
