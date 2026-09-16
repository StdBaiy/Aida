"""LangChain/LangGraph runtime adapter."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    HumanInTheLoopMiddleware,
    ModelCallLimitMiddleware,
    ModelRetryMiddleware,
    ToolCallLimitMiddleware,
)
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
    convert_to_messages,
)
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command, StreamMode
from langsmith import tracing_context
from pydantic import SecretStr

from coding_agent.config import AgentConfig
from coding_agent.context import CompressUrgency, ContextAccountant, ContextCompressor
from coding_agent.context.callbacks import ContextUsageCallbackHandler
from coding_agent.context.config import resolve_context_config
from coding_agent.errors import fail
from coding_agent.execution import ToolRunManager, activate_tool_turn
from coding_agent.mcp import LazyMCPToolProvider
from coding_agent.prompts import build_system_prompt
from coding_agent.sandbox import (
    DockerCliProvider,
    ResourceBudget,
    SandboxExecutionProvider,
    SandboxExecutionRepository,
    SandboxExecutionService,
    SeatbeltProvider,
)
from coding_agent.skill_execution import SkillExecutionManager, activate_skill_thread
from coding_agent.skills import (
    SkillRegistry,
    default_skill_roots,
    discover_skills,
    format_skill_catalog,
)
from coding_agent.tools import build_tools
from coding_agent.tracing.callbacks import LocalTraceCallbackHandler
from coding_agent.tracing.context import activate_recorder
from coding_agent.tracing.recorder import TraceRecorder
from coding_agent.workspace.mutation import (
    WorkspaceMutationGate,
    activate_workspace_mutation,
)

ApprovalCallback = Callable[[dict[str, Any]], bool]
TokenCallback = Callable[[str], None]
ToolEventCallback = Callable[[str, dict[str, Any]], None]


def approve_by_default(_request: dict[str, Any]) -> bool:
    """Approve commands that already passed the non-bypassable command policy."""
    return True


class AgentRuntime:
    """Own the model, compiled graph, and SQLite checkpoint connection."""

    def __init__(
        self,
        *,
        config: AgentConfig,
        workspace_root: Path,
        repo_root: Path,
        checkpoint_path: Path,
        extra_tools: list[BaseTool] | None = None,
        allowed_tool_names: frozenset[str] | None = None,
        allowed_mcp_tool_names: frozenset[str] | None = None,
        mutation_gate: WorkspaceMutationGate | None = None,
        role_instruction: str | None = None,
    ) -> None:
        model_name = config.model.removeprefix("openai:")
        model = ChatOpenAI(
            model=model_name,
            api_key=SecretStr(config.api_key or ""),
            base_url=config.base_url,
            temperature=0,
            timeout=config.model_timeout_seconds,
            use_responses_api=False,
        )
        self.skill_registry = SkillRegistry(
            discover_skills(default_skill_roots(workspace_root)),
            max_read_bytes=config.max_read_bytes,
        )
        self.tool_runs = ToolRunManager(
            max_workers=config.max_parallel_tools,
            max_output_bytes=config.max_command_output_bytes,
        )
        self.tool_probe_interval_seconds = config.tool_probe_interval_seconds
        self.max_tool_scheduler_wakes = config.max_tool_scheduler_wakes
        self.mutation_gate = mutation_gate
        self.sandbox_provider: SandboxExecutionProvider
        if config.sandbox_provider == "seatbelt":
            self.sandbox_provider = SeatbeltProvider(
                read_only_paths=config.sandbox_read_only_paths
            )
        else:
            self.sandbox_provider = DockerCliProvider(
                image=config.sandbox_image or "coding-agent-sandbox:local"
            )
        self.execution_service = SandboxExecutionService(
            workspace_root=workspace_root,
            provider=self.sandbox_provider,
            repository=SandboxExecutionRepository(
                checkpoint_path.parent / "sandbox.db"
            ),
            budget=ResourceBudget(
                cpus=config.sandbox_cpu_limit,
                memory_bytes=config.sandbox_memory_bytes,
                pid_limit=config.sandbox_pid_limit,
                nofile_limit=config.sandbox_nofile_limit,
                tmpfs_bytes=config.sandbox_tmpfs_bytes,
                home_tmpfs_bytes=config.sandbox_home_tmpfs_bytes,
                output_bytes=config.sandbox_output_bytes,
                workspace_growth_bytes=config.sandbox_workspace_growth_bytes,
            ),
            enabled=config.sandbox_enabled,
            session_parallelism=config.sandbox_max_parallel_per_session,
            global_parallelism=config.sandbox_max_parallel_global,
            stop_grace_seconds=config.sandbox_cleanup_grace_seconds,
            sandbox_user=config.sandbox_user,
            max_result_output_bytes=config.max_command_output_bytes,
        )
        self.sandbox_health = self.sandbox_provider.health()
        self.execution_service.record_health()
        if self.sandbox_health.available:
            self.execution_service.reconcile()
        self.skill_execution = SkillExecutionManager(
            self.skill_registry,
            workspace_root=workspace_root,
            config=config,
            execution_service=self.execution_service,
            tool_runs=self.tool_runs,
        )
        skill_catalog = format_skill_catalog(self.skill_registry.skills)
        local_tools = [
            *build_tools(
                workspace_root,
                repo_root,
                config,
                self.tool_runs,
                self.execution_service,
            ),
            self.skill_registry.tool,
            *self.skill_execution.tools,
            *self.tool_runs.tools,
        ]
        self.mcp_provider = LazyMCPToolProvider(
            config.mcp_servers,
            self.tool_runs,
            allowed_mcp_tool_names,
        )
        tools = [*local_tools, *self.mcp_provider.tools, *(extra_tools or [])]
        if allowed_tool_names is not None:
            tools = [tool for tool in tools if tool.name in allowed_tool_names]
        system_prompt = build_system_prompt(skill_catalog) + (
            f"\n\nAssigned role:\n{role_instruction}" if role_instruction else ""
        )
        self.context_config = resolve_context_config(model_name)
        self.accountant = ContextAccountant(self.context_config)
        self._context_tools = tools
        self._system_prompt = system_prompt
        try:
            self.connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
            self.saver = SqliteSaver(self.connection)
            self.saver.setup()
        except BaseException:
            self.mcp_provider.close()
            self.tool_runs.close()
            self.execution_service.close()
            raise
        middleware = cast(
            "list[AgentMiddleware[Any, None, Any]]",
            [
                ModelRetryMiddleware(max_retries=2),
                ModelCallLimitMiddleware(run_limit=20, exit_behavior="end"),
                ToolCallLimitMiddleware(run_limit=50, exit_behavior="end"),
                HumanInTheLoopMiddleware(
                    interrupt_on={
                        "run_command": {
                            "allowed_decisions": ["approve", "reject"],
                            "description": "Approved by default after command policy validation.",
                        },
                        "activate_skill": {
                            "allowed_decisions": ["approve", "reject"],
                            "description": "Enable this Skill's declared commands and MCP servers.",
                        },
                    }
                ),
            ],
        )
        self.graph = create_agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            middleware=middleware,
            checkpointer=self.saver,
            name="coding_agent",
        )

    @staticmethod
    def graph_config(thread_id: str, checkpoint_id: str | None = None) -> RunnableConfig:
        """Build a LangGraph checkpoint selector."""
        configurable = {"thread_id": thread_id}
        if checkpoint_id:
            configurable["checkpoint_id"] = checkpoint_id
        return {"configurable": configurable}

    def initialize_thread(self, thread_id: str) -> str:
        """Create the initial empty state and return its physical checkpoint ID."""
        result = self.graph.update_state(self.graph_config(thread_id), {"messages": []})
        return str(result["configurable"]["checkpoint_id"])

    def run_turn(
        self,
        *,
        thread_id: str,
        user_text: str,
        approve: ApprovalCallback,
        recorder: TraceRecorder | None = None,
        on_token: TokenCallback | None = None,
        on_tool_event: ToolEventCallback | None = None,
        cancelled: threading.Event | None = None,
        cancellation_code: str = "OPERATION_CANCELLED",
    ) -> tuple[str, str]:
        """Run one turn, resolving command interrupts through the callback."""
        config = self.graph_config(thread_id)
        self.tool_runs.begin_turn(thread_id)
        self._repair_unresolved_invalid_tool_calls(config)
        self._repair_unresolved_tool_calls(config)
        callbacks: list[Any] = []
        if hasattr(self, "accountant"):
            callbacks.append(ContextUsageCallbackHandler(self.accountant))
        if recorder is not None:
            callbacks.append(LocalTraceCallbackHandler(recorder))
        config["callbacks"] = callbacks
        try:
            with (
                tracing_context(enabled=False),
                activate_recorder(recorder),
                activate_skill_thread(thread_id),
                activate_tool_turn(thread_id, recorder, on_tool_event),
                activate_workspace_mutation(
                    getattr(self, "mutation_gate", None),
                    thread_id,
                    cancelled,
                ),
            ):
                result = self._run_with_approvals(
                    {"messages": [{"role": "user", "content": user_text}]},
                    config,
                    thread_id,
                    approve,
                    recorder,
                    on_token,
                    cancelled,
                    cancellation_code,
                )
                for wake_count in range(self.max_tool_scheduler_wakes):
                    self._raise_if_cancelled(cancelled, thread_id, cancellation_code)
                    active_run_ids = self.tool_runs.active_run_ids(thread_id)
                    if not active_run_ids:
                        break
                    wake = self.tool_runs.wait_for_thread(
                        thread_id,
                        timeout_seconds=self.tool_probe_interval_seconds,
                    )
                    wake_span_id = None
                    if recorder is not None:
                        wake_span_id = recorder.start_span(
                            "scheduler.wake",
                            kind="scheduler",
                            attributes={
                                "wake_count": wake_count + 1,
                                "wake_reason": wake["wake_reason"],
                                "tool_run_ids": active_run_ids,
                            },
                        )
                        for tool_span_id in self.tool_runs.span_ids(
                            thread_id,
                            active_run_ids,
                        ):
                            recorder.link_spans(
                                tool_span_id,
                                wake_span_id,
                                "triggered_by",
                            )
                    result = self._run_with_approvals(
                        {
                            "messages": [
                                {
                                    "role": "user",
                                    "content": (
                                        "[Tool scheduler] Background tools still require a "
                                        f"decision. Wake reason: {wake['wake_reason']}. "
                                        f"Run IDs: {', '.join(active_run_ids)}. Inspect their "
                                        "latest output, then wait, cancel, or start an "
                                        "independent parallel tool. Do not finish while any "
                                        "tool is active."
                                    ),
                                }
                            ]
                        },
                        config,
                        thread_id,
                        approve,
                        recorder,
                        on_token,
                        cancelled,
                        cancellation_code,
                    )
                    if recorder is not None and wake_span_id is not None:
                        recorder.end_span(wake_span_id)
                else:
                    self.tool_runs.cancel_thread(
                        thread_id,
                        reason="scheduler wake limit reached",
                    )
                    if not self.tool_runs.drain_thread(thread_id, timeout_seconds=10):
                        raise fail(
                            "TOOL_DRAIN_TIMEOUT",
                            "Background tools did not stop before the turn deadline.",
                        )
                    self.tool_runs.seal_thread(thread_id)
                    result = self._run_with_approvals(
                        {
                            "messages": [
                                {
                                    "role": "user",
                                    "content": (
                                        "[Tool scheduler] The wake limit was reached and all "
                                        "remaining tools were cancelled. Provide the final "
                                        "answer without starting another tool."
                                    ),
                                }
                            ]
                        },
                        config,
                        thread_id,
                        approve,
                        recorder,
                        on_token,
                        cancelled,
                        cancellation_code,
                    )
                self.tool_runs.seal_thread(thread_id)
                self.tool_runs.finalize_thread(thread_id)
        except BaseException as exc:
            is_cancellation = (
                hasattr(exc, "code") and exc.code == cancellation_code
            )
            self.tool_runs.cancel_thread(thread_id, reason="turn failed")
            drain_timeout = 5 if is_cancellation else 10
            if self.tool_runs.drain_thread(
                thread_id,
                timeout_seconds=drain_timeout,
            ):
                self.tool_runs.finalize_thread(thread_id)
            # Preserve the original runtime/cancellation error; the next turn retries repair.
            with suppress(Exception):
                self._repair_unresolved_tool_calls(config)
            if self._is_timeout_error(exc):
                raise fail(
                    "MODEL_TIMEOUT",
                    "The model call exceeded the configured timeout.",
                ) from exc
            raise
        if recorder is None:
            snapshot = self.graph.get_state(config)
            checkpoint_id = str(snapshot.config["configurable"]["checkpoint_id"])
        else:
            with recorder.span("checkpoint.read", kind="checkpoint") as span_id:
                snapshot = self.graph.get_state(config)
                checkpoint_id = str(snapshot.config["configurable"]["checkpoint_id"])
                recorder.end_span(span_id, output={"checkpoint_id": checkpoint_id})
        messages: list[BaseMessage] = result["messages"]
        return self._message_text(messages[-1]), checkpoint_id

    def cancel_active_tools(self, *, reason: str) -> list[str]:
        """Propagate an operation cancellation into active background tools."""
        return self.tool_runs.cancel_all(reason=reason)

    def _update_accountant_after_stream(
        self, result: dict[str, Any]
    ) -> None:
        """Refresh message count and retain exact callback usage when available."""
        if not hasattr(self, "accountant"):
            return
        messages = result.get("messages", [])
        estimate = self._estimate_context(messages)
        self.accountant.update_estimate(estimate, len(messages))

    def _maybe_compress_context(
        self,
        config: RunnableConfig,
        recorder: TraceRecorder | None,
        pending_value: Any,
    ) -> None:
        """Check context usage and compress if needed before a model call."""
        if not hasattr(self, "accountant") or not hasattr(self, "context_config"):
            return
        snapshot = self.graph.get_state(config)
        messages = list(snapshot.values.get("messages", []))
        pending_messages = self._pending_messages(pending_value)
        estimated_total = self._estimate_context(messages, pending_messages)
        if self.accountant.has_exact_usage:
            estimated_total = max(
                estimated_total,
                self.accountant.total_tokens
                + self._estimate_context(pending_messages, include_static=False),
            )
        self.accountant.update_estimate(
            estimated_total,
            len(messages) + len(pending_messages),
            force=True,
        )
        urgency = self.accountant.should_compress_cached()
        if (
            urgency == CompressUrgency.NONE
            and len(messages) + len(pending_messages) > self.context_config.max_messages
        ):
            urgency = CompressUrgency.NORMAL
        if urgency == CompressUrgency.NONE:
            return
        if self.accountant.compression_count >= 5:
            self._raise_if_context_unsafe(estimated_total)
            return

        if (
            urgency != CompressUrgency.EMERGENCY
            and len(messages) < self.context_config.min_messages_before_compress
            and len(messages) <= self.context_config.max_messages
        ):
            return

        raw_messages = [
            {
                "role": {
                    "human": "user",
                    "ai": "assistant",
                    "tool": "tool",
                    "system": "system",
                }.get(message.type, message.type),
                "content": message.content,
            }
            for message in messages
        ]
        pre_compress_artifact_id = None
        persist_artifact = None
        cache_hit_before = self.accountant.cache_hit_tokens
        cache_miss_before = self.accountant.cache_miss_tokens
        if recorder is not None:
            persist_artifact = recorder.capture_text_artifact
            pre_compress_artifact_id = recorder.capture_text_artifact(
                json.dumps(
                    [message.model_dump(mode="json") for message in messages],
                    ensure_ascii=False,
                    default=str,
                ),
                "application/json",
            )

        compressor = ContextCompressor(self.context_config, self.accountant)
        result = compressor.compress(
            raw_messages,
            urgency,
            persist_artifact=persist_artifact,
        )
        if not result.changed:
            self._raise_if_context_unsafe(estimated_total)
            return

        replacements = self._apply_compressed_messages(
            config,
            messages,
            result.messages,
        )
        compressed_total = self._estimate_context(replacements, pending_messages)
        self.accountant.mark_compressed(
            result.strategies[-1],
            compressed_total,
        )
        if recorder is not None:
            recorder.record_compression(
                session_id=recorder.session_id,
                timeline_id=recorder.timeline_id,
                turn_number=recorder.turn_number,
                urgency=urgency.value,
                strategy=",".join(result.strategies),
                original_message_count=len(messages),
                compressed_message_count=len(replacements),
                original_token_count=estimated_total,
                compressed_token_count=compressed_total,
                cache_hit_before=cache_hit_before,
                cache_miss_before=cache_miss_before,
                pre_compress_artifact_id=pre_compress_artifact_id,
            )
        self._raise_if_context_unsafe(compressed_total)

    def _apply_compressed_messages(
        self,
        config: RunnableConfig,
        original_messages: list[Any],
        compressed_dicts: list[dict[str, Any]],
    ) -> list[BaseMessage]:
        """Replace graph messages while preserving IDs and tool metadata."""
        replacements: list[BaseMessage] = []
        for compressed, original in zip(
            compressed_dicts,
            original_messages,
            strict=True,
        ):
            content = compressed.get("content", "")
            if content != original.content:
                replacements.append(original.model_copy(update={"content": content}))
            else:
                replacements.append(original)
        self.graph.update_state(
            config,
            {
                "messages": [
                    RemoveMessage(id=REMOVE_ALL_MESSAGES),
                    *replacements,
                ]
            },
        )
        return replacements

    def _estimate_context(
        self,
        messages: list[Any],
        pending_messages: list[BaseMessage] | None = None,
        *,
        include_static: bool = True,
    ) -> int:
        """Estimate all model-visible input, including tools and pending messages."""
        converted = [
            message
            if isinstance(message, BaseMessage)
            else convert_to_messages([message])[0]
            for message in messages
        ]
        converted.extend(pending_messages or [])
        if include_static:
            converted.insert(0, SystemMessage(content=self._system_prompt))
        try:
            estimated = count_tokens_approximately(
                converted,
                tools=(
                    cast("list[BaseTool | dict[str, Any]]", self._context_tools)
                    if include_static
                    else None
                ),
            )
        except (TypeError, ValueError):
            serialized = json.dumps(
                [
                    message.model_dump(mode="json")
                    for message in converted
                ],
                ensure_ascii=False,
                default=str,
            )
            estimated = self.accountant.estimate_text(serialized)
        non_ascii = sum(
            not char.isascii()
            for message in converted
            for char in self._message_text(message)
        )
        return estimated + (non_ascii * 3 // 4)

    @staticmethod
    def _pending_messages(value: Any) -> list[BaseMessage]:
        if not isinstance(value, dict):
            return []
        messages = value.get("messages")
        if not isinstance(messages, list):
            return []
        try:
            return list(convert_to_messages(messages))
        except (NotImplementedError, TypeError, ValueError):
            return []

    def _raise_if_context_unsafe(self, total_tokens: int) -> None:
        safe_limit = self.context_config.hard_limit - max(
            self.context_config.max_output_tokens,
            self.context_config.min_reserved_tokens,
        )
        if total_tokens > safe_limit:
            raise fail(
                "CONTEXT_LIMIT_EXCEEDED",
                "Context remains too large after safe compression "
                f"({total_tokens} tokens; safe limit {safe_limit}).",
            )

    def _run_with_approvals(
        self,
        value: Any,
        config: RunnableConfig,
        thread_id: str,
        approve: ApprovalCallback,
        recorder: TraceRecorder | None,
        on_token: TokenCallback | None,
        cancelled: threading.Event | None = None,
        cancellation_code: str = "OPERATION_CANCELLED",
    ) -> dict[str, Any]:
        """Run until completion while resolving HumanInTheLoop interrupts."""
        invalid_retries = 0
        while True:
            self._raise_if_cancelled(cancelled, thread_id, cancellation_code)
            self._maybe_compress_context(config, recorder, value)
            result = self._stream_graph(
                value,
                config,
                on_token,
                cancelled,
                thread_id,
                cancellation_code,
            )
            self._update_accountant_after_stream(result)
            while interrupts := result.get("__interrupt__"):
                self._raise_if_cancelled(cancelled, thread_id, cancellation_code)
                interrupt = interrupts[0]
                requests = interrupt.value.get("action_requests", [])
                decisions = []
                for request in requests:
                    approval_span_id = (
                        recorder.start_span(
                            "approval.wait",
                            kind="approval",
                            inputs=request,
                        )
                        if recorder is not None
                        else None
                    )
                    try:
                        approved = self._approve_request(thread_id, request, approve)
                    except BaseException as exc:
                        if recorder is not None and approval_span_id is not None:
                            recorder.end_span(approval_span_id, status="error", error=exc)
                        raise
                    if recorder is not None and approval_span_id is not None:
                        recorder.end_span(
                            approval_span_id,
                            attributes={"decision": "approve" if approved else "reject"},
                        )
                    decisions.append({"type": "approve" if approved else "reject"})
                result = self._stream_graph(
                    Command(resume={"decisions": decisions}),
                    config,
                    on_token,
                    cancelled,
                    thread_id,
                    cancellation_code,
                )
                self._update_accountant_after_stream(result)

            invalid_calls = self._terminal_invalid_tool_calls(result)
            if not invalid_calls:
                return result
            responses = [
                ToolMessage(
                    content=json.dumps(
                        {
                            "ok": False,
                            "error_code": "INVALID_TOOL_CALL",
                            "message": (
                                f"Arguments for {call.get('name') or 'tool'} were malformed. "
                                "Retry with smaller valid arguments."
                            ),
                        }
                    ),
                    tool_call_id=str(call["id"]),
                    status="error",
                )
                for call in invalid_calls
                if call.get("id")
            ]
            if not responses:
                raise fail(
                    "INVALID_TOOL_CALL",
                    "The model returned a malformed tool call without an ID.",
                )
            if invalid_retries >= 2:
                self.graph.update_state(config, {"messages": responses})
                raise fail(
                    "INVALID_TOOL_CALL",
                    "The model repeatedly returned malformed tool arguments.",
                )
            invalid_retries += 1
            value = {"messages": responses}

    def _repair_unresolved_invalid_tool_calls(self, config: RunnableConfig) -> None:
        """Remove legacy invalid calls that have no adjacent ToolMessage response."""
        snapshot = self.graph.get_state(config)
        values = getattr(snapshot, "values", {})
        messages = list(values.get("messages", []))
        replacements: list[AIMessage] = []
        for index, message in enumerate(messages):
            if not isinstance(message, AIMessage) or not message.invalid_tool_calls:
                continue
            following_ids: set[str] = set()
            for following in messages[index + 1 :]:
                if not isinstance(following, ToolMessage):
                    break
                following_ids.add(following.tool_call_id)
            retained = [
                call
                for call in message.invalid_tool_calls
                if call.get("id") in following_ids
            ]
            if len(retained) != len(message.invalid_tool_calls):
                replacements.append(
                    message.model_copy(update={"invalid_tool_calls": retained})
                )
        if replacements:
            self.graph.update_state(config, {"messages": replacements})

    def _repair_unresolved_tool_calls(self, config: RunnableConfig) -> None:
        """Repair tool calls left unmatched by cancellation or a failed resumed turn."""
        snapshot = self.graph.get_state(config)
        values = getattr(snapshot, "values", {})
        messages = list(values.get("messages", []))
        if not messages:
            return

        replacements: list[AIMessage] = []
        tail_responses: list[ToolMessage] = []
        for index, message in enumerate(messages):
            if not isinstance(message, AIMessage) or not message.tool_calls:
                continue
            following_ids: set[str] = set()
            cursor = index + 1
            while cursor < len(messages) and isinstance(messages[cursor], ToolMessage):
                following_ids.add(messages[cursor].tool_call_id)
                cursor += 1
            missing = [
                call
                for call in message.tool_calls
                if call.get("id") and str(call["id"]) not in following_ids
            ]
            if not missing:
                continue
            if cursor == len(messages):
                tail_responses.extend(self._interrupted_tool_responses(missing))
                continue
            retained = [
                call
                for call in message.tool_calls
                if call.get("id") and str(call["id"]) in following_ids
            ]
            replacements.append(message.model_copy(update={"tool_calls": retained}))
        if replacements:
            self.graph.update_state(config, {"messages": replacements})
        if tail_responses:
            self.graph.update_state(config, {"messages": tail_responses})

    @staticmethod
    def _interrupted_tool_responses(calls: list[Any]) -> list[ToolMessage]:
        return [
            ToolMessage(
                content=json.dumps(
                    {
                        "ok": False,
                        "error_code": "TOOL_CALL_INTERRUPTED",
                        "message": (
                            "The previous Agent operation ended before this tool call "
                            "completed. Re-evaluate whether it should be retried."
                        ),
                    }
                ),
                tool_call_id=str(call["id"]),
                name=str(call.get("name") or "tool"),
                status="error",
            )
            for call in calls
        ]

    @staticmethod
    def _terminal_invalid_tool_calls(result: dict[str, Any]) -> list[dict[str, Any]]:
        messages = result.get("messages", [])
        if not messages or not isinstance(messages[-1], AIMessage):
            return []
        return [dict(call) for call in messages[-1].invalid_tool_calls]

    def _stream_graph(
        self,
        value: Any,
        config: RunnableConfig,
        on_token: TokenCallback | None,
        cancelled: threading.Event | None = None,
        thread_id: str = "",
        cancellation_code: str = "OPERATION_CANCELLED",
    ) -> dict[str, Any]:
        """Consume message deltas while retaining the final values state."""
        result: dict[str, Any] | None = None
        stream_modes: list[StreamMode] = ["messages", "values"]
        events = self.graph.stream(
            value,
            config,
            stream_mode=stream_modes,
            durability="sync",
        )
        for event in events:
            self._raise_if_cancelled(cancelled, thread_id, cancellation_code)
            mode, data = cast("tuple[str, Any]", event)
            if mode == "values":
                result = cast("dict[str, Any]", data)
                continue
            if mode != "messages" or on_token is None:
                continue
            message, _metadata = cast("tuple[BaseMessage, dict[str, Any]]", data)
            if isinstance(message, AIMessageChunk):
                text = self._message_text(message)
                if text:
                    on_token(text)
        if result is None:
            raise RuntimeError("Agent stream completed without a values state.")
        return result

    def _raise_if_cancelled(
        self,
        cancelled: threading.Event | None,
        thread_id: str,
        cancellation_code: str,
    ) -> None:
        if cancelled is None or not cancelled.is_set():
            return
        self.tool_runs.cancel_thread(thread_id, reason="Agent operation cancelled")
        raise fail(cancellation_code, "Agent execution was cancelled.")

    @staticmethod
    def _is_timeout_error(error: BaseException) -> bool:
        """Recognize timeout wrappers without coupling to one HTTP client."""
        current: BaseException | None = error
        while current is not None:
            if "timeout" in type(current).__name__.lower():
                return True
            current = current.__cause__ or current.__context__
        return False

    def fork_checkpoint(
        self,
        *,
        source_thread_id: str,
        source_checkpoint_id: str,
        target_thread_id: str,
    ) -> str:
        """Copy one historical graph state into a new thread."""
        source = self.graph.get_state(self.graph_config(source_thread_id, source_checkpoint_id))
        result = self.graph.update_state(
            self.graph_config(target_thread_id),
            source.values,
        )
        return str(result["configurable"]["checkpoint_id"])

    def annotate_cancelled_context(self, *, thread_id: str) -> str:
        """Append a cancellation context marker and return the latest checkpoint ID.

        After a cancelled turn the execution thread contains the user message
        and may still have unresolved tool calls.  This method first repairs
        the message sequence so every AIMessage with tool_calls is immediately
        followed by matching ToolMessages, then appends a system-level summary.
        """
        config = self.graph_config(thread_id)

        # --- repair ---
        self._repair_unresolved_invalid_tool_calls(config)
        self._repair_unresolved_tool_calls(config)

        # Sanity: remove any orphaned tool_calls that still have no response.
        snapshot = self.graph.get_state(config)
        values = getattr(snapshot, "values", {})
        messages: list[BaseMessage] = list(values.get("messages", []))
        orphans: list[AIMessage] = []
        for index, msg in enumerate(messages):
            if not isinstance(msg, AIMessage) or not msg.tool_calls:
                continue
            responded: set[str] = set()
            cursor = index + 1
            while cursor < len(messages) and isinstance(messages[cursor], ToolMessage):
                responded.add(cast("ToolMessage", messages[cursor]).tool_call_id)
                cursor += 1
            unresponded = [
                call for call in msg.tool_calls
                if call.get("id") and str(call["id"]) not in responded
            ]
            if unresponded:
                kept = [
                    call for call in msg.tool_calls
                    if call.get("id") and str(call["id"]) in responded
                ]
                orphans.append(msg.model_copy(update={"tool_calls": kept}))
        if orphans:
            self.graph.update_state(config, {"messages": orphans})

        # --- count ---
        snapshot = self.graph.get_state(config)
        messages = list(getattr(snapshot, "values", {}).get("messages", []))
        tool_count = sum(
            1
            for msg in messages
            if isinstance(msg, ToolMessage)
            and "TOOL_CALL_INTERRUPTED" in str(getattr(msg, "content", ""))
        )

        tools_part = (
            f"{tool_count} background tool(s) were running and have been terminated."
            if tool_count
            else "No background tools were active."
        )
        context = (
            "[SYSTEM · CANCELLATION CONTEXT]\n"
            f"The previous operation was cancelled by the user. {tools_part} "
            "The workspace has NOT been modified by this turn. "
            "The user may resubmit the same request, modify it, "
            "or continue with a different task."
        )

        self.graph.update_state(
            config,
            {"messages": [HumanMessage(content=context)]},
        )
        state = self.graph.get_state(config)
        return str(state.config["configurable"]["checkpoint_id"])

    def close(self) -> None:
        """Close checkpoint storage and the MCP execution bridge."""
        self.tool_runs.close()
        self.connection.close()
        self.skill_execution.close()
        self.mcp_provider.close()
        self.execution_service.close()

    def _approve_request(
        self,
        thread_id: str,
        request: dict[str, Any],
        approve: ApprovalCallback,
    ) -> bool:
        if request.get("name") != "activate_skill":
            return approve(request)
        arguments = request.get("args")
        skill_name = arguments.get("name") if isinstance(arguments, dict) else None
        if not isinstance(skill_name, str):
            return False
        if self.skill_execution.is_authorized(thread_id, skill_name):
            return True
        approval_request = {
            **request,
            "skill_capabilities": self.skill_execution.activation_summary(skill_name),
        }
        approved = approve(approval_request)
        if approved:
            self.skill_execution.authorize(thread_id, skill_name)
        return approved

    @staticmethod
    def _message_text(message: BaseMessage) -> str:
        text = getattr(message, "text", None)
        if isinstance(text, str):
            return text
        return str(message.content)
