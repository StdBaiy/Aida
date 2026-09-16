"""Model callback that feeds exact provider usage into context accounting."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from coding_agent.context.accounting import ContextAccountant


class ContextUsageCallbackHandler(BaseCallbackHandler):
    """Record the latest model request usage for preflight context decisions."""

    run_inline = True
    raise_error = False

    def __init__(self, accountant: ContextAccountant) -> None:
        super().__init__()
        self.accountant = accountant

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        del run_id, kwargs
        usage = extract_token_usage(response)
        if usage.get("prompt_tokens", 0):
            self.accountant.record_usage(usage)


def extract_token_usage(response: LLMResult) -> dict[str, int]:
    """Normalize provider and LangChain usage metadata."""
    raw_usage = (response.llm_output or {}).get("token_usage", {})
    usage = dict(raw_usage) if isinstance(raw_usage, dict) else {}
    try:
        generation = response.generations[0][0]
        if isinstance(generation, ChatGeneration) and isinstance(
            generation.message,
            AIMessage,
        ):
            message = generation.message
            metadata = cast("dict[str, Any]", message.usage_metadata or {})
            response_usage = message.response_metadata.get("token_usage", {})
            if isinstance(response_usage, dict):
                usage = {**response_usage, **usage}
            usage.setdefault("prompt_tokens", metadata.get("input_tokens", 0))
            usage.setdefault("completion_tokens", metadata.get("output_tokens", 0))
            details = metadata.get("input_token_details") or {}
            if isinstance(details, dict):
                usage.setdefault(
                    "prompt_cache_hit_tokens",
                    details.get("cache_read", 0),
                )
    except (AttributeError, IndexError, TypeError):
        pass

    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    cache_hit_tokens = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
    cache_miss_tokens = usage.get("prompt_cache_miss_tokens")
    if cache_miss_tokens is None and prompt_tokens:
        cache_miss_tokens = max(0, prompt_tokens - cache_hit_tokens)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
        "prompt_cache_hit_tokens": cache_hit_tokens,
        "prompt_cache_miss_tokens": int(cache_miss_tokens or 0),
    }
