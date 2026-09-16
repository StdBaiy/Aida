"""Prompt cache hit/miss tracking and cost accounting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass
class CacheSnapshot:
    """One model request's cache statistics."""

    timestamp: str
    prompt_tokens: int = 0
    prompt_cache_hit_tokens: int = 0
    prompt_cache_miss_tokens: int = 0
    completion_tokens: int = 0

    @property
    def cache_hit_rate(self) -> float:
        total = self.prompt_cache_hit_tokens + self.prompt_cache_miss_tokens
        return self.prompt_cache_hit_tokens / max(total, 1)

    @property
    def estimated_cost_usd(self) -> float:
        """Estimate cost in USD based on DeepSeek Flash pricing."""
        hit_cost = self.prompt_cache_hit_tokens / 1_000_000 * 0.003
        miss_cost = self.prompt_cache_miss_tokens / 1_000_000 * 0.14
        output_cost = self.completion_tokens / 1_000_000 * 0.28
        return hit_cost + miss_cost + output_cost

    @property
    def estimated_savings_usd(self) -> float:
        """Savings from cache hits vs. no-cache baseline."""
        total_input = self.prompt_cache_hit_tokens + self.prompt_cache_miss_tokens
        no_cache_cost = total_input / 1_000_000 * 0.14
        return no_cache_cost - (
            self.prompt_cache_hit_tokens / 1_000_000 * 0.003
            + self.prompt_cache_miss_tokens / 1_000_000 * 0.14
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "prompt_tokens": self.prompt_tokens,
            "cache_hit_tokens": self.prompt_cache_hit_tokens,
            "cache_miss_tokens": self.prompt_cache_miss_tokens,
            "completion_tokens": self.completion_tokens,
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "estimated_savings_usd": round(self.estimated_savings_usd, 6),
        }


class CacheStatsTracker:
    """Aggregate cache statistics across a session."""

    def __init__(self, session_id: str, model: str = "deepseek-flash") -> None:
        self.session_id = session_id
        self.model = model
        self.snapshots: list[CacheSnapshot] = []

    def record(self, snapshot: CacheSnapshot) -> None:
        """Record one model request's cache stats."""
        self.snapshots.append(snapshot)

    def record_usage(
        self,
        prompt_tokens: int,
        cache_hit_tokens: int,
        cache_miss_tokens: int,
        completion_tokens: int,
    ) -> None:
        """Record cache stats from raw usage values."""
        self.record(
            CacheSnapshot(
                timestamp=datetime.now(UTC).isoformat(),
                prompt_tokens=prompt_tokens,
                prompt_cache_hit_tokens=cache_hit_tokens,
                prompt_cache_miss_tokens=cache_miss_tokens,
                completion_tokens=completion_tokens,
            )
        )

    @property
    def request_count(self) -> int:
        return len(self.snapshots)

    @property
    def total_prompt_tokens(self) -> int:
        return sum(s.prompt_tokens for s in self.snapshots)

    @property
    def total_cache_hit_tokens(self) -> int:
        return sum(s.prompt_cache_hit_tokens for s in self.snapshots)

    @property
    def total_cache_miss_tokens(self) -> int:
        return sum(s.prompt_cache_miss_tokens for s in self.snapshots)

    @property
    def total_completion_tokens(self) -> int:
        return sum(s.completion_tokens for s in self.snapshots)

    @property
    def overall_cache_hit_rate(self) -> float:
        total = self.total_cache_hit_tokens + self.total_cache_miss_tokens
        return self.total_cache_hit_tokens / max(total, 1)

    @property
    def total_cost_usd(self) -> float:
        return sum(s.estimated_cost_usd for s in self.snapshots)

    @property
    def total_savings_usd(self) -> float:
        return sum(s.estimated_savings_usd for s in self.snapshots)

    @property
    def blended_cost_per_1m(self) -> float:
        total_input = self.total_cache_hit_tokens + self.total_cache_miss_tokens
        if total_input == 0:
            return 0.14
        return (
            self.total_cache_hit_tokens * 0.003 + self.total_cache_miss_tokens * 0.14
        ) / total_input

    def summary(self) -> dict[str, Any]:
        """Aggregate summary for reporting."""
        return {
            "model": self.model,
            "total_requests": self.request_count,
            "total_input_tokens": self.total_prompt_tokens,
            "total_cache_hit_tokens": self.total_cache_hit_tokens,
            "total_cache_miss_tokens": self.total_cache_miss_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "overall_cache_hit_rate": round(self.overall_cache_hit_rate, 4),
            "total_cost_usd": round(self.total_cost_usd, 6),
            "total_savings_usd": round(self.total_savings_usd, 6),
            "blended_cost_per_1m": round(self.blended_cost_per_1m, 6),
            "cost_if_no_cache_usd": round(self.total_cost_usd + self.total_savings_usd, 6),
        }
