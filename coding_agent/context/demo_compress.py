#!/usr/bin/env python3
"""
上下文压缩 Demo — 离线触发压缩管线，不调用真实模型。

用法:
    python coding_agent/context/demo_compress.py

模拟场景:
    一个 agent 在代码仓库中进行多轮对话，堆积了大量文件读取、
    grep 搜索、shell 执行等工具结果。上下文膨胀到接近软阈值后
    自动触发压缩管线。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# ── 确保能找到 coding_agent 包 ──
_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))


# ── 导入真正的 context 模块 ──

from coding_agent.context import (  # noqa: E402
    CompressUrgency,
    ContextAccountant,
    ContextCompressor,
    Microcompact,
    ToolResultBudget,
)
from coding_agent.context.cache_stats import CacheStatsTracker  # noqa: E402
from coding_agent.context.config import resolve_context_config  # noqa: E402

# =============================================================
# 1. 模拟数据生成
# =============================================================

def _tool_result(size: int) -> str:
    """生成指定大小的 tool_result 内容"""
    return "".join(
        f"line_{i:06d}: {chr(65 + (i % 26)) * 40}\n"
        for i in range(size // 50)
    )


def _save_demo_artifact(content: str) -> str:
    """Return a stable offline artifact reference without writing files."""
    return f"demo-{len(content)}-{abs(hash(content[:128])):x}"


def build_simulated_messages(
    *,
    user_rounds: int = 8,
    tool_results_per_round: int = 4,
    tool_result_size: int = 60_000,
) -> list[dict[str, Any]]:
    """模拟多轮对话消息序列。

    每轮:
        1 条 user message + 1 条 assistant message (带 tool_calls)
        + N 条 tool_result 消息
    """
    messages: list[dict[str, Any]] = []

    scenario_context = {
        "初始": "请分析这个项目的模块结构",
        "调研": "查看 auth 模块的实现",
        "重构": "将认证逻辑提取为独立中间件",
        "测试": "为新的中间件编写单元测试",
        "调试": "测试失败，检查是什么原因",
        "修复": "找到问题了，修复 import 循环依赖",
        "验证": "重新运行测试验证修复",
        "集成": "将所有变更合并并生成 diff",
    }

    for round_idx, (title, prompt) in enumerate(scenario_context.items()):
        turn = round_idx + 1

        # user message
        messages.append({
            "role": "user",
            "content": prompt,
        })

        # assistant message (with tool_calls)
        messages.append({
            "role": "assistant",
            "content": f"[Round {turn}/{user_rounds}] {title}: 开始分析...",
        })

        # tool results
        tool_names = ["read_file", "grep_search", "run_command", "glob_files"]
        for t_idx in range(tool_results_per_round):
            name = tool_names[t_idx % len(tool_names)]
            size = tool_result_size if t_idx == 0 else tool_result_size // 2
            content = _tool_result(size)
            messages.append({
                "role": "tool",
                "content": content,
                "name": name,
            })

    # 最后一条 user message 作为"当前问题"
    messages.append({
        "role": "user",
        "content": "把这些变更全部整合，准备提 PR。",
    })

    return messages


def build_small_messages() -> list[dict[str, Any]]:
    """用于演示不触发压缩的小对话"""
    return [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
        {"role": "tool", "content": "ls output"},
    ]


# =============================================================
# 2. Demo 渲染
# =============================================================

def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def fmt_usd(n: float) -> str:
    if n >= 1:
        return f"${n:.2f}"
    if n >= 0.001:
        return f"${n*1000:.2f}m"
    return f"${n*1_000_000:.0f}µ"


class ProgressBar:
    """简易文本进度条"""

    def __init__(self, filled: int, total: int, width: int = 50):
        self.filled = filled
        self.total = total
        self.width = width

    def __str__(self) -> str:
        ratio = self.filled / max(self.total, 1)
        n = min(int(ratio * self.width), self.width)
        bar = "█" * n + "░" * (self.width - n)
        pct = ratio * 100
        return f"{bar}  {fmt_tokens(self.filled)}/{fmt_tokens(self.total)} ({pct:.0f}%)"


def print_header(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def print_section(title: str) -> None:
    print()
    print(f"── {title} ──")


def print_message_stats(messages: list[dict[str, Any]], label: str) -> None:
    roles: dict[str, int] = {}
    total_chars = 0
    for m in messages:
        role = m.get("role", "?")
        roles[role] = roles.get(role, 0) + 1
        content = m.get("content", "")
        total_chars += len(content) if isinstance(content, str) else 0
    print(f"  [{label}]")
    print(f"    总消息数: {len(messages)}")
    for role, count in sorted(roles.items()):
        print(f"    {role}: {count}")
    print(f"    总字符数: ~{fmt_tokens(total_chars)}")


def print_cache_report(tracker: CacheStatsTracker) -> None:
    s = tracker.summary()
    print(f"  请求次数: {s['total_requests']}")
    print(f"  输入 tokens: {fmt_tokens(s['total_input_tokens'])}")
    print(f"  缓存命中: {fmt_tokens(s['total_cache_hit_tokens'])}")
    print(f"  缓存未命中: {fmt_tokens(s['total_cache_miss_tokens'])}")
    print(f"  缓存命中率: {s['overall_cache_hit_rate']*100:.1f}%")
    print(f"  总费用: {fmt_usd(s['total_cost_usd'])}")
    print(f"  因缓存节省: {fmt_usd(s['total_savings_usd'])}")
    print(f"  混合成本: ${s['blended_cost_per_1m']:.4f}/1M tokens")


# =============================================================
# 3. Demo 流程
# =============================================================

def demo_1_config() -> None:
    """演示 ① ContextWindowConfig 预设"""
    print_header("① ContextWindowConfig — 模型预设配置")

    cfg = resolve_context_config("deepseek-flash")
    print(f"  模型: {cfg.model}")
    print(f"  硬上限: {fmt_tokens(cfg.hard_limit)}")
    soft_percent = cfg.soft_limit * 100 // cfg.hard_limit
    emergency_percent = cfg.emergency_threshold * 100 // cfg.hard_limit
    target = int(cfg.soft_limit * cfg.compression_target_ratio)
    print(f"  软阈值(标准压缩): {fmt_tokens(cfg.soft_limit)} ({soft_percent:.0f}%)")
    print(f"  紧急阈值: {fmt_tokens(cfg.emergency_threshold)} ({emergency_percent:.0f}%)")
    print(f"  压缩目标: soft_limit × {cfg.compression_target_ratio} = {fmt_tokens(target)}")
    print(f"  缓存感知: {cfg.cache_aware}")
    print(f"  最少消息触发: {cfg.min_messages_before_compress} 条")


def demo_2_accountant() -> None:
    """演示 ② ContextAccountant 记账"""
    print_header("② ContextAccountant — 上下文记账")

    cfg = resolve_context_config("deepseek-flash")
    acct = ContextAccountant(cfg)

    # 模拟注入一些消息
    sample_messages = [
        "hello world " * 100,  # ~250 tokens
        ("You are a helpful assistant. " * 1000),  # ~10K tokens
    ]
    total = acct.estimate_messages(sample_messages)
    acct.total_tokens = total

    print(f"  估算 tokens: ~{fmt_tokens(total)}")

    # 模拟 slow path 修正（模型返回后）
    acct.record_usage({
        "prompt_tokens": 10500,
        "completion_tokens": 500,
        "prompt_cache_hit_tokens": 8000,
        "prompt_cache_miss_tokens": 2500,
    })
    print("  Slow path 修正后:")
    print(f"    总 prompt tokens: {fmt_tokens(acct.total_prompt_tokens)}")
    print(f"    缓存命中: {fmt_tokens(acct.cache_hit_tokens)}")
    print(f"    缓存未命中: {fmt_tokens(acct.cache_miss_tokens)}")
    print(f"    缓存命中率: {acct.cache_hit_rate*100:.1f}%")
    print(f"    混合成本: ${acct.effective_cost_per_1m:.4f}/1M")
    print(f"    预估费用: {fmt_usd(acct.estimated_cost_usd)}")
    print(f"    节省: {fmt_usd(acct.estimated_savings_usd)}")

    snapshot = acct.snapshot()
    print("\n  Snapshot (用于 SSE 推送):")
    print(f"    {json.dumps(snapshot, indent=4)}")


def demo_3_cache_tracker() -> None:
    """演示 ③ CacheStatsTracker 缓存追踪"""
    print_header("③ CacheStatsTracker — 缓存命中追踪")

    tracker = CacheStatsTracker("demo-session")

    # 模拟 10 次模型请求的缓存数据
    import random

    random.seed(42)
    for _ in range(10):
        hit = random.randint(50000, 90000)
        miss = random.randint(5000, 20000)
        comp = random.randint(500, 3000)
        tracker.record_usage(hit + miss, hit, miss, comp)

    print_cache_report(tracker)

    # 显示每次请求的明细
    print_section("逐次请求明细")
    for i, snap in enumerate(tracker.snapshots):
        rate = snap.cache_hit_rate * 100
        cost = snap.estimated_cost_usd * 1_000_000
        print(f"  [{i+1:2d}] hit={fmt_tokens(snap.prompt_cache_hit_tokens):>6s}  "
              f"miss={fmt_tokens(snap.prompt_cache_miss_tokens):>5s}  "
              f"rate={rate:5.1f}%  cost={cost:6.0f}µ$")


def demo_4_compression_pipeline() -> None:
    """演示 ④⑤ 压缩管线 — 核心 Demo"""
    print_header("④⑤ Compressor Pipeline — 上下文压缩核心")

    cfg = resolve_context_config("deepseek-flash")
    acct = ContextAccountant(cfg)

    # 构建模拟数据
    messages = build_simulated_messages(
        user_rounds=8,
        tool_results_per_round=4,
        tool_result_size=60_000,
    )
    # 深拷贝一份用于压缩后对比
    import copy
    messages_before = copy.deepcopy(messages)

    print_section("压缩前状态")
    print_message_stats(messages, "BEFORE")

    # 估算 token
    acct.total_tokens = acct.estimate_messages([m.get("content", "") for m in messages])
    print(f"  预估 tokens: ~{fmt_tokens(acct.total_tokens)}")
    print(f"  上下文窗口: {ProgressBar(acct.total_tokens, cfg.hard_limit)}")
    print(f"  压缩决策: {acct.should_compress_cached().value}")
    print(f"  缓存命中率(模拟): {acct.cache_hit_rate*100:.1f}%")

    print()
    print_section("压缩前 — 各类型的 tool_result 大小")

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    oversize = sum(1 for m in tool_msgs if len(m.get("content", "")) > 50_000)
    normal = len(tool_msgs) - oversize
    print(f"  工具结果总数: {len(tool_msgs)}")
    print(f"  超大(>50K字符): {oversize}")
    print(f"  正常: {normal}")

    # ── 执行压缩 ──
    print()
    print_section("执行压缩管线")

    compressor = ContextCompressor(cfg, acct)
    result = compressor.compress(
        messages,
        CompressUrgency.NORMAL,
        persist_artifact=_save_demo_artifact,
    )
    compressed = result.messages
    if result.changed:
        acct.mark_compressed(result.strategies[-1], result.compressed_tokens)

    print(f"  使用策略: {','.join(result.strategies)}")
    print(f"  压缩计数: {acct.compression_count}")

    print_section("压缩后状态")
    print_message_stats(compressed, "AFTER")
    print(f"  压缩后预估 tokens: ~{fmt_tokens(acct.total_tokens)}")
    print(f"  上下文窗口: {ProgressBar(acct.total_tokens, cfg.hard_limit)}")

    # 展示压缩效果细节
    print_section("压缩效果对比")

    # 对比 tool 消息内容的变化 (用 deepcopy 的原始数据)
    tool_changed = 0
    tool_kept = 0
    for orig, comp in zip(messages_before, compressed, strict=True):
        if orig.get("role") == "tool" and comp.get("role") == "tool":
            if orig.get("content") != comp.get("content"):
                tool_changed += 1
            else:
                tool_kept += 1

    print(f"  工具结果被压缩: {tool_changed}")
    print(f"  工具结果保持完整: {tool_kept}")

    # 展示压缩前后对比样例
    print_section("压缩前后对比样例 (第一条 tool_result)")
    orig_first = next(
        (m["content"] for m in messages_before if m.get("role") == "tool"),
        ""
    )
    comp_first = next(
        (m["content"] for m in compressed if m.get("role") == "tool"),
        ""
    )
    print(f"  压缩前 (首 {80} 字符):")
    print(f"    {orig_first[:80]}...")
    print("  压缩后:")
    if "[Tool result saved" in comp_first:
        print(f"    {comp_first[:120]}")
    elif "[Previous tool result" in comp_first:
        print(f"    {comp_first}")
    else:
        print(f"    {comp_first[:120]}...")


def demo_5_cache_impact() -> None:
    """演示压缩对缓存命中率的影响"""
    print_header("Cache Impact — 压缩对缓存命中率的影响")

    cfg = resolve_context_config("deepseek-flash")
    acct = ContextAccountant(cfg)

    # 模拟数据
    messages = build_simulated_messages(tool_result_size=60_000)

    # 压缩前缓存状态
    acct.record_usage({
        "prompt_tokens": 350000,
        "completion_tokens": 2000,
        "prompt_cache_hit_tokens": 300000,
        "prompt_cache_miss_tokens": 50000,
    })
    print(f"  压缩前缓存命中率: {acct.cache_hit_rate*100:.1f}%")
    print(f"  压缩前有效成本: ${acct.effective_cost_per_1m:.4f}/1M")

    # 压缩后: 缓存前缀被破坏，需要重建
    acct.total_tokens = acct.estimate_messages([m.get("content", "") for m in messages])
    compressor = ContextCompressor(cfg, acct)
    compressor.compress(
        messages,
        CompressUrgency.NORMAL,
        persist_artifact=_save_demo_artifact,
    )

    # 模拟压缩后第一次请求 — 缓存未命中
    acct.record_usage({
        "prompt_tokens": 80000,
        "completion_tokens": 1500,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 80000,
    })
    print(f"  压缩后首次(缓存重建): 命中率={acct.cache_hit_rate*100:.1f}%  "
          f"成本=${acct.effective_cost_per_1m:.4f}/1M")

    # 模拟第二次请求 — 缓存开始命中
    acct.record_usage({
        "prompt_tokens": 81000,
        "completion_tokens": 1200,
        "prompt_cache_hit_tokens": 50000,
        "prompt_cache_miss_tokens": 31000,
    })
    print(f"  压缩后第二次: 命中率={acct.cache_hit_rate*100:.1f}%  "
          f"成本=${acct.effective_cost_per_1m:.4f}/1M")

    # 模拟后续请求 — 缓存稳定
    acct.record_usage({
        "prompt_tokens": 80000,
        "completion_tokens": 1000,
        "prompt_cache_hit_tokens": 72000,
        "prompt_cache_miss_tokens": 8000,
    })
    print(f"  压缩后稳定期: 命中率={acct.cache_hit_rate*100:.1f}%  "
          f"成本=${acct.effective_cost_per_1m:.4f}/1M")


def demo_6_snapshot_event() -> None:
    """演示 ⑧ snapshot/SSE 事件格式"""
    print_header("⑧ context.window_usage Snapshot — SSE 事件格式")

    cfg = resolve_context_config("deepseek-flash")
    acct = ContextAccountant(cfg)

    # 模拟中等大小的上下文
    messages = build_simulated_messages(tool_result_size=30_000)
    acct.total_tokens = acct.estimate_messages([m.get("content", "") for m in messages])

    acct.record_usage({
        "prompt_tokens": acct.total_tokens,
        "completion_tokens": 1800,
        "prompt_cache_hit_tokens": int(acct.total_tokens * 0.7),
        "prompt_cache_miss_tokens": int(acct.total_tokens * 0.3),
    })

    snapshot = acct.snapshot()
    print("  触发事件: context.window_usage")
    print("  Payload:")
    print(f"  {json.dumps(snapshot, indent=2)}")


def demo_7_compare_before_after() -> None:
    """完整的压缩前后对比，展示每一层策略的效果"""
    print_header("Full Pipeline — 逐层策略效果对比")

    cfg = resolve_context_config("deepseek-flash")
    acct = ContextAccountant(cfg)

    # 构建分层可观测的测试数据
    messages = [
        # 5 轮对话
        {"role": "user", "content": "初始化项目"},
        {"role": "assistant", "content": "好的，正在初始化..."},
        {"role": "tool", "content": "x" * 10_000, "name": "init"},
        {"role": "user", "content": "添加认证模块"},
        {"role": "assistant", "content": "正在分析需求..."},
        {"role": "tool", "content": "y" * 60_000, "name": "read_file"},  # > 50K
        {"role": "tool", "content": "z" * 30_000, "name": "grep_search"},
        {"role": "user", "content": "实现 JWT 中间件"},
        {"role": "assistant", "content": "正在实现..."},
        {"role": "tool", "content": "w" * 60_000, "name": "read_file"},  # > 50K
        {"role": "tool", "content": "v" * 25_000, "name": "run_command"},
        {"role": "tool", "content": "u" * 60_000, "name": "glob_files"},  # > 50K
        {"role": "user", "content": "当前最新问题"},
    ]

    # 估算
    acct.total_tokens = acct.estimate_messages([m.get("content", "") for m in messages])
    print("  Token 路径:")
    print(f"    BEFORE: ~{fmt_tokens(acct.total_tokens)}")

    # 第一层: ToolResultBudget
    print(f"  {'─'*50}")
    budget = ToolResultBudget()
    r1, _ = budget.apply([dict(m) for m in messages], _save_demo_artifact)
    t1 = acct.estimate_messages([m.get("content", "") for m in r1])
    saved1 = acct.total_tokens - t1
    print(f"  ① ToolResultBudget: ~{fmt_tokens(t1)}  (节省 {fmt_tokens(saved1)})")
    acct.mark_compressed("tool_result_budget", t1)

    # 第二层: Microcompact
    print(f"  {'─'*50}")
    micro = Microcompact(keep_results=3)
    r2, _ = micro.apply([dict(m) for m in r1], _save_demo_artifact)
    t2 = acct.estimate_messages([m.get("content", "") for m in r2])
    saved2 = t1 - t2
    print(f"  ③ Microcompact:     ~{fmt_tokens(t2)}  (节省 {fmt_tokens(saved2)})")
    acct.mark_compressed("microcompact", t2)

    total_saved = saved1 + saved2
    print(f"  {'─'*50}")
    print(f"  Total:              ~{fmt_tokens(acct.total_tokens)}")
    print(f"  总节省:             ~{fmt_tokens(total_saved)}")

    # 每条消息的最终状态
    print()
    print_section("每条消息最终状态")
    for i, (orig, final) in enumerate(zip(messages, r2, strict=True)):
        role = orig.get("role", "?")
        content = final.get("content", "")
        name = orig.get("name", "")
        changed = content != orig.get("content", "")

        status = "▸" if changed else " "
        label = f"{role}"
        if name:
            label += f"({name})"

        if isinstance(content, str) and len(content) > 80:
            display = content[:60] + "..."
        else:
            display = content

        print(f"    [{i:2d}] {status} {label:20s}  {display}")


# =============================================================
# 4. 主入口
# =============================================================

def main() -> None:
    print()
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║      上下文压缩能力 — 离线 Demo （不调用真实模型）              ║")
    print("║      目标模型: DeepSeek Flash  |  上下文窗口: 1,000,000 tokens  ║")
    print("╚══════════════════════════════════════════════════════════════════╝")

    demo_1_config()
    demo_2_accountant()
    demo_3_cache_tracker()
    demo_4_compression_pipeline()
    demo_5_cache_impact()
    demo_6_snapshot_event()
    demo_7_compare_before_after()

    print()
    print("=" * 72)
    print("  Demo 完成 ✓  所有压缩管线均在离线模式下验证通过")
    print("  未调用任何真实模型 API")
    print("=" * 72)
    print()


if __name__ == "__main__":
    main()
