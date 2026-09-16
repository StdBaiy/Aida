import json
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from coding_agent.execution.host import HostExecutionBackend
from coding_agent.models import CommandRequest
from coding_agent.prompting import assemble_prompt
from coding_agent.tracing import MetricsLangSmithExporter, TraceRecorder, TraceStore
from coding_agent.tracing.callbacks import LocalTraceCallbackHandler
from coding_agent.tracing.context import activate_recorder
from coding_agent.tracing.recorder import truncate_middle
from coding_agent.tracing.redaction import redact
from coding_agent.workspace.paths import PathGuard


class FakeLangSmithClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def create_run(
        self,
        name: str,
        inputs: dict[str, Any],
        run_type: str,
        **kwargs: Any,
    ) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append({"name": name, "inputs": inputs, "run_type": run_type, **kwargs})

    def flush(self, timeout: float | None = None) -> None:
        return None


def make_recorder(tmp_path: Path) -> tuple[TraceStore, TraceRecorder]:
    store = TraceStore(tmp_path / "agent.db", tmp_path / "artifacts")
    recorder = TraceRecorder(
        store=store,
        trace_id="trace-1",
        turn_id="turn-1",
        session_id="private-session",
        timeline_id="private-timeline",
        logical_run_id="logical-1",
        model_id="test-model",
        secrets=("sk-super-secret-value",),
    )
    return store, recorder


def test_trace_records_parent_spans_large_artifacts_and_redacts(tmp_path: Path) -> None:
    store, recorder = make_recorder(tmp_path)
    parent = recorder.start_span(
        "agent.invoke",
        kind="agent",
        inputs={"api_key": "hidden", "message": "sk-super-secret-value"},
    )
    child = recorder.start_span(
        "read_file",
        kind="tool",
        parent_span_id=parent,
        inputs={"content": "x" * 20_000},
    )
    recorder.end_span(child, output={"ok": True})
    recorder.end_span(parent)
    recorder.finish(status="completed")

    rows = store.query_all(
        "SELECT * FROM spans WHERE trace_id = ? ORDER BY started_at", ("trace-1",)
    )
    child_row = next(row for row in rows if row["span_id"] == child)
    parent_row = next(row for row in rows if row["span_id"] == parent)
    assert child_row["parent_span_id"] == parent
    assert child_row["input_artifact_id"] is not None
    assert (
        store.artifacts.path(
            store.query_one(
                "SELECT relative_path FROM artifacts WHERE artifact_id = ?",
                (child_row["input_artifact_id"],),
            )["relative_path"]
        )
        .stat()
        .st_size
        > 16_000
    )
    assert "hidden" not in parent_row["attributes_json"]
    assert "sk-super-secret-value" not in parent_row["attributes_json"]
    assert "[REDACTED]" in parent_row["attributes_json"]
    store.close()


def test_redaction_preserves_metric_token_keys() -> None:
    assert redact({"input_tokens": 12, "token": "secret"}) == {
        "input_tokens": 12,
        "token": "[REDACTED]",
    }


def test_trace_summary_prints_attributes_and_truncates_the_middle(tmp_path: Path) -> None:
    store, recorder = make_recorder(tmp_path)
    short = recorder.start_span("short", kind="tool", attributes={"ok": True})
    recorder.end_span(short)
    long = recorder.start_span(
        "long",
        kind="tool",
        attributes={"value": ("a" * 3000) + "middle-secret" + ("z" * 3000)},
    )
    recorder.end_span(long)
    recorder.finish(status="completed")

    summary = store.trace_for_turn_id("turn-1")
    assert summary is not None
    spans = {span["name"]: span for span in summary["spans"]}
    assert '"ok": true' in spans["short"]["attributes_json"]
    assert not spans["short"]["attributes_json_truncated"]
    assert len(spans["long"]["attributes_json"]) == 4096
    assert "\n...\n" in spans["long"]["attributes_json"]
    assert "middle-secret" not in spans["long"]["attributes_json"]
    assert spans["long"]["attributes_json_truncated"]
    store.close()


def test_middle_truncation_preserves_both_ends() -> None:
    assert truncate_middle("abcdefghij", 9) == "ab\n...\nij"


def test_trace_store_recovers_running_traces_after_restart(tmp_path: Path) -> None:
    store, recorder = make_recorder(tmp_path)
    recorder.start_span("model.call", kind="model")
    store.close()

    recovered = TraceStore(tmp_path / "agent.db", tmp_path / "artifacts")
    trace = recovered.query_one(
        "SELECT status, ended_at FROM traces WHERE trace_id = ?",
        ("trace-1",),
    )
    spans = recovered.query_all(
        "SELECT status, ended_at FROM spans WHERE trace_id = ?",
        ("trace-1",),
    )

    assert trace is not None
    assert trace["status"] == "interrupted"
    assert trace["ended_at"] is not None
    assert {span["status"] for span in spans} == {"cancelled"}
    assert all(span["ended_at"] is not None for span in spans)
    recovered.close()


def test_cancelled_trace_has_cancelled_root_span(tmp_path: Path) -> None:
    store, recorder = make_recorder(tmp_path)
    recorder.finish(status="cancelled")

    root = store.query_one(
        "SELECT status FROM spans WHERE span_id = ?",
        (recorder.root_span_id,),
    )
    assert root is not None
    assert root["status"] == "cancelled"
    store.close()


def test_langsmith_export_contains_only_fixed_metrics(tmp_path: Path) -> None:
    store, recorder = make_recorder(tmp_path)
    model = recorder.start_span(
        "model.call",
        kind="model",
        inputs={"messages": [{"content": "private source /tmp/repo/file.py"}]},
    )
    recorder.end_span(model, output={"content": "private response"})
    tool = recorder.start_span(
        "run_command",
        kind="tool",
        inputs={"argv": ["pytest", "/tmp/repo/test_secret.py"]},
    )
    recorder.end_span(tool, status="error", output={"ok": False})
    snapshot = recorder.start_span("workspace.snapshot", kind="workspace")
    recorder.end_span(
        snapshot,
        attributes={"changed_file_count": 2, "added_lines": 7, "deleted_lines": 3},
    )
    recorder.add_tokens(10, 4)
    recorder.finish(status="completed")

    client = FakeLangSmithClient()
    exporter = MetricsLangSmithExporter(
        store,
        enabled=True,
        project="test",
        client_factory=lambda: client,
    )
    assert exporter.export("trace-1")
    payload = client.calls[0]["inputs"]
    assert payload["input_tokens"] == 10
    assert payload["tool_call_count"] == 1
    assert payload["command_failure_count"] == 1
    assert payload["changed_file_count"] == 2
    encoded = json.dumps(client.calls[0], default=str)
    for forbidden in ("private source", "private response", "/tmp/repo", "pytest"):
        assert forbidden not in encoded
    assert payload["session_id_hash"] != "private-session"
    assert payload["timeline_id_hash"] != "private-timeline"
    store.close()


def test_langsmith_failure_is_persisted_and_does_not_raise(tmp_path: Path) -> None:
    store, recorder = make_recorder(tmp_path)
    recorder.finish(status="completed")
    exporter = MetricsLangSmithExporter(
        store,
        enabled=True,
        project="test",
        client_factory=lambda: FakeLangSmithClient(error=RuntimeError("offline")),
    )

    assert not exporter.export("trace-1")
    row = store.query_one(
        "SELECT langsmith_export_status, langsmith_export_attempts FROM traces WHERE trace_id = ?",
        ("trace-1",),
    )
    assert row["langsmith_export_status"] == "failed"
    assert row["langsmith_export_attempts"] == 1
    store.close()


def test_callback_collects_model_usage(tmp_path: Path) -> None:
    store, recorder = make_recorder(tmp_path)
    callback = LocalTraceCallbackHandler(recorder)
    run_id = uuid4()
    callback.on_chat_model_start({}, [[AIMessage(content="prompt")]], run_id=run_id)
    callback.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content="answer",
                            usage_metadata={
                                "input_tokens": 9,
                                "output_tokens": 3,
                                "total_tokens": 12,
                            },
                        )
                    )
                ]
            ]
        ),
        run_id=run_id,
    )
    recorder.finish(status="completed")

    row = store.query_one(
        "SELECT input_tokens, output_tokens FROM traces WHERE trace_id = ?",
        ("trace-1",),
    )
    assert dict(row) == {"input_tokens": 9, "output_tokens": 3}
    store.close()


def test_callback_records_prompt_manifest_on_each_model_call(tmp_path: Path) -> None:
    store, recorder = make_recorder(tmp_path)
    manifest = assemble_prompt(role_instruction="private task input").metadata()
    callback = LocalTraceCallbackHandler(recorder, prompt_metadata=manifest)
    try:
        for _ in range(2):
            callback.on_chat_model_start({}, [[AIMessage(content="hello")]], run_id=uuid4())
        rows = store.query_all(
            "SELECT attributes_json FROM spans WHERE name = 'model.call'",
        )
        assert len(rows) == 2
        for row in rows:
            attributes = json.loads(row["attributes_json"])
            assert attributes["prompt"] == manifest
            assert "private task input" not in row["attributes_json"]
    finally:
        recorder.finish(status="completed")
        store.close()


def test_cache_usage_accumulates_across_model_calls(tmp_path: Path) -> None:
    store, recorder = make_recorder(tmp_path)

    recorder.add_cache_stats(70, 30)
    recorder.add_cache_stats(20, 80)

    row = store.query_one(
        "SELECT attributes_json FROM spans WHERE span_id = ?",
        (recorder.root_span_id,),
    )
    attributes = json.loads(row["attributes_json"])
    assert attributes["cache_hit_tokens"] == 90
    assert attributes["cache_miss_tokens"] == 110
    assert attributes["cache_hit_rate"] == 0.45
    store.close()


def test_command_full_output_is_saved_as_redacted_artifact(tmp_path: Path) -> None:
    store, recorder = make_recorder(tmp_path)
    backend = HostExecutionBackend(PathGuard(tmp_path), max_output_bytes=1024)
    output = "sk-super-secret-value" + ("x" * 3000)
    with activate_recorder(recorder):
        result = backend.run(CommandRequest(argv=[sys.executable, "-c", f"print({output!r})"]))

    assert result.stdout_truncated
    assert result.stdout_artifact_id is not None
    artifact = store.query_one(
        "SELECT relative_path FROM artifacts WHERE artifact_id = ?",
        (result.stdout_artifact_id,),
    )
    content = store.artifacts.path(artifact["relative_path"]).read_text()
    assert "sk-super-secret-value" not in content
    assert "[REDACTED]" in content
    assert len(content) > len(result.stdout)
    store.close()
