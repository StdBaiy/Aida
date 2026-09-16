"""Context window configuration by model preset."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextWindowConfig:
    """上下文窗口配置，按模型预设。

    Attributes:
        model: 模型名称（如 "deepseek-flash"）。
        hard_limit: 模型硬上限 (tokens)。
        soft_limit: 触发标准压缩的软阈值。
        emergency_threshold: 触发紧急压缩的阈值。
        compression_target_ratio: 压缩目标 — 压缩后占 soft_limit 的比例。
        max_output_tokens: 为模型输出预留的 token 数。
        min_reserved_tokens: 压缩后至少保留的 token 数（给后续模型输出）。
        max_messages: 上下文中的最大消息条数上限。
        min_turns_before_compress: 至少经过多少个 turn 后才允许压缩。
        min_messages_before_compress: 至少积累多少条消息后才允许压缩。
        cache_aware: 是否感知 prompt cache 做压缩决策。
    """

    model: str
    hard_limit: int = 1_000_000
    soft_limit: int = 700_000
    emergency_threshold: int = 900_000
    compression_target_ratio: float = 0.4
    max_output_tokens: int = 32_000
    min_reserved_tokens: int = 32_000
    max_messages: int = 300
    min_turns_before_compress: int = 5
    min_messages_before_compress: int = 30
    cache_aware: bool = True


# ── 预设注册表 ──
CONTEXT_WINDOW_PRESETS: dict[str, ContextWindowConfig] = {
    "deepseek-flash": ContextWindowConfig(
        model="deepseek-flash",
        hard_limit=1_000_000,
        soft_limit=700_000,
        emergency_threshold=900_000,
    ),
    "deepseek-chat": ContextWindowConfig(
        model="deepseek-chat",
        hard_limit=1_000_000,
        soft_limit=700_000,
        emergency_threshold=900_000,
    ),
}


def resolve_context_config(model: str) -> ContextWindowConfig:
    """Resolve a model string to its ContextWindowConfig preset.

    Falls back to a reasonable default if the model is unknown.
    """
    # Strip known prefixes
    for prefix in ("openai:", "azure:"):
        if model.startswith(prefix):
            model = model.removeprefix(prefix)
    preset = CONTEXT_WINDOW_PRESETS.get(model)
    if preset is not None:
        return preset
    # Fallback: use a safe default
    if "deepseek" in model.lower():
        return CONTEXT_WINDOW_PRESETS["deepseek-flash"]
    return ContextWindowConfig(
        model=model,
        hard_limit=128_000,
        soft_limit=90_000,
        emergency_threshold=110_000,
        max_output_tokens=16_000,
        min_reserved_tokens=16_000,
    )
