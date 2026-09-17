# 上下文压缩 V2 实现稿

## 1. 目标

本文基于当前代码，设计一套可持久化、可恢复、主/子 Agent 一致的上下文计量与
LLM 摘要压缩机制，满足以下要求：

1. 每个 turn 结束后，为每个 Agent 维护当前上下文窗口占用量。
2. WebUI 使用圆环展示 `已使用 / 最大窗口`，悬浮显示精确 token 数。
3. 每个 turn 完整结束后执行一次自动压缩判定，turn 内不执行压缩。
4. CLI 和 WebUI 均可手动触发压缩，但只允许在 turn 结束后的空闲边界执行；手动
   触发不受自动阈值限制。
5. 自动压缩阈值默认 80%，可配置。
6. turn 内模型调用前若上下文已达到安全上限，禁止继续采样并安全结束当前 turn；
   压缩统一在随后形成的 turn 边界执行。
7. 压缩任务读取当前完整对话上下文，并在末尾追加专用交接摘要提示词。
8. 压缩后上下文按以下顺序重建：
   - 固定系统提示词；
   - 历史压缩摘要 `summary_000 ... summary_N-1`；
   - 本次摘要 `summary_N`；
   - 尽可能多的近期真实用户输入，保持原始顺序，总计最多 2000 token。
9. 压缩开始、压缩中、压缩完成均对用户可见；完成结果作为持久化系统消息出现在
   对话窗口中。
10. 压缩前后 LangGraph checkpoint、业务元数据、UI 状态和审计记录必须一致。
11. 所有跨越 ToolMessage 边界的工具输出必须先持久化，再返回给 LLM；输出随
    Session 永久保留，并允许 LLM 在压缩后按引用重新核对。

本文中的“上下文占用”指下一次模型调用可见的输入 token，而不是会话累计消耗，也
不等于某次调用的 `prompt_tokens + completion_tokens`。

### 1.1 实施状态（2026-09-17）

本设计已完成首轮生产实现：

- 主 Agent 与子 Agent 的 turn/phase 结束计量、持久化恢复和独立圆环；
- `timeline.head_checkpoint_id`、窗口占用修正及 turn 内硬预算安全收束；
- turn commit 后自动判定、空闲边界 Web/CLI 手动压缩；
- LLM handoff summary、摘要链、近期用户输入 2000 token 预算和 evidence manifest；
- started/progress/completed/failed SSE、持久化完成 notice 和 Settings 配置；
- 同步工具 fail-closed 输出信封、Session 所有权回读/搜索；
- 后台工具 NDJSON spool、terminal artifact 和 Host 重启 partial 恢复。

后续阶段仍需完成：

- `generating/applying` 跨 Host 崩溃的三阶段补偿恢复；
- 子 Agent 暂停态手动压缩入口；
- summary 独立 artifact、trace compression 审计字段和统一运维清理策略。

第 2 节保留为实施前的基线差距记录，不代表当前代码状态。

## 2. 现状结论

当前仓库已经实现了一套规则式上下文裁剪 MVP，但没有实现本需求定义的 LLM 摘要
压缩。

| 能力 | 当前状态 | 结论 |
| --- | --- | --- |
| 模型窗口配置 | `ContextWindowConfig` 提供 hard/soft/emergency limit | 部分实现 |
| 快速 token 估算 | `_estimate_context()` 使用 LangChain 近似计数并补偿 CJK | 已实现 |
| 模型 usage 校正 | `ContextUsageCallbackHandler.on_llm_end()` 读取 provider usage | 已实现 |
| turn 结束后判定 | Web Host 只发 usage，没有在 turn commit 后执行 LLM 摘要压缩 | 未实现 |
| 每 turn 结束计量 | Web Host 在 turn 完成后发一次 `context.window_usage` | 部分实现 |
| 每个 Agent 独立计量 | 主 Session Runtime 相对独立；子 Agent Runtime 重建后计数清零 | 未实现 |
| 计量持久化 | `ContextAccountant` 仅在内存中 | 未实现 |
| 自动阈值 | 预设为 70%，高缓存命中时动态提高；不在用户配置中 | 不符合预期 |
| 规则式压缩 | 大工具结果落盘、旧工具结果清空、紧急对话截断 | 已实现 |
| LLM 交接摘要 | 没有摘要模型、摘要 prompt、摘要链 | 未实现 |
| 压缩后重组 | 原地替换消息内容，消息数量和结构基本不变 | 不符合预期 |
| 手动压缩 | CLI/Web API/WebUI 均无入口 | 未实现 |
| 压缩过程事件 | 只有完成 turn 后的窗口占用事件 | 未实现 |
| 对话系统消息 | turn 只有 user/assistant 两类展示 | 未实现 |
| 全量工具输出持久化 | command stdout/stderr 和较大 trace payload 部分落盘 | 未实现 |
| 压缩后工具输出回读 | 没有面向 LLM 的持久化输出查询工具和所有权校验 | 未实现 |
| 压缩审计 | `compressions` 表、trace span、压缩前 artifact 已存在 | 部分实现 |
| checkpoint 一致性 | 消息替换写入 checkpoint；计量和摘要元数据不在 checkpoint | 部分实现 |

### 2.1 当前调用链

主 Agent：

```text
CodingAgentHost._run_turn
  -> TurnCoordinator.run_turn
    -> AgentRuntime.run_turn
      -> AgentRuntime._run_with_approvals
        -> _maybe_compress_context       # 每次 graph.stream 前
        -> _stream_graph                 # 内部可能发生多次 LLM 采样
        -> _update_accountant_after_stream
  -> emit context.window_usage          # 整个业务 turn 完成后一次
```

子 Agent 在 `SubagentDemoManager.run_phase()` 中临时创建 `AgentRuntime`。每个 phase
结束都会关闭 Runtime，因而内存中的 token、压缩次数和精确 usage 无法跨 phase 恢复。

### 2.2 当前实现的关键问题

1. `ContextAccountant.record_usage()` 把 `prompt_tokens + completion_tokens` 记为
   `total_tokens`。这适合表示单次调用消耗，不适合表示下一次调用的上下文占用。
2. 一个 `graph.stream()` 可以执行“模型 -> 工具 -> 模型”多次循环。callback 会收集
   每次模型 usage，但当前没有在完整 turn commit 后执行统一压缩判定。
3. 当前标准压缩只处理工具结果。纯对话历史只有达到 emergency 后才会被字符截断，
   不生成语义摘要。
4. `compression_count >= 5` 后停止压缩，而且该计数不持久化。
5. `min_turns_before_compress` 已配置但运行时未使用。
6. `context.window_usage` 只属于某个 operation 的 SSE 日志。operation 结束、页面刷新
   或 Host 重启后，WebUI 没有稳定的数据源。
7. WebUI 只在运行步骤中显示“上下文占用 N%”，没有圆环、精确 tooltip 或手动入口。
8. CLI 没有 `/compact` 和上下文占用展示。
9. 压缩表只能表达规则策略，缺少摘要序号、触发源、状态、基础/结果 checkpoint、
   摘要 artifact 和错误信息。

### 2.3 代码证据

| 结论 | 当前代码 |
| --- | --- |
| hard/soft/emergency 固定预设 | `coding_agent/context/config.py:8-36` |
| usage 把 prompt 和 completion 相加 | `coding_agent/context/accounting.py:90-102` |
| 自动判定和 cache-aware 阈值 | `coding_agent/context/accounting.py:154-198` |
| callback 只在 `on_llm_end` 更新内存 | `coding_agent/context/callbacks.py:25-35` |
| Runtime 初始化内存 accountant | `coding_agent/runtime.py:204-208` |
| 当前压缩发生在 graph stream 前 | `coding_agent/runtime.py:444-549`、`647-671` |
| 当前 graph stream 内部消费完整 agent loop | `coding_agent/runtime.py:831-864` |
| turn 完成后才发窗口事件 | `coding_agent/application/service.py:453-535` |
| 子 Agent 每个 phase 重建 Runtime | `coding_agent/subagents/manager.py:1181-1231` |
| SSE 已支持窗口事件持久日志 | `coding_agent/application/events.py:131-135` |
| WebUI 仅把窗口事件渲染为步骤文字 | `web/src/App.tsx:1929-1996`、`3037-3046` |
| ArtifactStore 已支持 SHA-256 和原子落盘 | `coding_agent/tracing/artifacts.py:11-43` |
| trace 小 payload 仍内联，且持久化失败为 fail-open | `coding_agent/tracing/recorder.py:590-618` |
| 后台工具流式输出只在内存保留有限窗口 | `coding_agent/execution/tool_runs.py:721-759` |
| command stdout/stderr 已部分持久化 | `coding_agent/execution/host.py:124-148`、`coding_agent/sandbox/execution.py:208-246` |
| compression 审计表已存在 | `coding_agent/tracing/recorder.py:84-100`、`480-559` |

## 3. 目标语义

### 3.1 Agent 与上下文所有权

使用稳定的 `context_owner_id` 标识一个可独立增长和压缩的上下文：

```text
main:{session_id}:{timeline_id}
subagent:{attempt_id}
```

- 主 Agent 的上下文随 timeline 变化。历史恢复会创建新 timeline，并复制源 turn 的
  上下文计量和摘要链。
- 子 Agent 以 attempt 为边界。跨 phase 复用同一 `attempt_id`、thread 和摘要链。
- `thread_id`、`checkpoint_id` 是当前物理状态位置，不作为稳定 owner 主键。

### 3.2 占用量定义

圆环中的 `used_tokens` 为下一次采样预计输入：

```text
system prompt
+ tool definitions
+ model-visible checkpoint messages
+ pending input（如有）
```

字段同时保留：

- `used_tokens`：当前窗口占用，UI 和阈值判定使用；
- `max_tokens`：模型上下文硬上限；
- `reserved_output_tokens`：为输出预留；
- `safe_input_tokens = max_tokens - reserved_output_tokens`；
- `last_prompt_tokens`：provider 最近一次返回的精确输入 token；
- `last_completion_tokens`：最近一次输出 token；
- `measurement = exact | estimated | corrected_estimate`。

采样结束后不能直接把 `prompt + completion` 当作新占用。应基于采样后的 checkpoint
重新计数；provider 的 `prompt_tokens` 只用于校正采样前估算误差。

### 3.3 触发类型

统一为 `CompressionTrigger`：

```text
auto_turn_end         turn 完整提交后的自动判定
manual_cli            空闲 turn 边界上的 CLI /compact
manual_web            空闲 turn 边界上的 WebUI 手动压缩
```

判定规则：

```text
manual => 总是压缩
used_tokens / max_tokens >= auto_compact_ratio => 自动压缩
否则 => 不压缩
```

默认 `auto_compact_ratio = 0.80`。不再根据 prompt cache 命中率静默改变用户配置的
阈值；cache 信息只进入观测数据。

### 3.4 Turn 边界

压缩只允许在完整 turn 边界提交。边界必须同时满足：

- 本 turn 不会再发生 LLM 采样；
- 所有后台工具已进入 terminal 状态；
- stdout/stderr/result/partial 输出已经完成归档；
- 不存在未配对的 assistant tool call；
- turn checkpoint 和业务 turn 记录已经持久化；
- 同一 `context_owner_id` 没有其他 graph 或压缩写操作。

固定时序：

```text
LLM/tool loop 结束
-> seal + drain + finalize tools
-> 归档全部工具输出
-> 提交 turn checkpoint、turn 记录和 workspace snapshot
-> 重新计算 turn 结束后的上下文占用
-> 判定是否压缩
-> 如触发则生成摘要并推进 timeline head checkpoint
-> operation 完成
```

每次采样后的 callback 仍记录 provider usage，用于 turn 内预算保护和最终计量校正，
但不执行压缩判定。

若 turn 内下一次模型调用已无法满足
`used_tokens + reserved_output_tokens < max_tokens`：

1. 不调用压缩模型；
2. 禁止新的普通 LLM 采样；
3. 停止启动新工具，并按既有策略等待或终止在途工具；
4. 归档所有已产生输出；
5. 以 `CONTEXT_BUDGET_EXHAUSTED` 安全结束并提交当前 turn；
6. 在该 turn 边界执行强制压缩；
7. 不自动重放被阻止的采样，下一 turn 从压缩后的 timeline head 继续。

这样压缩永远不需要处理半个 tool protocol，也不需要与后台工具并发修改 checkpoint。

## 4. 压缩算法

### 4.1 输入

`LlmContextCompactor` 使用独立、无工具、无 checkpoint 的模型调用。输入为当前
model-visible 对话上下文的副本，末尾追加压缩提示词：

```text
[base system prompt]
[all current checkpoint messages, including prior summary messages]
[COMPACTION HANDOFF PROMPT]
```

工具 schema 属于稳定运行配置，会在新上下文中重新绑定，不要求摘要模型复述。工具
结果正文、用户消息、assistant 回复、运行时系统消息和已有 summary 均属于待总结
上下文。已落盘的大结果保留 artifact 引用，不重新读取无限大正文。

压缩调用必须使用单独 callback 和 trace request type，禁止覆盖业务 Agent 最近一次
usage。

### 4.2 摘要提示词

建议新增 `coding_agent/prompting/assets/context_compaction.md`：

```text
You are producing a handoff summary for another coding Agent.

Summarize the supplied context so execution can continue without access to the
discarded messages. Preserve:
- the user's current goal, corrections, constraints, and explicit preferences;
- confirmed technical conclusions and decisions;
- exact file paths, symbols, interfaces, commands, errors, and relevant outputs;
- work already completed and verification results;
- active work, unresolved questions, blockers, and ordered next steps;
- tool/artifact references required to recover omitted large outputs.

Do not add facts, recommendations, or claims not supported by the context.
Distinguish confirmed facts from hypotheses. Do not include hidden reasoning.
Return only the handoff summary.
```

调用参数：

- `temperature=0`；
- 禁用 tools；
- 独立 `summary_model`，未配置时复用当前模型；
- `max_output_tokens` 单独配置，建议默认 4000；
- 超时、取消 token 和 trace 均继承当前 operation。

### 4.3 近期用户输入

从压缩前上下文中提取所有
`HumanMessage(additional_kwargs.origin == "user")`，在总计
`recent_user_inputs_max_tokens`（默认 2000）的预算内保留尽可能多的近期输入：

1. 调度器 wake、tool scheduler 和内部修复消息不算真实用户输入。
2. 从最新输入开始向前装入预算，优先保证最新指令、修正和约束不被较早输入挤出。
3. 选取结束后恢复为原始时间顺序，禁止按“新到旧”顺序写入新上下文。
4. 每条完整输入均保留独立 `HumanMessage`；只有预算边界上的最早一条允许截断。
5. 边界消息保留尾部内容并添加明确的前部截断标记，因为其后续修正通常更接近消息
   末尾。
6. 使用模型对应 tokenizer 计算整个用户输入集合，确保总计不超过 2000 token。
7. tokenizer 不可用时使用当前近似计数器，并标记 `measurement=estimated`。
8. 若没有真实用户输入则省略，不制造空 `HumanMessage`。

### 4.4 输出验证

摘要必须满足：

1. 非空且只有文本；
2. 不包含 tool call；
3. token 数不超过 `summary_max_tokens`；
4. 新上下文低于 `safe_input_tokens`；
5. 新消息序列可被 LangChain 序列化；
6. summary 序号严格单调，不能覆盖已有摘要。

普通自动压缩失败时：

- 已完成的业务 turn 保持 committed，不回滚用户回复或 workspace snapshot；
- timeline head 保持压缩前 checkpoint；
- 记录失败 notice 和审计；
- 若旧上下文仍低于 `safe_input_tokens`，允许下一 turn 正常开始；
- 若旧上下文已不安全，下一 turn 在入口处返回 `CONTEXT_COMPRESSION_REQUIRED`，只能
  先在空闲边界重试手动压缩。

provider 在 turn 内返回 context-length error 时不执行压缩重试。该 turn 按
`CONTEXT_BUDGET_EXHAUSTED` 收束并提交，随后在 turn 边界强制压缩，避免在同一 turn
中重入模型调用。

### 4.5 新上下文组装

第一次压缩：

```text
base system prompt（由 create_agent 的 system_prompt 自动注入）
SystemMessage("[CONTEXT SUMMARY summary_000]\n{summary_000}")
SystemMessage("[TOOL EVIDENCE]\nmanifest_ref={manifest_id}")
HumanMessage("{较早的近期真实用户输入，可选且可能在头部截断}")
...
HumanMessage("{最新的真实用户输入；全部用户输入总计最多 2000 token}")
```

第二次压缩：

```text
base system prompt
SystemMessage("[CONTEXT SUMMARY summary_000]\n{summary_000}")
SystemMessage("[CONTEXT SUMMARY summary_001]\n{summary_001}")
SystemMessage("[TOOL EVIDENCE]\nmanifest_ref={manifest_id}")
HumanMessage("{较早的近期真实用户输入，可选且可能在头部截断}")
...
HumanMessage("{最新的真实用户输入；全部用户输入总计最多 2000 token}")
```

注意：固定 system prompt 不重复写入 `messages`，否则 `create_agent` 会再次注入。这里
的“系统提示词 + 摘要”描述的是最终 model-visible 顺序。

提交 checkpoint 时使用一次 state update：

```python
{
    "messages": [
        RemoveMessage(id=REMOVE_ALL_MESSAGES),
        *summary_messages,
        tool_evidence_message,
        *recent_user_messages,
    ],
    "context_summaries": [...old_summaries, new_summary],
    "context_usage": post_compaction_usage,
    "last_compaction_id": compaction_id,
}
```

摘要链本身最终也可能增长到上限。必须设置安全阀
`summary_chain_max_tokens`。达到安全阀时可对摘要链生成一个 rollup summary，旧摘要
仍完整保存在数据库和 artifact 中，但 model-visible 链替换为 rollup。否则“永远
拼接所有历史摘要”和“永远不超过模型上限”在数学上无法同时成立。

## 5. 状态与持久化

### 5.1 LangGraph State

为 `create_agent` 增加自定义 `state_schema`：

```python
class ContextSummary(TypedDict):
    summary_id: str
    sequence: int
    content: str
    token_count: int
    compaction_id: str

class ContextUsageState(TypedDict):
    used_tokens: int
    max_tokens: int
    reserved_output_tokens: int
    measurement: str
    updated_at: str

class CodingAgentState(AgentState):
    context_summaries: list[ContextSummary]
    context_usage: ContextUsageState | None
    last_compaction_id: str | None
```

摘要内容和消息重建结果随 checkpoint 一起提交，保证 restore/fork 后语义状态一致。

### 5.2 业务数据库

新增当前状态表：

```sql
CREATE TABLE agent_context_states (
    context_owner_id TEXT PRIMARY KEY,
    agent_kind TEXT NOT NULL,              -- main | subagent
    session_id TEXT NOT NULL,
    timeline_id TEXT,
    task_id TEXT,
    attempt_id TEXT,
    thread_id TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    used_tokens INTEGER NOT NULL,
    max_tokens INTEGER NOT NULL,
    reserved_output_tokens INTEGER NOT NULL,
    last_prompt_tokens INTEGER NOT NULL DEFAULT 0,
    last_completion_tokens INTEGER NOT NULL DEFAULT 0,
    measurement TEXT NOT NULL,
    compression_count INTEGER NOT NULL DEFAULT 0,
    last_compaction_id TEXT,
    updated_at TEXT NOT NULL
);
```

为 timeline 增加独立 head：

```sql
ALTER TABLE timelines ADD COLUMN head_checkpoint_id TEXT;
```

当前 `TurnCoordinator.run_turn()` 从 `turns[-1].checkpoint_id` fork。手动压缩发生在两个
业务 turn 之间，不会创建新 turn；如果没有 timeline head，下一轮会绕过手动压缩产生
的新 checkpoint。迁移后：

- `timeline.thread_id + timeline.head_checkpoint_id` 表示下一次执行的唯一起点；
- user turn 的 `checkpoint_id` 保持该 turn 提交时的历史恢复点；
- turn commit、自动压缩、手动压缩和 restore 均推进 timeline head；
- 初始化迁移时，head 取每条 timeline 最新 turn 的 checkpoint；
- `TurnCoordinator.run_turn()` 从 timeline head fork，不再从最新 turn 行直接取值。

扩展现有 `compressions` 表，或迁移为下列结构：

```sql
ALTER TABLE compressions ADD COLUMN context_owner_id TEXT;
ALTER TABLE compressions ADD COLUMN trigger TEXT;
ALTER TABLE compressions ADD COLUMN status TEXT;
ALTER TABLE compressions ADD COLUMN summary_sequence INTEGER;
ALTER TABLE compressions ADD COLUMN summary_artifact_id TEXT;
ALTER TABLE compressions ADD COLUMN base_checkpoint_id TEXT;
ALTER TABLE compressions ADD COLUMN result_checkpoint_id TEXT;
ALTER TABLE compressions ADD COLUMN error_code TEXT;
ALTER TABLE compressions ADD COLUMN completed_at TEXT;

CREATE UNIQUE INDEX compression_sequence
ON compressions(context_owner_id, summary_sequence);
```

新增持久化对话通知：

```sql
CREATE TABLE conversation_notices (
    notice_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    timeline_id TEXT NOT NULL,
    turn_number INTEGER,
    kind TEXT NOT NULL,                    -- context.compression.completed
    text TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

`conversation_notices` 是 UI 对话记录，不进入模型上下文。model-visible summary 使用
LangGraph `SystemMessage`，两者职责不能混用。

### 5.3 一致性协议

SQLite 业务库和 LangGraph checkpoint 库不能做单事务，因此使用可恢复的三阶段提交：

1. Prepare
   - 读取并固定 `base_checkpoint_id`；
   - 插入 `compressions(status='generating')`；
   - 发出 `context.compression.started`。
2. Apply
   - 调用摘要模型并验证；
   - 更新 compression 为 `applying`；
   - 单次 `graph.update_state()` 写入 messages、summary chain 和 usage；
   - 获得 `result_checkpoint_id`。
3. Commit
   - 在业务库事务中更新 compression 为 `completed`；
   - 将 timeline `head_checkpoint_id` 推进到 `result_checkpoint_id`；
   - upsert `agent_context_states`；
   - 写入 `conversation_notices`；
   - 发出 `context.compression.completed`。

恢复规则：

- `generating` 且没有结果 checkpoint：标记 failed，可重新触发；
- `applying`：读取 checkpoint 的 `last_compaction_id`；
- ID 相同则补写业务状态和 notice；
- ID 不同则标记 failed，不修改当前 checkpoint；
- completed notice 使用 `compaction_id` 去重，避免 SSE 重放产生重复系统消息。

压缩提交前再次比较当前 checkpoint 是否仍等于 `base_checkpoint_id`。不一致时返回
`CONTEXT_CHANGED` 并重新判定，禁止覆盖并发产生的新消息。

### 5.4 工具输出永久暂存

#### 5.4.1 设计原则

工具输出存储不能继续作为 `TraceRecorder` 的附属能力。Trace 当前是 fail-open：
artifact 写入失败只记录 warning，并可能把小 payload 留在 SQLite。对于上下文压缩，
这会造成不可恢复的信息丢失。

新增强制边界 `ToolOutputArchiveService`：

```text
工具执行
  -> ToolOutputArchiveService 持久化完整输出
  -> 写入逻辑输出索引和所有权
  -> 生成有界 preview + tool_output_ref
  -> ToolMessage 返回给 LLM
```

规则：

1. 所有工具结果都落盘，不再设置“大于某阈值才落盘”。
2. 成功、业务失败、异常、超时、取消和 Host 中断均需留下记录。
3. 工具输出必须先持久化成功，再构造最终 `ToolMessage`。
4. 持久化失败时返回 `TOOL_OUTPUT_PERSIST_FAILED`，不得把未归档的完整输出继续交给
   LLM。
5. 已产生外部副作用的工具要在错误中明确
   `side_effect_may_have_occurred=true`，不能因归档失败宣称工具未执行。
6. 第一版不设置 TTL；引用记录和 blob 随 Session 永久保留。
7. 永久保留不等于忽略磁盘压力：达到磁盘 hard watermark 后禁止启动新工具，已经
   运行的工具尽可能归档 partial 输出并终止。

#### 5.4.2 两层标识

复用现有 `LocalArtifactStore` 的 SHA-256 内容寻址和原子写入，但区分：

- `artifact_id`：内容哈希，表示不可变物理 blob，相同内容跨调用去重；
- `tool_output_id`：随机逻辑 ID，表示“某个 Session 中某次工具调用的某个输出”。

不能只把 `artifact_id` 交给模型。内容哈希不携带 Session、Agent、tool call 或权限
信息，也无法区分两个产生相同内容的调用。LLM 和 API 对外使用 `tool_output_id`，
内部再解析到 artifact。

文件布局沿用：

```text
{workspace.data_dir}/artifacts/{sha256[0:2]}/{sha256}
```

文件写入继续采用同目录临时文件、`fsync`、`chmod 0600`、`os.replace`。目录保持
`0700`，不写入 Git workspace，也不允许 Agent 通过普通文件工具直接访问。

#### 5.4.3 数据模型

新增工具调用主表：

```sql
CREATE TABLE tool_invocations (
    invocation_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    timeline_id TEXT,
    context_owner_id TEXT NOT NULL,
    turn_id TEXT,
    attempt_id TEXT,
    thread_id TEXT NOT NULL,
    tool_call_id TEXT,
    tool_run_id TEXT,
    tool_name TEXT NOT NULL,
    status TEXT NOT NULL,
    args_digest TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
```

新增输出 part 表：

```sql
CREATE TABLE tool_outputs (
    tool_output_id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    part_name TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    media_type TEXT NOT NULL,
    encoding TEXT,
    byte_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    redacted INTEGER NOT NULL DEFAULT 1,
    complete INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(invocation_id, sequence)
);

CREATE INDEX tool_outputs_by_session
ON tool_outputs(session_id, created_at);
```

`part_name` 取 `result/stdout/stderr/error/partial/index`。`artifacts` 继续保存物理 blob
元数据；`tool_outputs` 是永久 Session 引用，因此任一引用仍存在时不得删除 blob。

#### 5.4.4 输出信封

同步工具返回值统一 canonical JSON 序列化后保存为 `result` part。命令类工具分别
保存完整字节 `stdout`、`stderr`，并保存包含退出码、超时、sandbox 证据等字段的
`result` part。异常保存结构化 `error` part。

最终写入上下文的 `ToolMessage.content` 使用有界信封：

```json
{
  "ok": true,
  "tool_output_ref": "tout_...",
  "parts": [
    {
      "name": "result",
      "media_type": "application/json",
      "byte_size": 182430,
      "sha256": "..."
    }
  ],
  "preview": "最多配置上限的安全预览",
  "preview_truncated": true,
  "persisted": true
}
```

`preview` 用于当前轮快速推理，引用用于后续核对。禁止把宿主机绝对路径写进
ToolMessage；文件路径只存在服务端元数据中。

仅用于启动后台任务的工具返回值也属于工具输出：其 run handle 归档为 `submission`
part；后台任务结束后，再向同一 invocation 追加 stdout/stderr/result parts。

#### 5.4.5 流式输出

后台命令当前仅在 `_ToolRun.chunks` 中保留一个受限滑动窗口。改为：

1. tool start 时创建同 Session 私有 spool 文件和 `tool_invocations(running)`；
2. 每个 stdout/stderr chunk 先追加 spool，再发送有界 SSE preview；
3. 分 stream 记录 byte offset 和递增 cursor；
4. terminal 时 flush + fsync，将 spool 内容提交到内容寻址 ArtifactStore；
5. 写入 stdout/stderr/result parts 后，将 invocation 标记为 terminal；
6. Host 重启时扫描未完成 spool，归档为 `partial`，invocation 标记 `interrupted`。

内存 `chunks` 只承担实时 UI 和短期 scheduler probe，不再是完整输出的事实来源。

为避免 stdout/stderr 交错关系丢失，另存一个小型 NDJSON `index` part：

```json
{"cursor":1,"stream":"stdout","offset":0,"length":120,"created_at":"..."}
{"cursor":2,"stream":"stderr","offset":0,"length":48,"created_at":"..."}
```

#### 5.4.6 脱敏与安全

Session 永久保留会放大敏感信息风险，第一版永久保存“模型可回读版本”：

- 落盘前应用现有 `redact()`，metadata 标记 `redacted=true`；
- 不持久化 API key、cookie、authorization header 等已知 secret；
- binary 输出可以保存 bytes，但默认不可直接注入 LLM；
- 回读限制单次字节/token 数，不允许一次加载整个超大 artifact；
- 回读必须校验 Session、timeline lineage、parent/child Agent 授权关系；
- artifact HTTP 下载继续要求本地认证，并增加 Session 所有权校验。

若未来需要保留未脱敏原文，应另建加密 vault，不能复用 LLM 可读 artifact。

#### 5.4.7 LLM 回读工具

新增只读工具：

```text
list_tool_outputs(
  tool_name?, status?, turn_number?, attempt_id?, limit=50, cursor?
)

read_tool_output(
  tool_output_id,
  part_name="result",
  byte_offset=0,
  max_bytes=32768
)

search_tool_output(tool_output_id, query, max_matches=50)
```

这些工具必须：

- 从当前 `context_owner_id` 推导授权范围，不接受调用方传入 session_id；
- 返回稳定 cursor、总大小、sha256 和是否还有后续内容；
- 文本按 UTF-8 安全边界分页；
- JSON 支持可选 JSON Pointer 读取，避免加载整个结构；
- binary 仅返回 metadata，除非有明确的受限解码器；
- 每次回读本身也作为新的工具调用归档，形成完整审计链。

主 Agent 默认可读取本 Session 当前 timeline 祖先和其子 Agent 产生的输出。子 Agent
默认只可读取本 attempt 的输出，以及父 Agent 显式授予的 `tool_output_id`。

#### 5.4.8 与上下文压缩集成

压缩前增加不变量检查：

```text
每个将被丢弃的 ToolMessage
  -> 必须含 persisted=true
  -> tool_output_ref 必须可解析
  -> artifact sha256 必须校验通过
```

任一检查失败时禁止压缩，返回 `UNARCHIVED_TOOL_OUTPUT`，避免不可逆丢失。

不能依赖摘要模型正确抄写所有引用。每次压缩应由系统确定性生成
`tool_evidence_manifest`，包含被压缩区间内的 `tool_output_id`、tool name、状态、时间
和 part 元数据。manifest 自身作为 artifact 保存，并在新上下文加入：

```text
SystemMessage(
  "[TOOL EVIDENCE]\n"
  "manifest_ref=manifest_...\n"
  "Use list_tool_outputs/read_tool_output to verify prior tool evidence."
)
```

新上下文顺序调整为：

```text
base system prompt
historical summaries
current summary
tool evidence manifest reference
recent user inputs（总计最多 2000 token）
```

manifest 只保存索引，不复制工具正文，因此不会随输出体积线性占用上下文。

#### 5.4.9 生命周期与配额

第一版采用 Session 永久保留：

- `expires_at = NULL`；
- Session 正常使用期间不执行 TTL GC；
- artifact blob 只有在不存在任何 Session、trace、summary、tool output 引用时才允许
  删除；
- restore timeline 不复制 blob，只建立 lineage 可见性；
- 删除 Session 的能力未来单独设计，默认不级联静默删除 artifact。

必须提供磁盘观测：

```text
tool_output_storage_bytes
tool_output_count
artifact_deduplicated_bytes
artifact_write_failures
partial_spool_count
```

建议配置 soft/hard watermark。soft watermark 仅告警；hard watermark 禁止启动可能
产生输出的新工具，避免出现“工具已执行但证据无法保存”的状态。

#### 5.4.10 后台工具与 Turn 结束条件

压缩不与后台工具并发。只要存在 active invocation，当前 turn 就尚未结束，不允许
自动或手动压缩。

后台工具处理规则：

1. 工具继续运行并持续写 spool。
2. scheduler 可以在预算允许时继续正常模型采样，但压缩判定保持关闭。
3. Runtime 准备结束 turn 时先 `seal_thread()`，禁止启动新工具。
4. `drain_thread()` 等待工具进入 completed/failed/cancelled。
5. 超时或用户取消时终止工具，并把已产生内容归档为 partial artifact。
6. `finalize_thread()` 完成 result artifact、invocation 状态和 trace。
7. 所有 tool call 配对完整后才提交 turn。
8. turn commit 后执行唯一一次自动压缩判定。

手动压缩不排队、不延迟执行。CLI/WebUI 只能在没有 active turn 的情况下调用；若
Agent 或后台工具仍在运行，立即返回 `TURN_ACTIVE`。这避免一个旧的手动请求在用户
不再预期的时刻自动修改 timeline head。

当 turn 内预算不足时，不引入 active-tail 压缩，而是提前关闭 turn：

```text
停止新采样和新工具
-> seal/drain/finalize
-> 归档输出
-> 提交当前 turn
-> 边界强制压缩
```

因此不需要 `stable_prefix`、`active_tail`、`inflight_tool_manifest`、
`context_generation` 或 `delivery_lease`。工具结果必须在 turn commit 前进入当前
checkpoint；restore/fork 仍只发生在空闲状态，不存在结果跨压缩 checkpoint 回灌。

## 6. 运行时组件

新增：

```text
coding_agent/context/state.py
  ContextOwner, ContextUsageSnapshot, ContextSummary

coding_agent/context/store.py
  ContextStateRepository

coding_agent/context/decision.py
  CompressionPolicy

coding_agent/context/llm_compactor.py
  LlmContextCompactor

coding_agent/context/budget.py
  ContextBudgetGuard

coding_agent/context/service.py
  ContextCompressionService

coding_agent/tool_outputs/models.py
  ToolInvocation, ToolOutputPart, ToolOutputEnvelope

coding_agent/tool_outputs/store.py
  ToolOutputArchiveStore

coding_agent/tool_outputs/service.py
  ToolOutputArchiveService

coding_agent/tool_outputs/tools.py
  list_tool_outputs, read_tool_output, search_tool_output
```

职责：

- `ContextBudgetGuard`：turn 内每次调用前后计量，超限时安全终止 turn，不执行压缩。
- `CompressionPolicy`：纯函数，只处理 turn-end 自动判定和空闲边界手动触发。
- `LlmContextCompactor`：只负责摘要调用和输出验证。
- `ContextCompressionService`：turn-boundary 锁、三阶段提交、事件和审计。
- `ContextStateRepository`：当前状态、摘要索引、恢复与查询。
- `ToolOutputArchiveService`：强制归档、spool 恢复、所有权和内容完整性校验。
- `AgentRuntime`：提供 checkpoint/message 适配，不再持有不可恢复的唯一计量真相。

现有规则式 `ToolResultBudget` 可保留为压缩前的低成本预处理，但它不能代替本次 LLM
摘要。建议顺序：

```text
oversized tool result artifact 化
-> 构造完整压缩输入
-> LLM handoff summary
-> 生成 tool evidence manifest
-> 原子重建上下文
```

删除或废弃：

- `compression_count >= 5` 的硬编码；
- cache hit 改变阈值的隐式策略；
- `EmergencyConversationCompact` 对用户/assistant 文本的字符级截断；
- 把 completion token 加入上下文占用的计算方式。

## 7. 主 Agent 与子 Agent 接入

### 7.1 主 Agent

`TurnCoordinator.run_turn()`：

1. 从源 checkpoint fork 执行 thread；
2. 同时复制或绑定 `context_owner_id`；
3. 每次采样只更新 usage，并由 `ContextBudgetGuard` 执行硬上限保护；
4. seal/drain/finalize 所有后台工具并归档输出；
5. 提交 workspace snapshot、turn checkpoint 和业务 turn；
6. 重新计算并 upsert `agent_context_states`；
7. 在 turn commit 后执行一次自动压缩判定；
8. 如触发压缩，生成新 checkpoint 并推进 timeline head；
9. 压缩失败不回滚已经完成的业务 turn。

### 7.2 子 Agent

创建 `AgentRuntime` 时必须传入：

```text
context_owner_id = subagent:{attempt_id}
context_state_repository
context_event_callback
```

Runtime 初始化时从 attempt checkpoint 恢复 `context_summaries/context_usage`，不能因为
`run_phase()` 重建 Runtime 而清零。每个 phase 结束后更新 attempt 的 checkpoint 和
context state。

Subagent 卡片展示该 attempt 的圆环。运行中的子 Agent 通过现有 subagent event 流
增加：

```text
context.window_usage
context.compression.started
context.compression.progress
context.compression.completed
context.compression.failed
```

## 8. API、事件与 CLI

### 8.1 查询 API

```http
GET /api/v1/sessions/{session_id}/context
```

返回：

```json
{
  "main": {
    "context_owner_id": "main:session:timeline",
    "used_tokens": 64210,
    "max_tokens": 128000,
    "usage_ratio": 0.5016,
    "measurement": "corrected_estimate",
    "compression_count": 1,
    "updated_at": "..."
  },
  "subagents": [
    {
      "attempt_id": "...",
      "used_tokens": 31020,
      "max_tokens": 128000,
      "usage_ratio": 0.2423
    }
  ]
}
```

`GET /status` 可携带当前 Session 的 main snapshot，避免首屏额外等待；完整子 Agent
状态仍由 session context API 或 subagent runs 返回。

### 8.2 手动压缩 API

```http
POST /api/v1/sessions/{session_id}/context/compact
{
  "expected_timeline_id": "...",
  "client_request_id": "..."
}
```

返回 202 operation。该接口只允许在 turn 结束后的空闲边界调用：

- 当前 turn 空闲时创建 `context_compact` operation 并立即执行；
- 当前 turn、后台工具或其他 graph operation 仍运行时返回 `TURN_ACTIVE`；
- 不创建 deferred request，不在稍后的非预期时间自动压缩；
- timeline 已变化返回 `TIMELINE_CHANGED`；
- 幂等键避免重复点击生成多个 summary；
- operation kind 为 `context_compact`，复用现有 SSE 和取消机制。

子 Agent 如需开放手动压缩，使用：

```http
POST /api/v1/subagent-attempts/{attempt_id}/context/compact
```

attempt 位于 `waiting_parent`、`waiting_capability`、completed 等 turn 边界状态时
允许执行；只要模型或后台工具仍运行就返回 `TURN_ACTIVE`。

### 8.3 SSE 事件

```text
context.window_usage
context.compression.requested
context.compression.started
context.compression.progress
context.compression.completed
context.compression.failed
```

自动压缩的事件顺序固定为：

```text
assistant.completed
-> turn.committed
-> context.window_usage
-> context.compression.started（达到阈值时）
-> context.compression.progress
-> context.compression.completed | context.compression.failed
-> operation.completed
```

因此 WebUI 只有在本轮 assistant 回复和 turn 均已完成后才进入“压缩中”状态。

完成事件示例：

```json
{
  "compaction_id": "...",
  "context_owner_id": "...",
  "trigger": "manual_web",
  "summary_id": "summary_001",
  "before_tokens": 103400,
  "after_tokens": 18750,
  "max_tokens": 128000,
  "usage_ratio": 0.1465,
  "notice_id": "...",
  "created_at": "..."
}
```

状态文案：

- started：`正在准备上下文压缩`
- progress/generating：`正在生成交接摘要`
- progress/applying：`正在重建上下文`
- completed：`上下文压缩完成：103,400 -> 18,750 tokens，已生成 summary_001`
- failed：`上下文压缩失败，原上下文未变更`

### 8.4 CLI

新增：

```text
/context      查看当前 Agent 的 used/max、比例、计量来源和压缩次数
/compact      立即压缩当前活动 timeline
```

CLI 使用同一个 `ContextCompressionService`，不能另写一套压缩逻辑。输出过程：

```text
system> 正在准备上下文压缩
system> 正在生成交接摘要
system> 正在重建上下文
system> 上下文压缩完成：103,400 -> 18,750 tokens，已生成 summary_001
```

## 9. WebUI 设计

### 9.1 主 Agent 圆环

在 `ConversationHeader` 右侧增加固定 28x28 px 的 SVG 圆环：

- 0%-79%：绿色；
- 80%-89%：黄色；
- 90% 以上：红色；
- 压缩中：环形 spinner，保持固定尺寸，避免 header 抖动；
- 圆环按钮 title/tooltip：
  `上下文 64,210 / 128,000 tokens (50.2%)`；
- 点击圆环打开小菜单，提供“立即压缩”；
- operation 运行中禁用手动压缩并说明原因。

圆环只表达占用，不在中心塞长文本；精确数字通过 tooltip 和菜单展示。

### 9.2 子 Agent 圆环

每个 `SubagentCard` header 增加 22x22 px 圆环，读取当前 attempt context snapshot。
tooltip 同样显示 used/max。安全暂停状态可提供手动压缩，执行中仅展示。

### 9.3 对话系统消息

扩展对话查询为有序 item：

```ts
type ConversationItem =
  | { kind: "turn"; turn: Turn }
  | {
      kind: "system_notice";
      notice_id: string;
      notice_kind: "context.compression.completed";
      text: string;
      created_at: string;
    };
```

系统通知使用紧凑、非卡片式行展示，带 `History` 或 `CircleDot` 图标。完成 notice 从
数据库加载，因此刷新页面后仍存在。started/progress 是运行态，不持久化到历史；
completed 必须持久化。

前端 Session runtime 新增：

```ts
contextUsage: ContextUsage | null;
compressionState:
  | "idle"
  | "preparing"
  | "generating"
  | "applying";
```

收到 `context.window_usage` 或 `context.compression.completed` 后立即更新圆环，不等待
整个 query refresh。

## 10. 配置

在 `AgentConfig`、环境变量、JSON 配置和 Web Settings 中增加：

```text
context_auto_compact_ratio        default 0.80, range 0.50..0.95
context_recent_user_inputs_max_tokens default 2000, range 256..8000
context_summary_max_tokens        default 4000, range 512..16000
context_summary_chain_max_tokens  default 16000
context_summary_model             default null（复用当前模型）
context_compaction_enabled        default true
tool_output_preview_tokens        default 1200, range 128..8000
tool_output_read_max_bytes        default 32768, range 4096..1048576
artifact_soft_min_free_bytes      default 5368709120
artifact_hard_min_free_bytes      default 1073741824
tool_output_spool_fsync_bytes     default 1048576
```

模型最大窗口不能继续只依赖未知模型的 128K 猜测。优先级：

1. 用户显式 `context_window_tokens`；
2. 已登记 model profile；
3. 无可靠值则启动时报配置错误。

这避免圆环分母错误以及错误的 hard-limit 决策。若需要兼容现状，可先保留 fallback，
但 API 必须返回 `limit_source = configured | profile | fallback`。

## 11. 文件级实施清单

| 文件 | 修改 |
| --- | --- |
| `coding_agent/config.py` | 新增压缩配置、环境变量读取和持久化 |
| `coding_agent/context/config.py` | 将固定 70% 迁移为用户配置的 80% |
| `coding_agent/context/accounting.py` | 分离窗口占用与累计调用 usage |
| `coding_agent/context/callbacks.py` | 每次采样记录 usage，供预算保护和 turn-end 校正 |
| `coding_agent/context/state.py` | 新增 graph state 和领域模型 |
| `coding_agent/context/store.py` | 新增上下文状态与恢复仓储 |
| `coding_agent/context/decision.py` | 新增纯函数压缩策略 |
| `coding_agent/context/llm_compactor.py` | 新增无工具摘要调用 |
| `coding_agent/context/service.py` | 新增压缩编排与三阶段提交 |
| `coding_agent/context/budget.py` | 新增 turn 内采样预算保护，不执行压缩 |
| `coding_agent/context/compressor.py` | 保留 artifact 预处理，移除字符截断职责 |
| `coding_agent/tool_outputs/*` | 新增强制工具输出归档、索引、回读和恢复 |
| `coding_agent/tracing/artifacts.py` | 抽取为可复用 CAS blob store，保留原子写入 |
| `coding_agent/tracing/recorder.py` | 关联已归档输出并扩充 compression 审计，不再作为唯一持久化入口 |
| `coding_agent/tracing/callbacks.py` | 同步/MCP tool completion 接入统一归档边界 |
| `coding_agent/execution/tool_runs.py` | 流式 spool、terminal finalize、partial 恢复 |
| `coding_agent/execution/host.py` | 删除重复落盘逻辑，返回统一 output envelope |
| `coding_agent/sandbox/execution.py` | 删除重复落盘逻辑，返回统一 output envelope |
| `coding_agent/runtime.py` | 接入自定义 state、owner、service 和事件 callback |
| `coding_agent/coordinator.py` | turn commit/restore 时同步 context state |
| `coding_agent/repository.py` | timeline head、migration、context state、notice 查询 |
| `coding_agent/application/service.py` | 查询/手动压缩 operation、SSE 事件 |
| `coding_agent/api/schemas.py` | `CompactContextRequest` |
| `coding_agent/api/app.py` | context 查询与 compact endpoint |
| `coding_agent/cli.py` | `/context`、`/compact` 和过程输出 |
| `coding_agent/subagents/manager.py` | attempt owner、状态恢复和事件转发 |
| `coding_agent/subagents/repository.py` | 子 Agent context snapshot 查询 |
| `web/src/lib/api.ts` | Context 类型、事件和接口 |
| `web/src/App.tsx` | 圆环、手动压缩、系统 notice |
| `web/src/styles.css` | 固定尺寸圆环和 notice 样式 |

## 12. 实施顺序

### Phase 1：正确计量和持久化

1. 修正占用定义，分离 prompt/completion/cumulative usage。
2. 引入 `context_owner_id` 和 `agent_context_states`。
3. 每次采样更新内存状态，每个 turn/phase 结束持久化。
4. status/context API 返回主、子 Agent snapshot。
5. WebUI 圆环和 tooltip 落地。
6. 引入强制 `ToolOutputArchiveService`，迁移同步、MCP 和后台工具输出。

### Phase 2：LLM 摘要压缩

1. 增加自定义 LangGraph state。
2. 实现 compaction prompt、摘要调用和验证。
3. 实现摘要链、近期用户输入集合截断和一次性 state update。
4. 生成并注入确定性的 tool evidence manifest。
5. 接入 turn commit 后自动判定和空闲边界手动触发。
6. 实现 turn 内预算耗尽时的安全收束。
7. 扩充 trace 与 compression 审计。

### Phase 3：交互与恢复

1. 增加 Web/CLI 手动压缩。
2. 增加 started/progress/completed/failed 事件。
3. 增加持久化 conversation notice。
4. 完成 applying 状态恢复、幂等和 checkpoint 冲突处理。
5. 接入子 Agent 卡片和暂停态手动压缩。

## 13. 测试方案

### 13.1 单元测试

- 占用只表示下一次输入，不把 completion 简单累加到窗口。
- 自动阈值默认 80%，边界 `79.99%/80%`。
- manual 在低占用下仍触发。
- turn 内达到 hard limit 时预算保护终止采样，不调用压缩器。
- 近期用户消息过滤内部 origin，从最新向前选取、按原始顺序重放，总计严格不超过
  2000 token。
- summary 序号从 `summary_000` 单调增长。
- 第二次压缩顺序为旧 summary、新 summary、按时间升序排列的近期用户输入。
- 摘要为空、超长、含 tool call 时拒绝提交。
- 压缩失败后原 checkpoint 不变。
- tool call 未配对或后台工具未 terminal 时不允许进入 turn boundary。
- provider limit 不在当前 turn 内压缩重试，而是安全收束 turn。
- 所有同步工具结果在 ToolMessage 可见前已经落盘。
- 相同输出复用 artifact blob，但生成不同 `tool_output_id`。
- 归档失败返回 `TOOL_OUTPUT_PERSIST_FAILED`，不泄漏未归档完整输出。
- Session、timeline lineage 和 parent/child Agent 所有权校验正确。
- read 按 byte/token 上限分页，UTF-8 边界不损坏。
- 自动和手动压缩均拒绝非 turn-boundary 状态。

### 13.2 集成测试

- 单次 graph run 内多次 LLM 采样只计量，不触发压缩。
- turn commit 后只执行一次自动判定。
- 自动压缩发生在业务 turn 提交后，并单独推进 timeline head checkpoint。
- Host 重启后主 Agent used/max、摘要链和压缩次数恢复。
- 子 Agent Runtime 跨 phase 重建后状态不清零。
- timeline restore 复制源 turn 的摘要链和 context state，不污染旧 timeline。
- `applying` 中断后可以根据 `last_compaction_id` 补偿提交。
- 并发手动压缩与新 turn 只能有一个成功，另一个得到明确冲突。
- stdout/stderr 流式写入完整保留，内存窗口淘汰不影响 artifact。
- Host 中断后 spool 恢复为 partial artifact。
- 压缩前发现未归档 ToolMessage 时拒绝压缩。
- 压缩后可通过 manifest 和 read tool 核对旧工具输出。
- Session 重启后工具输出引用仍可读取。
- 后台工具未 terminal 时不会执行自动或手动压缩。
- turn 内预算耗尽时停止新采样，归档工具输出并提交 turn，随后强制压缩。

### 13.3 Web E2E

- 圆环按事件实时更新，悬浮显示精确 `used/max`。
- 80%/90% 颜色状态正确。
- 手动压缩按钮创建 operation，并展示三个阶段。
- turn 运行时手动压缩按钮禁用；并发请求返回 `TURN_ACTIVE`。
- 压缩完成 notice 出现在对话中，刷新后仍存在且不重复。
- 主 Agent 和每个子 Agent 显示各自计量，不串数据。
- SSE 重连从 cursor 恢复，不重复 notice。

### 13.4 CLI

- `/context` 输出准确状态。
- `/compact` 低占用也执行。
- 开始、生成、重建、完成均可见。
- 压缩失败返回错误且后续会话仍使用原 checkpoint。

## 14. 验收标准

1. 任一主 Agent turn 或子 Agent phase 结束后，数据库均存在其最新
   `used_tokens/max_tokens`。
2. 重启 Host、切换 Session、恢复 timeline 后，计量和摘要链保持一致。
3. WebUI 主/子 Agent 圆环显示正确，tooltip 提供精确 token 数。
4. 每个 turn commit 后只执行一次判定；达到 80% 自动压缩。
5. turn 内达到安全上限时不会执行压缩或继续采样，而是安全结束 turn 后强制压缩。
6. CLI/WebUI 手动触发在低于阈值时也会压缩，但非空闲 turn 边界返回 `TURN_ACTIVE`。
7. 压缩模型收到完整现有对话上下文和末尾 handoff prompt。
8. 新上下文严格按 system prompt、历史 summaries、本次 summary、按原始顺序排列的
   近期用户输入组装，用户输入总计不超过 2000 token。
9. 压缩提交不会留下未配对 tool call，不会覆盖并发 checkpoint。
10. 压缩完成系统 notice 可实时看到、可刷新恢复、不会进入模型上下文。
11. 每次压缩均可通过 compaction ID 关联触发源、前后 token、摘要 artifact、基础和
    结果 checkpoint。
12. 任一模型可见工具结果都包含有效 `tool_output_ref`，其完整输出已原子落盘。
13. 压缩后 LLM 可通过 evidence manifest 和受限回读工具核对历史工具输出。
14. 工具输出随 Session 永久保留，且不会因 trace 导出失败、Host 重启或内存窗口淘汰
    而丢失。
15. 后台工具运行期间不会触发或排队压缩；所有工具 terminal、输出归档且 turn commit
    后，才允许自动或手动压缩。

## 15. 结论

当前代码可复用的基础包括 token 估算、provider usage callback、checkpoint 消息替换、
operation SSE、artifact 和 compression trace。需要重构的核心是：

1. 把“单次调用 usage”改造成“每个上下文 owner 的可恢复窗口状态”；
2. 把规则式消息裁剪升级为专用 LLM handoff summary；
3. 把压缩判定统一放到 turn commit 后，turn 内只保留计量和预算保护；
4. 用 checkpoint state + 业务状态索引 + 可恢复提交协议保证一致性；
5. 将所有工具输出先归档为 Session 永久 artifact，再向 LLM 返回有界预览和引用；
6. 为 CLI/WebUI 提供同源的手动触发、过程事件、圆环和持久化系统通知。

不建议直接在现有 `_maybe_compress_context()` 中继续叠加条件。它位于
`graph.stream()` 前，无法表达“turn 已提交、工具已结束”的压缩边界。应由
`ContextBudgetGuard` 负责 turn 内保护，由 `ContextCompressionService` 在
`TurnCoordinator` 提交 turn 后统一执行压缩。
