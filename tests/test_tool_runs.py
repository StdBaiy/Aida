import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from coding_agent.execution import ToolRunManager, activate_tool_turn
from coding_agent.execution.host import HostExecutionBackend
from coding_agent.models import CommandRequest
from coding_agent.runtime import AgentRuntime
from coding_agent.tracing import TraceRecorder, TraceStore
from coding_agent.workspace.paths import PathGuard


class _ToolState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]


def _recorder(tmp_path: Path) -> tuple[TraceStore, TraceRecorder]:
    store = TraceStore(tmp_path / "trace.db", tmp_path / "artifacts")
    return store, TraceRecorder(
        store=store,
        trace_id="trace",
        turn_id="turn",
        session_id="session",
        timeline_id="timeline",
        logical_run_id="logical",
        model_id="model",
    )


def test_tool_runs_execute_concurrently_and_share_trace_group(tmp_path: Path) -> None:
    store, recorder = _recorder(tmp_path)
    manager = ToolRunManager(max_workers=2)
    manager.begin_turn("thread")
    barrier = threading.Barrier(2)

    def work(_cancel: threading.Event, output: Any) -> dict[str, object]:
        barrier.wait(timeout=2)
        output("stdout", b"done\n")
        time.sleep(0.05)
        return {"ok": True}

    with activate_tool_turn("thread", recorder):
        first = manager.start_current(
            name="first",
            effect="read_only",
            execution_group_id="alternatives",
            work=work,
        )
        second = manager.start_current(
            name="second",
            effect="read_only",
            execution_group_id="alternatives",
            work=work,
        )

    assert manager.drain_thread("thread", timeout_seconds=3)
    manager.finalize_thread("thread")
    assert manager.inspect("thread", str(first["run_id"]))["status"] == "completed"
    assert manager.inspect("thread", str(second["run_id"]))["status"] == "completed"

    tool_spans = store.query_all(
        "SELECT * FROM spans WHERE trace_id = ? AND kind = 'tool_run'",
        ("trace",),
    )
    [group_span] = store.query_all(
        "SELECT * FROM spans WHERE trace_id = ? AND name = 'tool.group'",
        ("trace",),
    )
    assert len(tool_spans) == 2
    assert {span["parent_span_id"] for span in tool_spans} == {group_span["span_id"]}
    assert all(span["status"] == "ok" for span in tool_spans)
    assert group_span["status"] == "ok"
    assert json.loads(group_span["attributes_json"])["peak_parallelism"] == 2

    wake_span = recorder.start_span("scheduler.wake", kind="scheduler")
    recorder.link_spans(tool_spans[0]["span_id"], wake_span, "triggered_by")
    recorder.end_span(wake_span)
    recorder.finish(status="completed")
    summary = store.trace_for_turn_id("turn")
    assert summary is not None
    assert summary["span_links"] == [
        {
            "source_span_id": tool_spans[0]["span_id"],
            "target_span_id": wake_span,
            "relation": "triggered_by",
            "attributes_json": "{}",
        }
    ]
    manager.close()
    store.close()


def test_tool_run_exposes_incremental_output_and_probe_timeout() -> None:
    manager = ToolRunManager(max_workers=1)
    manager.begin_turn("thread")
    output_ready = threading.Event()
    release = threading.Event()
    events: list[tuple[str, dict[str, Any]]] = []

    def work(_cancel: threading.Event, output: Any) -> dict[str, object]:
        output("stdout", b"working\n")
        output_ready.set()
        release.wait(timeout=2)
        return {"ok": True, "value": 42}

    with activate_tool_turn(
        "thread",
        None,
        lambda event_type, payload: events.append((event_type, payload)),
    ):
        started = manager.start_current(name="slow", effect="read_only", work=work)
        run_id = str(started["run_id"])
        assert output_ready.wait(timeout=1)
        snapshot = manager.inspect_current(run_id)
        wake = manager.wait_current([run_id], timeout_seconds=1)

    assert snapshot["status"] == "running"
    assert snapshot["output"] == [
        {"cursor": 1, "stream": "stdout", "text": "working\n"}
    ]
    assert wake["wake_reason"] == "probe_timeout"

    release.set()
    assert manager.drain_thread("thread", timeout_seconds=2)
    final = manager.inspect("thread", run_id, after_cursor=1)
    assert final["status"] == "completed"
    assert final["result"]["value"] == 42
    assert final["output"] == []
    assert [event_type for event_type, _payload in events] == [
        "tool.started",
        "tool.running",
        "tool.output",
        "tool.completed",
    ]
    assert events[-1][1]["result_preview"] == '{"ok": true, "value": 42}'
    manager.close()


def test_command_tool_run_can_be_cancelled(tmp_path: Path) -> None:
    manager = ToolRunManager(max_workers=1)
    backend = HostExecutionBackend(PathGuard(tmp_path))
    manager.begin_turn("thread")
    request = CommandRequest(
        argv=[sys.executable, "-c", "import time; print('ready', flush=True); time.sleep(30)"],
        timeout_seconds=60,
    )

    with activate_tool_turn("thread", None):
        started = manager.start_current(
            name="command",
            effect="workspace_write",
            work=lambda cancel, output: backend.run(
                request,
                cancel_event=cancel,
                on_output=output,
            ).model_dump(),
        )
        run_id = str(started["run_id"])
        deadline = time.monotonic() + 2
        while not manager.inspect_current(run_id)["output"] and time.monotonic() < deadline:
            time.sleep(0.02)
        assert manager.inspect_current(run_id)["output"]
        manager.cancel_current(run_id, reason="alternative selected")

    assert manager.drain_thread("thread", timeout_seconds=5)
    final = manager.inspect("thread", run_id)
    assert final["status"] == "cancelled"
    assert final["result"]["exit_code"] is None
    manager.close()


def test_runtime_scheduler_wakes_model_until_tool_is_terminal() -> None:
    manager = ToolRunManager(max_workers=1)

    class SchedulerGraph:
        calls = 0

        def stream(self, value: Any, *_args: Any, **_kwargs: Any) -> Any:
            self.calls += 1
            if self.calls == 1:
                manager.start_current(
                    name="slow",
                    effect="read_only",
                    work=lambda _cancel, _output: (time.sleep(0.05), {"ok": True})[1],
                )
                text = "premature"
            else:
                assert "Tool scheduler" in value["messages"][0]["content"]
                text = "final"
            yield "values", {"messages": [AIMessage(content=text)]}

        def get_state(self, _config: Any) -> Any:
            return SimpleNamespace(config={"configurable": {"checkpoint_id": "checkpoint"}})

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.graph = SchedulerGraph()
    runtime.tool_runs = manager
    runtime.tool_probe_interval_seconds = 1
    runtime.max_tool_scheduler_wakes = 2

    response, checkpoint_id = runtime.run_turn(
        thread_id="thread",
        user_text="run",
        approve=lambda _request: True,
    )

    assert response == "final"
    assert checkpoint_id == "checkpoint"
    assert runtime.graph.calls == 2
    assert manager.active_run_ids("thread") == []
    manager.close()


def test_tool_node_propagates_turn_context_to_parallel_dispatch() -> None:
    manager = ToolRunManager(max_workers=2)
    manager.begin_turn("thread")
    barrier = threading.Barrier(2)

    @tool
    def remote_lookup(value: str) -> str:
        """Return a value after both speculative calls have started."""
        barrier.wait(timeout=2)
        return value

    builder = StateGraph(_ToolState)
    builder.add_node("tools", ToolNode([manager.wrap_tool(remote_lookup, effect="read_only")]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    graph = builder.compile()
    message = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "remote_lookup",
                "args": {"value": "one"},
                "id": "call-1",
                "type": "tool_call",
            },
            {
                "name": "remote_lookup",
                "args": {"value": "two"},
                "id": "call-2",
                "type": "tool_call",
            },
        ],
    )

    with activate_tool_turn("thread", None):
        graph.invoke({"messages": [message]})

    assert manager.drain_thread("thread", timeout_seconds=3)
    run_ids = manager.run_ids("thread")
    assert len(run_ids) == 2
    assert all(manager.inspect("thread", run_id)["status"] == "completed" for run_id in run_ids)
    manager.close()
