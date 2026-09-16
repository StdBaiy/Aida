# 上下文压缩能力设计文档

> **目标模型**: DeepSeek Flash (deepseek-flash) | **上下文窗口**: 1,000,000 tokens  
> **参考实现**: Codex CLI, Claude Code  
> **日期**: 2026-09-15

---

## 目录

- [0. 上下文窗口记账系统](#0-上下文窗口记账系统)
- [1. 触发时机](#1-触发时机)
- [2. 压缩机制设计](#2-压缩机制设计)
- [3. 主流方案对比：Codex CLI vs Claude Code](#3-主流方案对比codex-cli-vs-claude-code)
- [4. Prompt Cache 命中统计](#4-prompt-cache-命中统计)
- [5. Trace/Turn 保真与可恢复性](#5-traceturn-保真与可恢复性)
- [6. 边界 Case 防护](#6-边界-case-防护)
- [7. 压缩效果评估](#7-压缩效果评估)

---

## 0. 上下文窗口记账系统

### 0.1 模型配置

DeepSeek Flash 上下文窗口为 **1,000,000 tokens**，定价结构如下：

| 项目 | 价格 ($/1M tokens) |
|------|-------------------|
| Cache 命中输入 (prompt_cache_hit) | $0.003 |
| Cache 未命中输入 (prompt_cache_miss) | $0.14 |
| 输出 | $0.28 |

> 参考：实测中 agent 场景可达到 **99.30% 的缓存命中率**，混合成本低至 $0.00892/1M tokens。

### 0.2 ContextWindowConfig

```python
# coding_agent/context/config.py (新增)

from dataclasses import dataclass
from enum import Enum


class CompressUrgency(Enum):
    NONE = "none"
    NORMAL = "normal"
    EMERGENCY = "emergency"


@dataclass
class ContextWindowConfig:
    """上下文窗口配置，按模型预设"""

    model: str
    hard_limit: int                     # 模型硬上限 (tokens)
    soft_limit: int                     # 触发压缩的软阈值
    emergency_threshold: int            # 紧急压缩阈值
    compression_target_ratio: float     # 压缩目标：占 soft_limit 的比例
    min_reserved_tokens: int            # 为模型输出预留的最小 token 数
    max_output_tokens: int              # 模型 max_tokens 设置
    max_messages: int                   # 最大消息条数上限
    min_turns_before_compress: int      # 至少几个 turn 后才允许压缩
    min_messages_before_compress: int   # 至少多少条消息后才允许压缩
    cache_aware: bool                   # 是否感知 prompt cache 做压缩决策


# ── DeepSeek Flash 预设 ──
DEEPSEEK_FLASH_1M = ContextWindowConfig(
    model="deepseek-flash",
    hard_limit=1_000_000,
    soft_limit=700_000,                 # 70% 触发标准压缩
    emergency_threshold=900_000,        # 90% 触发紧急压缩
    compression_target_ratio=0.4,       # 压缩到 soft_limit 的 40% = 280K
    min_reserved_tokens=32_000,         # 为输出预留 32K
    max_output_tokens=32_000,
    max_messages=300,
    min_turns_before_compress=5,
    min_messages_before_compress=30,
    cache_aware=True,
)

# ── 预设注册表 ──
CONTEXT_WINDOW_PRESETS: dict[str, ContextWindowConfig] = {
    "deepseek-flash": DEEPSEEK_FLASH_1M,
    "deepseek-chat": DEEPSEEK_FLASH_1M,
}
```

### 0.3 ContextAccountant

```python
# coding_agent/context/accounting.py (新增)

class ContextAccountant:
    """
    上下文窗口记账员。

    双重记账策略：
    - Fast path: 使用 tiktoken 或模型的编码器逐条消息估算
    - Slow path: 模型返回后从 usage.prompt_cache_hit_tokens +
                 usage.prompt_cache_miss_tokens 获取精确值进行修正
    """

    def __init__(self, config: ContextWindowConfig, encoding_name: str = "cl100k_base"):
        self.config = config
        self.encoding = tiktoken.get_encoding(encoding_name)
        self.total_tokens = 0
        self.cache_hit_tokens = 0
        self.cache_miss_tokens = 0
        self.message_tokens: dict[str, int] = {}
        self.system_prompt_tokens = 0
        self.available_tokens: int = config.hard_limit

    def estimate_message(self, msg: BaseMessage) -> int:
        """Fast path: 快速估算单条消息 token 数"""
        overhead = 4  # role 标签 + 基础开销
        if isinstance(msg, AIMessage) and msg.tool_calls:
            overhead += 3 * len(msg.tool_calls)
        content_tokens = len(self.encoding.encode(msg.content or ""))
        return overhead + content_tokens

    def record_model_response(self, usage: dict) -> None:
        """
        Slow path: 从模型响应中获取精确计数。
        DeepSeek 返回格式：
        {
            "prompt_tokens": ...,
            "completion_tokens": ...,
            "prompt_cache_hit_tokens": ...,
            "prompt_cache_miss_tokens": ...,
        }
        """
        self.total_tokens = (
            usage.get("prompt_cache_hit_tokens", 0)
            + usage.get("prompt_cache_miss_tokens", 0)
            + usage.get("completion_tokens", 0)
        )
        self.cache_hit_tokens = usage.get("prompt_cache_hit_tokens", 0)
        self.cache_miss_tokens = usage.get("prompt_cache_miss_tokens", 0)

    @property
    def cache_hit_rate(self) -> float:
        """缓存命中率 = hit / (hit + miss)"""
        total = self.cache_hit_tokens + self.cache_miss_tokens
        return self.cache_hit_tokens / max(total, 1)

    @property
    def effective_cost_per_1m(self) -> float:
        """混合成本：综合缓存命中与未命中的加权单价"""
        total = self.cache_hit_tokens + self.cache_miss_tokens
        if total == 0:
            return 0.14  # 默认未命中价
        hit_ratio = self.cache_hit_tokens / total
        return 0.003 * hit_ratio + 0.14 * (1 - hit_ratio)

    def usage_ratio(self) -> float:
        return self.total_tokens / self.config.hard_limit

    def should_compress(self) -> CompressUrgency:
        if self.usage_ratio() * self.config.hard_limit >= self.config.emergency_threshold:
            return CompressUrgency.EMERGENCY
        if self.usage_ratio() * self.config.hard_limit >= self.config.soft_limit:
            return CompressUrgency.NORMAL
        return CompressUrgency.NONE
```

### 0.4 与前端的集成

通过 `EventJournal` SSE 推送实时上下文水位：

```json
{
  "event_type": "context.window_usage",
  "payload": {
    "total_tokens": 750000,
    "hard_limit": 1000000,
    "usage_ratio": 0.75,
    "urgency": "normal",
    "message_count": 85,
    "compression_count": 2,
    "cache_hit_tokens": 420000,
    "cache_miss_tokens": 180000,
    "cache_hit_rate": 0.70,
    "effective_cost_per_1m": 0.0441
  }
}
```

---

## 1. 触发时机

### 1.1 三级触发策略（适配 1M 窗口）

| 级别 | 阈值 | 触发时机 | 行为 |
|------|------|----------|------|
| **NONE** | < 700K (70%) | — | 不触发 |
| **NORMAL** | 700K ~ 900K (70%~90%) | 每轮模型调用前 | 执行标准压缩：ToolResult 落盘 + Snip Compact |
| **EMERGENCY** | ≥ 900K (90%) | 每轮模型调用前 | 执行完整压缩流水线 |

### 1.2 触发点

在 `runtime.py` 的 `_stream_graph()` 之前插入压缩检查：

```python
# runtime.py 修改示意

def _run_turn(self, ...) -> None:
    accountant = self._get_accountant()

    # 每次图节点执行前检查上下文
    urgency = accountant.should_compress()
    if urgency != CompressUrgency.NONE:
        state = self._get_current_graph_state()
        messages = state.get("messages", [])

        # 启发式跳过：消息太少时不压缩
        if (
            len(messages) >= self.context_config.min_messages_before_compress
            and self.turn_number >= self.context_config.min_turns_before_compress
        ):
            compressor = ContextCompressor(self.context_config, accountant, self.trace_recorder)
            state = compressor.compress(state, urgency)
            self._apply_compressed_state(state)

    # 继续正常流程
    events = self.graph.stream(value, config, ...)
```

### 1.3 DeepSeek 1M 窗口的特殊考量

1M 窗口意味着压缩频率远低于 200K 窗口的系统。实测表明：

- 一个 agent session 在复杂任务下每小时约消耗 150K~250K tokens
- 理论上可持续运行 **4~6 小时** 才触及 70% 软阈值
- 因此压缩应更注重 **效果** 而非 **频率**，每次压缩应有更高的压缩比
- 压缩对 **Prompt Cache 的破坏** 成本也更高（1M prefix 重建耗时更长）

---

## 2. 压缩机制设计

### 2.1 分层压缩流水线

参考 Claude Code 的 5 层递进设计，并适配 1M 窗口的特点：

```
┌────────────────────────────────────────── query() 每轮循环 ──────────────────────────────────────────┐
│                                                                                                      │
│  ① Tool Result Budget ──→ ② Snip Compact ──→ ③ Microcompact                                        │
│       ↓ 不够                  ↓ 不够                  ↓ 不够                                         │
│  ④ Session Memory Compact ──→ ⑤ Full Compact (LLM 摘要)                                            │
│                                                                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

#### 第 ① 层：Tool Result Budget — 大结果落盘

**成本**: 零（纯文件操作） | **破坏缓存**: 否

- 单条 `tool_result` 超过 **50,000 字符** 时，将完整内容写入 `trace_db.artifacts`
- 上下文内替换为预览（~2KB）+ 文件路径引用
- 单轮聚合 `tool_result` 总量限制 **200,000 字符**
- 始终保留最近 **3 条** 工具结果的完整内容

```python
class ToolResultBudget:
    """大工具结果优先落盘，不破坏对话顺序和缓存前缀。"""

    PERSIST_THRESHOLD_CHARS = 50_000      # 单条阈值
    AGGREGATE_BUDGET_CHARS = 200_000      # 单轮聚合预算
    KEEP_MOST_RECENT = 3                  # 保留最近 N 条完整

    def apply(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        # 识别 MessageType.TOOL_RESULT 消息，检查大小
        # 超限 → 写入 artifact，替换内容为引用
        ...
```

#### 第 ② 层：Snip Compact — 轮次级别精简

**成本**: 极低（字符串操作） | **破坏缓存**: 是（修改了历史）

- 删除低价值的历史轮次中的工具调用细节
- 每轮精简为 `[Round N: user_intent → action → outcome]` 格式
- 使用 `cache_editing` 模式（Anthropic 的 context editing 概念），告知模型"以下内容已被压缩"

```python
class SnipCompact:
    """轮次级别精简：用结构化摘要替换旧工具调用对。"""

    KEEP_RECENT_ROUNDS = 5   # 保留最近 5 轮完整
    MAX_TOKENS_PER_ROUND = 500  # 每轮摘要上限

    def apply(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        turns = self._extract_turns(messages)
        old_turns, recent_turns = turns[:-self.KEEP_RECENT_ROUNDS], turns[-self.KEEP_RECENT_ROUNDS:]
        compressed = [self._summarize_turn(t) for t in old_turns]
        return compressed + recent_turns
```

#### 第 ③ 层：Microcompact — 工具结果清除

**成本**: 低（无需 LLM 调用） | **破坏缓存**: 是

- 清除阈值之前的所有 `tool_result` 内容，替换为 `[Tool result saved to artifact: <id>]`
- 保留 `tool_call` 的签名（工具名、关键参数）
- 不涉及对话内容，用户请求和助手回复保持不变

```python
class Microcompact:
    """
    清除旧工具结果，保留调用签名。
    类似 Claude Code 的 microcompact：
    - 只操作 tool_result 内容块
    - 最近的 N 条结果保留完整
    - 旧结果替换为 artifact 引用
    """

    KEEP_TOOL_RESULTS = 5  # 保留最近 5 条完整

    def apply(self, messages: list[BaseMessage]) -> list[BaseMessage]:
        tool_result_count = 0
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], ToolMessage):
                if tool_result_count >= self.KEEP_TOOL_RESULTS:
                    messages[i].content = "[Tool result saved to artifact]"
                else:
                    tool_result_count += 1
        return messages
```

#### 第 ④ 层：Session Memory Compact — 基于已提取笔记的压缩

**成本**: 中等（拼接预提取的笔记，无需 LLM 调用） | **破坏缓存**: 是

- Agent 运行过程中持续维护一个 `session_memory`（结构化笔记）
- 包含：已修改的文件列表、关键决策记录、当前任务状态
- 压缩时直接用 session_memory 重建紧凑历史，无需额外 LLM 摘要

```python
class SessionMemoryCompact:
    """
    基于 session_memory 的无需 LLM 的压缩。

    类似 Codex CLI 的 session memory compact：
    - Agent 运行中持续维护 key-value 形式的结构化记忆
    - 包含：files_touched, decisions_made, task_progress, errors_encountered
    - 压缩时：保留最新 K 轮完整 + session_memory + 系统提示
    - 不需要 LLM 调用生成摘要
    """

    KEEP_FULL_ROUNDS = 3

    def apply(self, messages: list[BaseMessage], memory: SessionMemory) -> list[BaseMessage]:
        # 构建压缩后的历史：
        # [system prompt] + [session_memory as summary] + [recent K rounds in full]
        return compressed_messages
```

#### 第 ⑤ 层：Full Compact — LLM 摘要压缩

**成本**: 高（一次 LLM 调用 + 缓存全量失效） | **破坏缓存**: 严重影响

- 仅当以上 4 层无法满足压缩目标时触发
- 将旧对话发送给 LLM 生成详细的结构化摘要
- 参考 Claude Code 的 `/compact` prompt，包含：
  - 用户原始请求的保留
  - 关键文件变更记录
  - 架构决策
  - 错误与修复过程

**提示词设计（参考 Claude Code 的 compact prompt）**：

```
You are a helpful AI assistant tasked with summarizing conversations
for a coding agent session.

Your task is to create a detailed summary of the conversation so far,
paying close attention to the user's explicit requests and your
previous actions.

This summary should be thorough in capturing technical details, code
patterns, and architectural decisions that would be essential for
continuing development work without losing context.

Before providing your final summary, wrap your analysis in <analysis>
tags to organize your thoughts. In your analysis process:
1. Chronologically analyze each message, identifying:
   - The user's explicit requests and intents
   - Key decisions, technical concepts and code patterns
   - Specific details like: file names, code snippets, function signatures
   - Errors encountered and how they were fixed
2. Double-check for technical accuracy and completeness.

Your summary should include:
- Files read or modified
- Key decisions made
- Current task status
- Pending issues or blockers
- User preferences discovered
- Tool outputs and results (as needed)
```

**重要**: Full Compact 是最高成本的操作——它不仅要支付一次 LLM 调用的费用，更重要的是 **会使整个 Prompt Cache 前缀失效**。对于 1M 上下文窗口，重建缓存可能需要数十秒。因此 Full Compact 应作为最后的兜底手段。

### 2.2 压缩管线调度器

```python
class ContextCompressor:
    """
    组合压缩管线，从低成本到高成本依次尝试。
    每次压缩后检查是否达到目标，已满足则提前终止。
    """

    def __init__(
        self,
        config: ContextWindowConfig,
        accountant: ContextAccountant,
        memory: SessionMemory | None = None,
    ):
        self.config = config
        self.accountant = accountant
        self.memory = memory
        self.strategy_used: str | None = None

    def compress(self, state: dict, urgency: CompressUrgency) -> dict:
        messages = state.get("messages", [])
        current_tokens = self.accountant.estimate_messages(messages)
        target_tokens = int(self.config.soft_limit * self.config.compression_target_ratio)

        # 构建按成本升序的管线
        pipeline = self._build_pipeline(urgency)

        for strategy in pipeline:
            if current_tokens <= target_tokens:
                break
            before = current_tokens
            try:
                messages = strategy.apply(messages, memory=self.memory)
                current_tokens = self.accountant.estimate_messages(messages)
                self._record_metric(strategy.name, before, current_tokens)
            except Exception as e:
                logger.warning(f"Strategy {strategy.name} failed: {e}")
                continue

        state["messages"] = messages
        state["_compressed"] = True
        state["_compression_strategy"] = self.strategy_used
        state["_compression_saved_tokens"] = self.accountant.total_tokens - current_tokens
        return state

    def _build_pipeline(self, urgency: CompressUrgency) -> list:
        if urgency == CompressUrgency.EMERGENCY:
            return [
                ToolResultBudget(),
                SnipCompact(keep_recent_rounds=3),
                Microcompact(keep_results=3),
                SessionMemoryCompact(keep_full_rounds=2),
                FullCompact(keep_recent_rounds=2),
            ]
        else:  # NORMAL
            return [
                ToolResultBudget(),
                SnipCompact(keep_recent_rounds=5),
                Microcompact(keep_results=5),
                SessionMemoryCompact(keep_full_rounds=3),
            ]
```

---

## 3. 主流方案对比：Codex CLI vs Claude Code

### 3.1 Codex CLI 压缩方案

| 特性 | Codex CLI |
|------|-----------|
| **压缩方式** | 服务端压缩 (Responses API `/responses/compact`) |
| **摘要存储** | `encrypted_content` AES 加密密文（Memento 策略） |
| **用户消息保留** | 保留原始用户消息（最近 64K token 预算） |
| **服务端指令** | 从磁盘重新注入 developer instructions + AGENTS.md |
| **文件恢复** | 重新读取最近修改的 5 个文件 |
| **Session Memory** | 有，支持无需 LLM 调用的 session memory compact |
| **缓存影响** | 压缩后全量缓存失效 |
| **触发阈值** | `context_window - max_output_tokens - 13,000` |
| **客户端替代** | 当使用第三方 provider（非 OpenAI）时走本地 compact |

**核心代码路径**：
```
query() → run_auto_compact() → session memory 足够?
  ├─ 是 → Session Memory Compact (无 LLM 调用)
  └─ 否 → POST /v1/responses/compact → 返回 encrypted_content
         → 构建新历史: 用户消息(verbatim) + 重新注入指令 + compaction item(last)
```

### 3.2 Claude Code 压缩方案

| 特性 | Claude Code |
|------|-------------|
| **压缩方式** | 客户端 5 层递进压缩 |
| **第 1 层** | Tool Result Budget — 大结果(>50K chars)落盘，保留最近 3 条 |
| **第 2 层** | Snip Compact — 轮次级别摘要替换（feature gate 控制） |
| **第 3 层** | Microcompact — 旧工具结果清除（`cache_editing` 模式） |
| **第 4 层** | Context Collapse — 提交日志式折叠 |
| **第 5 层** | AutoCompact — LLM 摘要（类似 `/compact`） |
| **自定义指令** | 支持在 CLAUDE.md 中配置 compact 指令 |
| **缓存感知** | 显式感知 prompt cache，尽量不破坏缓存前缀 |
| **触发阈值** | 默认 ~78%（200K 窗口下 ≈155K），预留 32K 输出 + 13K buffer |

**各层成本对比**：

```
Cost:  层① < 层② < 层③ < 层④ < 层⑤
Cache: 层① ✓ 不破坏  |  层②③④⑤ ✗ 破坏缓存
LLM:   层①②③④ 无 LLM 调用  |  层⑤ 需要 LLM 调用
```

### 3.3 对比总结与本方案定位

| 维度 | Codex CLI | Claude Code | **本方案（结合两者优点）** |
|------|-----------|-------------|--------------------------|
| 摘要位置 | 服务端 (API) | 客户端 (agent) | 客户端为主，支持服务端可选 |
| 摘要格式 | 加密密文 | 明文摘要 | 明文摘要（可审计）+ 结构化 |
| 用户消息 | 保留原文 | 摘要中包含 | 保留原文 |
| 文件恢复 | 自动重读 5 个 | 自动重读 5 个 | 基于 checkpoint 恢复 |
| Session Memory | 内置 | 无 | 内置（利用现有 tracing 系统） |
| 缓存命中统计 | 无暴露 | 无暴露 | **显式追踪 cache_hit/miss** |
| 多级流水线 | 2 级 | 5 级 | 5 级 + DeepSeek 1M 调优 |
| 可审计性 | 密文不可审计 | 明文可审计 | 明文 + artifact 持久化 |
| Trace 恢复 | 加密 blob 保存状态 | no | checkpoint + artifact 双重保障 |

---

## 4. Prompt Cache 命中统计

### 4.1 DeepSeek Cache 机制

DeepSeek API 的上下文硬盘缓存默认对所有用户开启。每次请求返回中包含：

```json
{
  "usage": {
    "prompt_tokens": 105000,
    "completion_tokens": 1500,
    "prompt_cache_hit_tokens": 100000,
    "prompt_cache_miss_tokens": 5000,
    "total_tokens": 106500
  }
}
```

- `prompt_cache_hit_tokens`: 命中缓存的 tokens 数
- `prompt_cache_miss_tokens`: 未命中缓存的 tokens 数

**缓存命中规则**：
1. 系统自动构建硬盘缓存，需完整匹配**缓存前缀单元**才能命中
2. 每次请求的**用户输入结束位置**和**模型输出结束位置**会生成缓存前缀单元
3. 公共前缀会被系统自动检测并落盘
4. 长输入按固定 token 间隔截取缓存单元
5. 缓存构建耗时为秒级，TTL 为数小时到数天

**缓存命中的关键是前缀结构稳定**：system prompt + 工具定义 + 历史消息的前缀部分保持稳定不动，只有尾部（最新用户消息）变化。

### 4.2 Cache 统计系统

```python
# coding_agent/context/cache_stats.py (新增)

from dataclasses import dataclass, field


@dataclass
class CacheSnapshot:
    """单次请求的缓存快照"""
    timestamp: str
    request_type: str          # "model_call" | "compaction"
    prompt_tokens: int
    prompt_cache_hit_tokens: int
    prompt_cache_miss_tokens: int
    completion_tokens: int
    cache_hit_rate: float
    estimated_cost: float       # USD

    @property
    def cache_savings(self) -> float:
        """因缓存命中节省的费用"""
        hit_cost = self.prompt_cache_hit_tokens / 1_000_000 * 0.003
        miss_cost = self.prompt_cache_miss_tokens / 1_000_000 * 0.14
        # 如果全部不命中
        full_cost = (self.prompt_cache_hit_tokens + self.prompt_cache_miss_tokens) / 1_000_000 * 0.14
        return full_cost - (hit_cost + miss_cost)


@dataclass
class CacheStatsTracker:
    """聚合缓存统计，按 session 跟踪"""

    session_id: str
    model: str = "deepseek-flash"
    requests: list[CacheSnapshot] = field(default_factory=list)

    # 累计值
    total_prompt_tokens: int = 0
    total_cache_hit_tokens: int = 0
    total_cache_miss_tokens: int = 0
    total_completion_tokens: int = 0
    total_cost: float = 0.0
    total_savings: float = 0.0

    def record(self, snapshot: CacheSnapshot) -> None:
        self.requests.append(snapshot)
        self.total_prompt_tokens += snapshot.prompt_tokens
        self.total_cache_hit_tokens += snapshot.prompt_cache_hit_tokens
        self.total_cache_miss_tokens += snapshot.prompt_cache_miss_tokens
        self.total_completion_tokens += snapshot.completion_tokens
        self.total_cost += snapshot.estimated_cost
        self.total_savings += snapshot.cache_savings

    @property
    def overall_cache_hit_rate(self) -> float:
        total = self.total_cache_hit_tokens + self.total_cache_miss_tokens
        return self.total_cache_hit_tokens / max(total, 1)

    @property
    def blended_cost_per_1m(self) -> float:
        total_input = self.total_cache_hit_tokens + self.total_cache_miss_tokens
        if total_input == 0:
            return 0.14
        return (
            self.total_cache_hit_tokens * 0.003 + self.total_cache_miss_tokens * 0.14
        ) / total_input

    def summary(self) -> dict:
        """生成摘要报告"""
        return {
            "model": self.model,
            "total_requests": len(self.requests),
            "total_input_tokens": self.total_prompt_tokens,
            "total_cache_hit_tokens": self.total_cache_hit_tokens,
            "total_cache_miss_tokens": self.total_cache_miss_tokens,
            "total_completion_tokens": self.total_completion_tokens,
            "overall_cache_hit_rate": self.overall_cache_hit_rate,
            "total_cost_usd": round(self.total_cost, 6),
            "total_savings_usd": round(self.total_savings, 6),
            "blended_cost_per_1m": round(self.blended_cost_per_1m, 6),
            "cost_if_no_cache_usd": round(self.total_cost + self.total_savings, 6),
        }
```

### 4.3 Token 成本明细报告

通过 tracing 系统，每轮 turn 完成后自动生成上下文使用报告：

```
┌─────────────────────────────────────────────────┐
│  Session Token Report                            │
├─────────────────────────────────────────────────┤
│  上下文窗口:        1,000,000 tokens             │
│  当前占用:          750,000  (75.0%)             │
│  可用:              250,000  (25.0%)             │
├─────────────────────────────────────────────────┤
│  缓存命中:          520,000  (69.3%)             │
│  缓存未命中:        230,000  (30.7%)             │
│  缓存命中率:        69.3%                        │
├─────────────────────────────────────────────────┤
│  实际成本:          $0.0078 / 1M blended         │
│  节省金额:          $0.0612 / 1M (vs no cache)   │
│  累计节省:          $3.42 (session 总计)          │
├─────────────────────────────────────────────────┤
│  压缩历史:                                        │
│  Turn 12: SnipCompact  ▼ 850K→620K (▽27%)       │
│  Turn 18: Microcompact ▼ 780K→550K (▽29%)        │
└─────────────────────────────────────────────────┘
```

### 4.4 缓存感知的压缩决策

对于 DeepSeek Flash 来说，**缓存命中率直接影响成本**。压缩策略需要考虑这一点：

```python
class CacheAwareCompressor(ContextCompressor):
    """
    缓存感知的压缩决策。

    核心原则：
    1. 缓存命中率 > 70% 时尽量不触发 Full Compact（避免缓存失效）
    2. 软阈值 700K 但缓存命中率高时可延迟到 800K+
    3. Snip/Microcompact 虽然破坏缓存，但 token 节省通常在 20-40%
       收益通常大于缓存重建的成本
    """

    def should_compact(self, accountant: ContextAccountant) -> CompressUrgency:
        base_urgency = accountant.should_compress()
        cache_hit_rate = accountant.cache_hit_rate

        # 缓存命中率高 → 提高压缩容忍度
        if cache_hit_rate > 0.7:
            if base_urgency == CompressUrgency.EMERGENCY:
                return CompressUrgency.EMERGENCY  # 不降级
            elif base_urgency == CompressUrgency.NORMAL:
                # 延迟触发：实际使用量 > 80% 才触发
                if accountant.total_tokens >= accountant.config.hard_limit * 0.8:
                    return CompressUrgency.NORMAL
                return CompressUrgency.NONE
        return base_urgency
```

---

## 5. Trace/Turn 保真与可恢复性

### 5.1 压缩不影响已持久化的 Turn

```
已提交的 Turn N:    checkpoints.db ── 完整消息历史 (永不压缩)
                   agent.db.turns ── 指向完整 checkpoint (不变)

执行中的 Turn N+1:  fork_checkpoint() ──→ 从 Turn N 的完整 checkpoint fork
                   → 在 fork 出的内存副本上执行压缩
                   → 压缩后的消息传递给模型
                   → Turn N+1 提交时写入新的 checkpoint (完整历史)
```

### 5.2 Trace 可恢复性

| 恢复方式 | 机制 | 场景 |
|---------|------|------|
| **Turn 回滚** | `TurnCoordinator.restore()` 从 `checkpoints.db` 加载完整状态 | 用户手动回滚 |
| **Trace Artifact** | 压缩前完整消息存为 artifact，通过 `pre_compress_artifact_id` 关联 | 调试/审计 |
| **Span Links** | 压缩事件创建 span_link 连接压缩前/后的消息 | 自动追踪变化 |
| **Cache 报告** | 每轮 `CacheSnapshot` 写入 trace_db | 成本分析 |

### 5.3 压缩日志与可审计性

新增 `compressions` 表（在 `trace_db` 中）：

```sql
CREATE TABLE compressions (
    compression_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    timeline_id TEXT NOT NULL,
    turn_number INTEGER NOT NULL,
    urgency TEXT NOT NULL,
    strategy TEXT NOT NULL,         -- 'tool_result_budget' | 'snip' | 'micro' | 'session_memory' | 'full'
    original_message_count INTEGER NOT NULL,
    compressed_message_count INTEGER NOT NULL,
    original_token_count INTEGER NOT NULL,
    compressed_token_count INTEGER NOT NULL,
    compression_ratio REAL NOT NULL,
    cache_hit_before INTEGER,       -- 压缩前的缓存命中
    cache_miss_before INTEGER,      -- 压缩前的缓存未命中
    pre_compress_artifact_id TEXT,  -- 压缩前完整消息的 artifact 引用
    created_at TEXT NOT NULL
);
```

---

## 6. 边界 Case 防护

### 6.1 边界 Case 矩阵

| # | 场景 | 风险 | 防护措施 |
|---|------|------|----------|
| 1 | **消息列表为空/过短** | 压缩器处理空列表 | 前置守卫：`min_messages_before_compress=30` 跳过 |
| 2 | **一轮压缩后仍未达到目标** | 逐级压缩后仍超限 | 兜底：直接截断到 `min_reserved_tokens` |
| 3 | **摘要质量污染推理** | 摘要丢失关键信息 | 注入 `[COMPRESSED SUMMARY]` 标记；保留 artifact 引用 |
| 4 | **ToolCall/ToolResult 不匹配** | Tool-call repair 被压缩打乱 | 压缩器必须维护 `tool_call_id` 配对完整性 |
| 5 | **多次压缩叠加** | 摘要再压缩 → 二次丢失 | `_compressed` 标记防止重复压缩 |
| 6 | **子 Agent 上下文独立增长** | 主/子上下文各自超限 | 子 Agent 独立使用 `ContextAccountant` |
| 7 | **用户消息含极长 diff** | 单条消息超过阈值 | 大内容采样 → artifact，替换为 `[FILE CONTENT SAMPLED]` |
| 8 | **并发压缩竞争** | Subagent wake + 同步压缩 | 获取 `mutation_lease` 序列化 |
| 9 | **首次 turn 不需要压缩** | 前几轮上下文短 | `min_turns_before_compress=5` 跳过 |
| 10 | **压缩后 Prompt Cache 失效成本 > 收益** | 缓存重建代价高 | `CacheAwareCompressor` 做成本收益分析后再决策 |
| 11 | **缓存命中率暴跌** | 压缩导致公共前缀改变 | 压缩后监控 cache_hit_rate，< 50% 时报警 |
| 12 | **压缩抖动 (Thrashing)** | 单次大文件/大结果反复触发压缩 | 检测到 thrashing 模式时，自动标记大结果为 artifact |

### 6.2 Tool Call 配对完整性保证

```python
class CompressedMessageValidator:
    """压缩后消息列表必须满足 LangGraph 约束：
    - 每条 AIMessage 的 tool_calls 都有对应 ToolMessage
    - 每个 ToolMessage 都有对应 AIMessage
    - 不得遗留 orphaned tool_call_ids
    """

    def validate(self, messages: list[BaseMessage]) -> bool:
        call_ids = set()
        for msg in messages:
            if isinstance(msg, AIMessage):
                for tc in getattr(msg, "tool_calls", []) or []:
                    call_ids.add(tc["id"])
            if isinstance(msg, ToolMessage):
                call_ids.discard(getattr(msg, "tool_call_id", ""))
        return len(call_ids) == 0  # 所有 call 都配对了
```

### 6.3 压缩后验证与回滚

```python
def compress_with_verification(self, state, urgency):
    """压缩 → 验证 → 回滚（失败时）"""
    original_messages = state["messages"].copy()
    try:
        new_state = self.compress(state, urgency)
        assert self.validator.validate(new_state["messages"]), "Tool call pairing broken"
        assert self.accountant.estimate_messages(new_state["messages"]) < self.config.hard_limit
        return new_state
    except Exception as e:
        logger.warning(f"Compression failed, falling back: {e}")
        state["messages"] = original_messages
        return state
```

---

## 7. 压缩效果评估

### 7.1 离线评估指标

| 指标 | 计算方式 | 目标值 | 说明 |
|------|----------|--------|------|
| **压缩比 (CR)** | `compressed_tokens / original_tokens` | < 0.5 | 压缩到原来的 50% 以下 |
| **压缩耗时** | 从开始到验证完成 | < 500ms (层①~④), < 5s (层⑤) | 含 LLM 调用 |
| **工具调用保真度** | 相同任务压缩前/后产生相同工具调用序列的比例 | > 0.95 | 用 SWE-bench 验证 |
| **摘要保真度** | 人类评估/LLM 对比摘要 vs 原文事实一致性 | > 0.9 | FactScore |
| **Cache 恢复耗时** | 压缩后首次请求的 prefill 延迟 | < 基线 1.5x | cache 重建的开销 |

### 7.2 在线评估指标

通过 tracing 系统自动收集：

```
# 上下文管理指标
context.compression.count         # 压缩触发次数
context.compression.strategy      # 各策略分布
context.compression.ratio         # 压缩比
context.compression.time_ms       # 压缩耗时

# Prompt Cache 指标
context.cache.hit_tokens          # 累计命中 tokens
context.cache.miss_tokens         # 累计未命中 tokens
context.cache.hit_rate            # 缓存命中率
context.cache.cost_per_1m         # 混合成本
context.cache.savings_usd         # 累计节省金额

# 下游影响指标
turn.success_rate                 # 压缩后的 turn 成功率
turn.token_usage_after_compact    # 压缩后 token 回升速度
turn.correction_rate              # 用户是否需要额外纠正
```

### 7.3 A/B 测试框架

```python
class CompressionExperiment:
    """
    比较组 A: 启用压缩 | 比较组 B: 不启用压缩
    每个 block = 10 turns 后切换。
    """

    BLOCK_SIZE = 10

    def evaluate_session(self, session_id: str):
        # 收集完整 session 的指标
        # 对比：token 消耗总量、完成任务数、用户纠正率、延迟、成本
        report = {
            "compression_enabled": Summary(...),
            "compression_disabled": Summary(...),
            "delta": {
                "token_savings_pct": ...,
                "task_success_delta": ...,
                "cost_savings_pct": ...,
                "latency_delta_ms": ...,
            }
        }
        return report
```

### 7.4 成本节省模型

基于 DeepSeek Flash 的定价，一个典型 agent session 的成本结构：

```
无压缩场景 (假设每轮 150K token 输入):
  50 轮对话 × 150K input = 7,500K tokens
  缓存命中率 60%:
    成本 = (7,500K × 60% × $0.003 + 7,500K × 40% × $0.14) / 1M
         = $0.0135 + $0.42 = $0.4335

有压缩场景 (压缩比为 50%，每轮压缩后 75K):
  50 轮对话 × 75K input = 3,750K tokens
  每次压缩额外消耗: ~5 次压缩 × 150K input (每次摘要) = 750K
  总计输入: 4,500K tokens
  缓存命中率 70% (前缀更稳定):
    成本 = (4,500K × 70% × $0.003 + 4,500K × 30% × $0.14) / 1M
         = $0.00945 + $0.189 = $0.1985

  节省: ($0.4335 - $0.1985) / $0.4335 = 54.2%
```

### 7.5 可视化仪表盘

通过 SSE 推送前端展示：

```
[上下文占用水位]          [缓存命中率]
████████████░░░░░ 75%    ███████░░░ 69.3%

[成本趋势]
$0.50 ┤             ╭───
$0.40 ┤    ╭───╮   ╱
$0.30 ┤   ╱   ╰───╯          ← 启用压缩后下降
$0.20 ┤  ╱
$0.10 ┤─╯
      └─────────────────
        Turn  10  20  30  40
```

---

## 附录：与现有系统的集成点

| 现有模块 | 集成内容 | 修改量 |
|---------|---------|--------|
| `runtime.py` | 在 `_stream_graph()` 前插入压缩检查 | 小 |
| `callbacks.py` | `on_llm_end` 中提取 `prompt_cache_hit/miss` 字段 | 小 |
| `recorder.py` | `traces` 表新增 `cache_hit_tokens, cache_miss_tokens` 字段 | 小 |
| `repository.py` | 新增 `compressions` 表 | 中 |
| `config.py` | 新增 `context_window` 配置节 | 小 |
| `events.py` | 新增 `context.window_usage` 事件类型 | 小 |

**新文件清单**：
- `coding_agent/context/__init__.py`
- `coding_agent/context/config.py` — ContextWindowConfig, 预设
- `coding_agent/context/accounting.py` — ContextAccountant
- `coding_agent/context/compressor.py` — ContextCompressor, 各层策略
- `coding_agent/context/cache_stats.py` — CacheSnapshot, CacheStatsTracker
- `coding_agent/context/validator.py` — CompressedMessageValidator

---

## 参考资料

- [Codex CLI 上下文压缩源码分析 (osolmaz)](https://gist.github.com/osolmaz/a38acf6e522df67530e3ed47c80fdcd5)
- [Claude Code 上下文文档](https://code.claude.com/docs/en/context-window)
- [Anthropic Context Windows 文档](https://docs.anthropic.com/en/docs/build-with-claude/context-windows)
- [Anthropic 服务端 Compaction 文档](https://docs.anthropic.com/en/docs/build-with-claude/compaction)
- [DeepSeek 上下文缓存文档](https://api-docs.deepseek.com/guides/kv_cache)
- [Codex CLI vs Claude Code vs OpenCode 对比](https://codex.danielvaughan.com/2026/04/14/context-compaction-deep-dive-codex-cli-claude-code-opencode/)
- [Context Pruning Research (SWE-Pruner, Pichay, ContextBudget)](https://codex.danielvaughan.com/2026/06/17/context-pruning-research-swe-pruner-pichay-contextbudget-codex-cli-token-management-compaction/)
- [DeepSeek V4.1 Flash Agent Cost Architecture](https://www.mech.app/articles/2-1-billion-tokens-for-19-how-deepseek-v4-1-flash-changes-agent-cost-architecture)