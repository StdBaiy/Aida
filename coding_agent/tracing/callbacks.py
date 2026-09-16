"""LangChain callback adapter for the local trace recorder."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from coding_agent.context.callbacks import extract_token_usage
from coding_agent.tracing.recorder import TraceRecorder

_LOGGER = logging.getLogger(__name__)


class LocalTraceCallbackHandler(BaseCallbackHandler):
    """Capture model, tool, chain, and retry events in a local trace."""

    run_inline = True
    raise_error = False

    def __init__(self, recorder: TraceRecorder) -> None:
        super().__init__()
        self.recorder = recorder
        self._spans: dict[str, str] = {}
        self._lock = threading.RLock()

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Record the exact locally redacted model input."""
        self._start(
            run_id,
            parent_run_id,
            name="model.call",
            kind="model",
            inputs={"serialized": serialized, "messages": messages},
        )

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Record a text-model input."""
        self._start(
            run_id,
            parent_run_id,
            name="model.call",
            kind="model",
            inputs={"serialized": serialized, "prompts": prompts},
        )

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """Record model output, standardized token usage, and cache stats."""
        usage = extract_token_usage(response)
        input_tokens = usage["prompt_tokens"]
        output_tokens = usage["completion_tokens"]
        self.recorder.add_tokens(input_tokens, output_tokens)
        self._record_cache_stats(self.recorder, usage)
        self._end(
            run_id,
            output=response,
            attributes={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """Record model failure."""
        self._end(run_id, status="error", error=error)

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Record tool name and arguments."""
        tool_name = str(serialized.get("name") or kwargs.get("name") or "unknown")
        self._start(
            run_id,
            parent_run_id,
            name=tool_name,
            kind="tool",
            inputs=inputs if inputs is not None else input_str,
        )

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        """Record tool result and outcome fields used by metrics."""
        value = self._as_mapping(output)
        self._end(
            run_id,
            status="ok" if value.get("ok", True) else "error",
            output=output,
            attributes={
                "ok": bool(value.get("ok", True)),
                "timed_out": bool(value.get("timed_out", False)),
                "exit_code": value.get("exit_code"),
                "error_code": value.get("error_code"),
            },
        )

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """Record an uncaught tool error."""
        self._end(run_id, status="error", error=error)

    def on_chain_start(
        self,
        serialized: dict[str, Any] | None,
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Record LangGraph and middleware chain execution."""
        serialized = serialized or {}
        name = str(
            kwargs.get("name") or serialized.get("name") or (serialized.get("id") or ["chain"])[-1]
        )
        self._start(run_id, parent_run_id, name=name, kind="chain", inputs=inputs)

    def on_chain_end(
        self,
        outputs: dict[str, Any],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """Record chain output."""
        self._end(run_id, output=outputs)

    def on_chain_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """Record chain failure."""
        self._end(run_id, status="error", error=error)

    def on_retry(
        self,
        retry_state: Any,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: Any,
    ) -> None:
        """Record each model retry as a completed event span."""
        try:
            span_id = self.recorder.start_span(
                "model.retry",
                kind="retry",
                parent_span_id=self._parent(parent_run_id),
                attributes={"attempt": getattr(retry_state, "attempt_number", None)},
            )
            self.recorder.end_span(span_id)
        except Exception as exc:
            _LOGGER.warning("Trace callback failed: %s", exc)

    def _start(
        self,
        run_id: UUID,
        parent_run_id: UUID | None,
        *,
        name: str,
        kind: str,
        inputs: Any,
    ) -> None:
        try:
            span_id = self.recorder.start_span(
                name,
                kind=kind,
                parent_span_id=self._parent(parent_run_id),
                inputs=inputs,
            )
            with self._lock:
                self._spans[str(run_id)] = span_id
        except Exception as exc:
            _LOGGER.warning("Trace callback failed: %s", exc)

    def _end(
        self,
        run_id: UUID,
        *,
        status: str = "ok",
        output: Any = None,
        attributes: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        try:
            with self._lock:
                span_id = self._spans.pop(str(run_id), None)
            if span_id is not None:
                self.recorder.end_span(
                    span_id,
                    status=status,
                    output=output,
                    attributes=attributes,
                    error=error,
                )
        except Exception as exc:
            _LOGGER.warning("Trace callback failed: %s", exc)

    def _parent(self, parent_run_id: UUID | None) -> str:
        if parent_run_id is None:
            return self.recorder.root_span_id
        with self._lock:
            return self._spans.get(str(parent_run_id), self.recorder.root_span_id)

    def _record_cache_stats(self, recorder: TraceRecorder, usage: dict[str, Any]) -> None:
        """Extract prompt cache stats from usage and record them."""
        cache_hit = int(usage.get("prompt_cache_hit_tokens", 0))
        cache_miss = int(usage.get("prompt_cache_miss_tokens", 0))
        if cache_hit or cache_miss:
            recorder.add_cache_stats(cache_hit, cache_miss)

    @staticmethod
    def _as_mapping(output: Any) -> dict[str, Any]:
        if isinstance(output, dict):
            return output
        content = getattr(output, "content", None)
        if isinstance(content, str):
            try:
                value = json.loads(content)
                if isinstance(value, dict):
                    return value
            except ValueError:
                pass
        return {}
