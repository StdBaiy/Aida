"""Context window token accounting — fast estimation and slow-path correction."""

from __future__ import annotations

import math
from enum import Enum
from typing import Any

from coding_agent.context.config import ContextWindowConfig

# Cache-hit price tiers for DeepSeek Flash
_CACHE_HIT_PRICE_PER_1M = 0.003
_CACHE_MISS_PRICE_PER_1M = 0.14
_OUTPUT_PRICE_PER_1M = 0.28

# Rough per-message overhead in tokens (role label, metadata)
_ROLE_OVERHEAD = 4
_TOOL_CALL_OVERHEAD = 8


class CompressUrgency(Enum):
    """Context compression urgency level."""

    NONE = "none"
    NORMAL = "normal"
    EMERGENCY = "emergency"


class ContextAccountant:
    """上下文窗口记账员。

    双重记账策略：
    - Fast path: 逐条消息估算 token 数（基于字符数近似）
    - Slow path: 模型返回后从 usage.prompt_cache_hit_tokens +
                 usage.prompt_cache_miss_tokens 获取精确值修正

    Thread-safe: 期望单线程使用（在 runtime.run_turn 的线程内）。
    """

    def __init__(self, config: ContextWindowConfig) -> None:
        self.config = config

        # Fast-path estimates (updated before model call)
        self._estimated_total: int = 0
        self._message_count: int = 0
        self._has_exact_usage = False

        # Slow-path exact counts (updated from model response usage)
        self.total_prompt_tokens: int = 0
        self.cache_hit_tokens: int = 0
        self.cache_miss_tokens: int = 0
        self.completion_tokens: int = 0

        # Compression tracking
        self.compression_count: int = 0
        self.last_strategy: str | None = None

    # ── Fast-path estimation ──

    def estimate_text(self, text: str) -> int:
        """Estimate mixed ASCII/CJK text without undercounting CJK characters."""
        non_ascii = sum(not char.isascii() for char in text)
        ascii_chars = len(text) - non_ascii
        return max(1, math.ceil(ascii_chars / 4) + non_ascii)

    def estimate_message(self, content: str, has_tool_calls: bool = False) -> int:
        """Estimate tokens for a single message."""
        overhead = _ROLE_OVERHEAD + (_TOOL_CALL_OVERHEAD if has_tool_calls else 0)
        return overhead + self.estimate_text(content)

    def estimate_messages(self, contents: list[str]) -> int:
        """Batch-estimate tokens for a list of message content strings."""
        return sum(_ROLE_OVERHEAD + self.estimate_text(content) for content in contents)

    def update_estimate(
        self,
        total_tokens: int,
        message_count: int,
        *,
        force: bool = False,
    ) -> None:
        """Update the fast-path estimate and current message count."""
        self._message_count = max(message_count, 0)
        if force or not self._has_exact_usage:
            self._estimated_total = max(total_tokens, 0)
            self._has_exact_usage = False

    # ── Slow-path correction (called from callback after model returns) ──

    def record_usage(self, usage: dict[str, Any]) -> None:
        """Record exact token usage from the model response.

        DeepSeek usage format:
            prompt_tokens, completion_tokens,
            prompt_cache_hit_tokens, prompt_cache_miss_tokens
        """
        self.total_prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        self.completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        self.cache_hit_tokens = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
        self.cache_miss_tokens = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
        self._estimated_total = self.total_prompt_tokens + self.completion_tokens
        self._has_exact_usage = self.total_prompt_tokens > 0

    # ── Read-only computed properties ──

    @property
    def total_tokens(self) -> int:
        """Current total tokens (estimated or exact, whichever is newer)."""
        return self._estimated_total

    @total_tokens.setter
    def total_tokens(self, value: int) -> None:
        self._estimated_total = value

    @property
    def has_exact_usage(self) -> bool:
        """Whether total_tokens currently comes from a model response."""
        return self._has_exact_usage

    @property
    def cache_hit_rate(self) -> float:
        """Prompt cache hit rate as a fraction 0..1."""
        total = self.cache_hit_tokens + self.cache_miss_tokens
        return self.cache_hit_tokens / max(total, 1)

    @property
    def effective_cost_per_1m(self) -> float:
        """Blended input cost combining cache hit & miss prices."""
        total = self.cache_hit_tokens + self.cache_miss_tokens
        if total == 0:
            return _CACHE_MISS_PRICE_PER_1M
        hit_ratio = self.cache_hit_tokens / total
        return _CACHE_HIT_PRICE_PER_1M * hit_ratio + _CACHE_MISS_PRICE_PER_1M * (1 - hit_ratio)

    @property
    def estimated_cost_usd(self) -> float:
        """Estimated cost for the current usage."""
        hit_cost = self.cache_hit_tokens / 1_000_000 * _CACHE_HIT_PRICE_PER_1M
        miss_cost = self.cache_miss_tokens / 1_000_000 * _CACHE_MISS_PRICE_PER_1M
        output_cost = self.completion_tokens / 1_000_000 * _OUTPUT_PRICE_PER_1M
        return hit_cost + miss_cost + output_cost

    @property
    def estimated_savings_usd(self) -> float:
        """Estimated savings from cache hits vs no-cache baseline."""
        total_input = self.cache_hit_tokens + self.cache_miss_tokens
        no_cache_cost = total_input / 1_000_000 * _CACHE_MISS_PRICE_PER_1M
        actual_cost = (
            self.cache_hit_tokens / 1_000_000 * _CACHE_HIT_PRICE_PER_1M
            + self.cache_miss_tokens / 1_000_000 * _CACHE_MISS_PRICE_PER_1M
        )
        return no_cache_cost - actual_cost

    def usage_ratio(self) -> float:
        """Current usage as a fraction of hard_limit (0..1)."""
        return self._estimated_total / max(self.config.hard_limit, 1)

    # ── Urgency decision ──

    def should_compress(self) -> CompressUrgency:
        """Determine whether compression should be triggered."""
        ratio = self.usage_ratio()
        if ratio * self.config.hard_limit >= self.config.emergency_threshold:
            return CompressUrgency.EMERGENCY
        if ratio * self.config.hard_limit >= self.config.soft_limit:
            return CompressUrgency.NORMAL
        return CompressUrgency.NONE

    # ── Cache-aware override ──

    def should_compress_cached(self) -> CompressUrgency:
        """Cache-aware urgency: raise the bar when cache hit rate is high.

        If the cache hit rate > 70%, the effective cost is very low,
        so we tolerate higher usage before compressing.
        """
        base = self.should_compress()
        if base == CompressUrgency.NONE:
            return CompressUrgency.NONE
        if not self.config.cache_aware:
            return base
        if self.cache_hit_rate > 0.7:
            # At high cache rates, only emergency triggers compression
            if base == CompressUrgency.EMERGENCY:
                return CompressUrgency.EMERGENCY
            # Raise the bar by 10% of hard_limit
            effective_threshold = self.config.soft_limit + int(self.config.hard_limit * 0.1)
            if self._estimated_total >= effective_threshold:
                return CompressUrgency.NORMAL
            return CompressUrgency.NONE
        return base

    def mark_compressed(self, strategy: str, total_tokens: int) -> None:
        """Record that a compression event occurred."""
        self.compression_count += 1
        self.last_strategy = strategy
        self._estimated_total = max(0, total_tokens)
        self._has_exact_usage = False

    def snapshot(self) -> dict[str, Any]:
        """Return a portable snapshot for event emission and persistence."""
        return {
            "total_tokens": self.total_tokens,
            "hard_limit": self.config.hard_limit,
            "usage_ratio": round(self.usage_ratio(), 4),
            "urgency": self.should_compress_cached().value,
            "message_count": self._message_count,
            "compression_count": self.compression_count,
            "cache_hit_tokens": self.cache_hit_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "effective_cost_per_1m": round(self.effective_cost_per_1m, 6),
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "estimated_savings_usd": round(self.estimated_savings_usd, 6),
        }
