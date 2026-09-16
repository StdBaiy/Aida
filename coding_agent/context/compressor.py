"""Context compression strategies — from cheapest to most expensive.

Strategy pipeline (each applied in order until the target token budget is met):
  1. ToolResultBudget — persist oversized tool results to disk, keep a short preview.
  2. Microcompact — clear old tool_result content, keep last N intact.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from coding_agent.context.accounting import CompressUrgency, ContextAccountant
from coding_agent.context.config import ContextWindowConfig

_LOGGER = logging.getLogger(__name__)

# Thresholds
_TOOL_RESULT_PERSIST_CHARS = 50_000  # single tool_result threshold
_TOOL_RESULT_KEEP_RECENT = 3  # always keep last N tool results intact
_TOOL_RESULT_PREVIEW_CHARS = 2_000  # preview length for persisted results
_MICROCOMPACT_KEEP_RESULTS = 5  # keep last N tool results in microcompact
_EMERGENCY_KEEP_MESSAGES = 8
_EMERGENCY_PREVIEW_CHARS = 1_000

ArtifactWriter = Callable[[str], str]


@dataclass(frozen=True)
class CompressionResult:
    """One immutable compression attempt and its provenance."""

    messages: list[dict[str, Any]]
    original_tokens: int
    compressed_tokens: int
    strategies: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    changed_indices: tuple[int, ...]

    @property
    def changed(self) -> bool:
        return bool(self.changed_indices)

    @property
    def saved_tokens(self) -> int:
        return max(0, self.original_tokens - self.compressed_tokens)


class CompressionStrategy(ABC):
    """Base class for a single compression strategy."""

    name: str = "base"

    @abstractmethod
    def apply(
        self,
        messages: list[dict[str, Any]],
        persist_artifact: ArtifactWriter | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Compress a private message list and return created artifact IDs."""
        ...


class ToolResultBudget(CompressionStrategy):
    """第 ① 层：大工具结果落盘。

    超过 50K 字符的 tool_result 内容持久化为 artifact，
    上下文中只保留预览 + 路径引用。
    始终保留最近 3 条工具结果的完整内容。
    """

    name = "tool_result_budget"

    def __init__(
        self,
        persist_threshold_chars: int = _TOOL_RESULT_PERSIST_CHARS,
        keep_recent: int = _TOOL_RESULT_KEEP_RECENT,
        preview_chars: int = _TOOL_RESULT_PREVIEW_CHARS,
    ) -> None:
        self.persist_threshold_chars = persist_threshold_chars
        self.keep_recent = keep_recent
        self.preview_chars = preview_chars

    def apply(
        self,
        messages: list[dict[str, Any]],
        persist_artifact: ArtifactWriter | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Compress tool results by saving oversized ones to artifacts."""
        artifact_ids: list[str] = []
        tool_result_count = 0
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if not isinstance(msg, dict) or msg.get("role") != "tool":
                continue

            tool_result_count += 1
            if tool_result_count <= self.keep_recent:
                continue  # keep recent results intact

            content = _content_text(msg.get("content", ""))

            if len(content) > self.persist_threshold_chars and persist_artifact is not None:
                artifact_id = persist_artifact(content)
                preview = content[: self.preview_chars]
                messages[i] = {
                    **msg,
                    "_compression_artifact_id": artifact_id,
                    "content": (
                        f"[Tool result saved to artifact: {artifact_id}; "
                        f"{len(content)} chars. "
                        f"Preview:\n{preview}\n...]"
                    ),
                }
                artifact_ids.append(artifact_id)
        return messages, artifact_ids


class Microcompact(CompressionStrategy):
    """第 ③ 层：旧工具结果清除。

    清除阈值之前的所有 tool_result 内容，替换为简洁引用。
    保留最近的 N 条完整。
    不涉及对话内容（user 和 assistant 消息保持不变）。
    """

    name = "microcompact"

    def __init__(self, keep_results: int = _MICROCOMPACT_KEEP_RESULTS) -> None:
        self.keep_results = keep_results

    def apply(
        self,
        messages: list[dict[str, Any]],
        persist_artifact: ArtifactWriter | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Clear old tool result content, keep last N intact."""
        artifact_ids: list[str] = []
        tool_result_count = 0
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if not isinstance(msg, dict) or msg.get("role") != "tool":
                continue
            tool_result_count += 1
            if tool_result_count <= self.keep_results:
                continue
            artifact_id = msg.get("_compression_artifact_id")
            if not isinstance(artifact_id, str):
                if persist_artifact is None:
                    continue
                artifact_id = persist_artifact(_content_text(msg.get("content", "")))
                artifact_ids.append(artifact_id)
            messages[i] = {
                **msg,
                "_compression_artifact_id": artifact_id,
                "content": f"[Previous tool result saved to artifact: {artifact_id}]",
            }
        return messages, artifact_ids


class EmergencyConversationCompact(CompressionStrategy):
    """Compact old conversation text when tool-only strategies are insufficient."""

    name = "emergency_conversation_compact"

    def __init__(
        self,
        keep_messages: int = _EMERGENCY_KEEP_MESSAGES,
        preview_chars: int = _EMERGENCY_PREVIEW_CHARS,
    ) -> None:
        self.keep_messages = keep_messages
        self.preview_chars = preview_chars

    def apply(
        self,
        messages: list[dict[str, Any]],
        persist_artifact: ArtifactWriter | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        if persist_artifact is None:
            return messages, []
        artifact_ids: list[str] = []
        cutoff = max(0, len(messages) - self.keep_messages)
        for i, msg in enumerate(messages[:cutoff]):
            if msg.get("role") not in {"user", "assistant"}:
                continue
            content = _content_text(msg.get("content", ""))
            if len(content) <= self.preview_chars:
                continue
            artifact_id = persist_artifact(content)
            preview_head = content[: self.preview_chars // 2]
            preview_tail = content[-self.preview_chars // 2 :]
            messages[i] = {
                **msg,
                "_compression_artifact_id": artifact_id,
                "content": (
                    f"[Previous conversation message saved to artifact: {artifact_id}; "
                    f"{len(content)} chars]\n{preview_head}\n...\n{preview_tail}"
                ),
            }
            artifact_ids.append(artifact_id)
        return messages, artifact_ids


class ContextCompressor:
    """组合压缩管线。

    从低成本到高成本依次尝试各策略。
    每次压缩后检查是否达到目标 token 数，已满足则提前终止。
    所有操作都在内存中进行，不修改持久化状态。
    """

    def __init__(
        self,
        config: ContextWindowConfig,
        accountant: ContextAccountant,
    ) -> None:
        self.config = config
        self.accountant = accountant
    def compress(
        self,
        messages: list[dict[str, Any]],
        urgency: CompressUrgency,
        *,
        persist_artifact: ArtifactWriter | None = None,
    ) -> CompressionResult:
        """Run the compression pipeline on a list of message dicts.

        Args:
            messages: The message list to compress. Each message is a dict
                     with at least "role" and "content" keys.
            urgency: The compression urgency level.

        Returns:
            Structured compression result. The input list is never mutated.
        """
        original_tokens = self._estimate(messages)
        if not messages or urgency == CompressUrgency.NONE:
            return CompressionResult(
                messages=deepcopy(messages),
                original_tokens=original_tokens,
                compressed_tokens=original_tokens,
                strategies=(),
                artifact_ids=(),
                changed_indices=(),
            )

        original_messages = deepcopy(messages)
        compressed_messages = deepcopy(messages)
        current_tokens = original_tokens
        target_tokens = int(self.config.soft_limit * self.config.compression_target_ratio)
        pipeline = self._build_pipeline(urgency)
        strategies: list[str] = []
        artifact_ids: list[str] = []

        for strategy in pipeline:
            if current_tokens <= target_tokens:
                break
            try:
                compressed_messages, created_artifacts = strategy.apply(
                    compressed_messages,
                    persist_artifact,
                )
                after_tokens = self._estimate(compressed_messages)
                saved = max(0, current_tokens - after_tokens)
                if saved > 0:
                    _LOGGER.info(
                        "Compression strategy %s saved ~%d tokens",
                        strategy.name,
                        saved,
                    )
                    strategies.append(strategy.name)
                    artifact_ids.extend(created_artifacts)
                current_tokens = after_tokens
            except Exception as exc:
                _LOGGER.warning("Compression strategy %s failed: %s", strategy.name, exc)
                continue

        changed_indices = tuple(
            i
            for i, (original, compressed) in enumerate(
                zip(original_messages, compressed_messages, strict=True)
            )
            if original.get("content") != compressed.get("content")
        )
        return CompressionResult(
            messages=compressed_messages,
            original_tokens=original_tokens,
            compressed_tokens=current_tokens,
            strategies=tuple(strategies),
            artifact_ids=tuple(dict.fromkeys(artifact_ids)),
            changed_indices=changed_indices,
        )

    def _build_pipeline(self, urgency: CompressUrgency) -> list[CompressionStrategy]:
        """Build the strategy pipeline ordered by cost."""
        pipeline: list[CompressionStrategy] = [
            ToolResultBudget(),
            Microcompact(
                keep_results=(
                    1 if urgency == CompressUrgency.EMERGENCY else _TOOL_RESULT_KEEP_RECENT
                )
            ),
        ]
        if urgency == CompressUrgency.EMERGENCY:
            pipeline.append(EmergencyConversationCompact())
        return pipeline

    def _estimate(self, messages: list[dict[str, Any]]) -> int:
        return self.accountant.estimate_messages(
            [_content_text(message.get("content", "")) for message in messages]
        )


def _content_text(content: Any) -> str:
    if isinstance(content, list):
        text_parts = [
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
            if not isinstance(block, dict) or block.get("type") == "text"
        ]
        return "\n".join(text_parts)
    if not isinstance(content, str):
        return json.dumps(content, ensure_ascii=False) if content is not None else ""
    return content
