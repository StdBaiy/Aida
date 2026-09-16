# Coding Agent MVP

本地交互式 Coding Agent。它在指定 Git 工作区内搜索、读取和修改代码，通过安全策略校验后默认批准并运行命令，并将每轮 LangGraph checkpoint 与 Git 文件快照联合保存。

## MVP 能力

- OpenAI 兼容 Chat Completions 接口。
- 从静态配置的 Streamable HTTP MCP Server 加载 Tools。
- 从项目、用户和 Codex 目录发现并按需加载 Skill。
- 流式多轮 CLI 对话和 SQLite checkpoint。
- React Web 工作台、FastAPI 本地 Host 和可重放 SSE 事件。
- 工作区内文件列表、读取、glob 和 ripgrep 搜索。
- 带 SHA256 乐观锁和失败回滚的结构化 patch。
- macOS 上每个模型可控命令默认经 Seatbelt profile 执行，限制文件写入并关闭网络。
- Git 临时 index 快照，不修改 HEAD、分支或真实 index。
- `/restore` 从历史轮 fork 新 timeline，并同时恢复对话状态和工作区文件；新 timeline
  保留恢复点之前的可见 history，后续轮次继续编号。
- 本地完整 Trace，覆盖模型、工具、审批、checkpoint、snapshot 和异常。
- LangSmith 仅上传固定 schema 的聚合指标，不上传消息、源码、路径、命令或输出。
- 路径逃逸、外部符号链接、`.git`、常见密钥文件和高危命令拦截。

Seatbelt MVP 允许读取系统运行时和明确配置的工具链目录，只允许写当前 workspace
（`.git` 除外）及每次执行独立的临时 HOME。它不提供容器或 microVM 级内核隔离，也不能
可靠限制 CPU、内存和 PID；高风险恶意代码仍应使用 OCI、gVisor 或 microVM Provider。

## 提示词维护

提示词资源位于 `coding_agent/prompting/assets/`，`manifest.json` 声明 profile、版本、
owner、组件文件和所需工具。修改文本时同步更新组件版本和 profile 版本，重建 Runtime 后生效。
`coding_agent/prompts.py` 保留旧调用入口，Runtime 使用 `assemble_prompt()` 统一组装。

- 固定规则根据实际工具集合选择；只读子 Agent 不会收到 Skill 加载或父任务调度协议。
- Skill 目录、子任务合同、阶段分析和调度 payload 有独立数据边界、长度限制和标签转义。
  超限返回 `PROMPT_CONTEXT_TOO_LARGE`，不静默丢失上下文。转义不替代权限校验。
- `model.call` 本地 trace 的 `prompt` 属性包含 profile/version、组件来源、字符数、
  bundle/system/tool schema/events digest；该元数据不包含动态正文。
- 调度事件继续使用兼容的 user 消息传输，来源在消息元数据中单独记录；
  尚未迁移到独立的 graph event channel。远端 LangSmith 仍只上传原有聚合指标。
- 当前仍使用完整 Skill 目录，尚未启用 top-k、workspace policy、远端 Prompt Hub 或模型行为评测。

回归命令：`uv run pytest tests/test_prompting.py tests/test_skills.py tests/test_tracing.py -q`。
设计和分阶段进度见 [提示词系统分析](PROMPT_SYSTEM_ANALYSIS.md)。

## 安装

```bash
cd apps/coding-agent
uv sync --all-groups
```

## 配置

```bash
export CODING_AGENT_MODEL=gpt-4.1-mini
export OPENAI_API_KEY=...
# OpenAI 兼容服务可选：
export CODING_AGENT_BASE_URL=https://example.com/v1
# LangSmith 指标项目名可选：
export LANGSMITH_API_KEY=...
export CODING_AGENT_LANGSMITH_PROJECT=coding-agent-evaluation
# 同一工作区最多并行运行的 session 数，默认 4：
export CODING_AGENT_MAX_PARALLEL_SESSIONS=4
# 默认使用 macOS Seatbelt：
export CODING_AGENT_SANDBOX_PROVIDER=seatbelt
```

Runtime 启动时会实际运行最小 Seatbelt profile 探针。`sandbox-exec` 缺失、平台不是
macOS、当前进程禁止嵌套 Seatbelt 或探针失败时，`run_command` 会 fail closed，绝不
回退宿主机。默认配置为：

```json
{
  "sandbox_enabled": true,
  "sandbox_provider": "seatbelt",
  "sandbox_workspace_growth_bytes": 1073741824,
  "sandbox_max_parallel_per_session": 2,
  "sandbox_max_parallel_global": 4
}
```

Seatbelt 真实边界测试必须从未被其他 Seatbelt profile 包裹的普通终端运行：

```bash
uv run pytest tests/test_sandbox.py -k real_workspace_boundary -v
```

`sandbox-exec` 是 Apple 的非公开弃用接口。本 Provider 面向轻量本地防误操作场景，
不能替代容器或 microVM 对主动恶意代码的隔离。

需要显式切回保留的 Docker Provider 时，先构建可信基础镜像：

```bash
docker build -f sandbox/Dockerfile -t coding-agent-sandbox:local .
docker image inspect coding-agent-sandbox:local --format '{{.Id}}'
```

再配置：

```json
{
  "sandbox_enabled": true,
  "sandbox_provider": "docker",
  "sandbox_image": "coding-agent-sandbox:local",
  "sandbox_cpu_limit": 2,
  "sandbox_memory_bytes": 2147483648,
  "sandbox_pid_limit": 256,
  "sandbox_workspace_growth_bytes": 1073741824,
  "sandbox_max_parallel_per_session": 2,
  "sandbox_max_parallel_global": 4
}
```

也可以通过 `--model` 和 `--base-url` 覆盖。配置文件默认为
`~/.config/langchain-coding-agent/config.json`，但 API key 只从环境变量读取。检测到
`LANGSMITH_API_KEY`（或 `LANGCHAIN_API_KEY`）时会自动开启指标上传；也可通过
`--langsmith` 开启、通过 `--no-langsmith` 关闭。需要持久设置时使用
`CODING_AGENT_LANGSMITH_ENABLED=true/false`。

MCP Server 通过配置文件静态声明：

```json
{
  "mcp_servers": {
    "docs": {
      "url": "https://example.com/mcp",
      "timeout_seconds": 30
    }
  }
}
```

Server 名称会作为工具名前缀。首版仅支持 HTTP(S) Tools，不支持 stdio、Resources、
Prompts、OAuth 和 elicitation。MCP Server 必须视为受信配置；其工具产生的外部副作用
不会随本地 Git snapshot 恢复。

Skill 按以下优先级发现，同名时只使用优先级最高的版本：

```text
<workspace>/.agents/skills/<skill-name>/SKILL.md
~/.agents/skills/<skill-name>/SKILL.md
~/.codex/skills/<skill-name>/SKILL.md
```

`SKILL.md` 必须以 YAML 风格的 frontmatter 开头，声明与目录同名的 `name` 和非空
`description`。Runtime 启动时只把名称、来源和描述加入系统提示词；当请求明确匹配
描述时，Agent 才通过 `load_skill(name)` 工具加载完整内容。该工具只接受启动时完成
校验和映射的 Skill 名称，不接受文件路径，因此不会放宽 `read_file` 的 workspace
访问边界。Skill 不能覆盖系统安全规则或获得额外工具权限。新增 Skill 后需要重启
Runtime 才能刷新目录和同名覆盖关系。

需要执行能力的 Skill 在 `SKILL.md` 同目录增加 `skill.json`：

```json
{
  "version": 1,
  "commands": {
    "check": {
      "description": "Run the Skill's validation script.",
      "script": "scripts/check.py",
      "interpreter": "python3",
      "allow_args": true,
      "timeout_seconds": 120,
      "env_from_env": {
        "SERVICE_TOKEN": "MY_SERVICE_TOKEN"
      }
    },
    "tool": {
      "description": "Run a trusted CLI.",
      "argv": ["my-cli", "inspect"],
      "allow_args": true
    }
  },
  "mcp_servers": {
    "docs": {
      "url": "https://example.com/mcp",
      "timeout_seconds": 30,
      "headers_from_env": {
        "Authorization": "DOCS_AUTHORIZATION"
      }
    }
  }
}
```

`script` 必须是 Skill 目录内的相对路径，并声明解释器；`argv` 用于固定 CLI 前缀，
二者只能选一个。命令不经过 shell，并继续受 `CommandPolicy` 限制。环境变量配置只保存
来源变量名，值在执行时读取且不会进入 manifest。Agent 首次调用 `activate_skill` 时
请求用户授权，授权仅在当前 Runtime 的当前会话中复用；MCP 也只在授权后连接。

仓库提供 `.agents/skills/skill-smoke-test` 用于验证完整链路。先在一个终端启动它的
本地 MCP Server：

```bash
uv run python .agents/skills/skill-smoke-test/scripts/mcp_server.py
```

再在另一个终端启动 Agent：

```bash
uv run coding-agent --workspace "$(pwd)"
```

发送以下请求并批准首次 Skill 激活：

```text
使用 skill-smoke-test：先调用 echo 脚本并传入 hello skill，
再调用 smoke_echo 发送 hello mcp，最后调用 smoke_add 计算 20 + 22。
```

预期脚本结果包含 `kind: skill-script` 和参数 `["hello", "skill"]`，MCP 结果分别包含
`message: hello mcp` 与 `result: 42`。也可直接运行执行层自动化测试：

```bash
uv run pytest -q tests/test_skill_execution.py
```

命令审批策略默认为自动通过，但不会绕过 `CommandPolicy`：禁止的程序、远程 Git
操作、动态脚本执行和含控制字符的参数仍会被直接拒绝。

## 启动

```bash
uv run coding-agent --workspace /absolute/path/to/git/repository
```

启动时会输出 `session_id`。恢复已有会话：

```bash
uv run coding-agent \
  --workspace /absolute/path/to/git/repository \
  --session <session-id>
```

启动 Web 工作台：

```bash
uv run coding-agent web --port 8765
```

也可以继续通过 `--workspace /absolute/path/to/git/repository` 指定启动目录。未指定时
Web UI 显示目录选择页，并提供最近使用目录；任意时刻只激活一个目录。切换时会关闭旧
Runtime、释放旧仓库锁并打开新目录，各仓库的会话和 Trace 数据彼此独立。Web Host 持有
活动仓库的写锁，不能与同一工作区的独立 CLI 同时运行。

Web 顶部的 Settings 可修改模型、Base URL、模型调用超时、命令超时和 LangSmith 配置。点击“应用”
后会热重建 Runtime，从下一轮对话开始生效，并将非敏感配置写回本地配置文件。模型和
LangSmith API Key 始终只从 Host 启动时的环境变量读取，不会进入页面、数据库或配置文件。

工作台的“运行双 Agent Demo”会并行启动两个后台子 Agent。每个 Attempt 使用独立 Git
worktree，并且只能调用主 Agent 下发的 `mock_sleep` 和 `write_deliverable` 工具。两个
首次 Attempt 都会真实运行至少 10 秒：Agent A 直接通过验收，Agent B 首次失败并接收
结构化反馈，再从失败 commit 创建第二个 Attempt 完成返工。验收结果发布到
`refs/coding-agent/subagent-integrations/<run_id>/<task_id>`，不会修改当前检出分支。
任务、Attempt 和进度事件保存在仓库的 `coding-agent/agent.db` 中，页面刷新后仍可恢复。

主 Agent也可以通过 `create_agent_tasks` 原子创建最多两个真实子 Agent，并用
`inspect_agent_task`、`wait_agent_tasks`、`request_agent_revision` 和
`accept_agent_result` 完成监控、返工和验收。子 Agent运行完整 Agent Runtime，但只能
看到任务契约授予的工具。`workspace_mode=none|auto|required` 控制 worktree 分配；
`auto` 模式只在子 Agent 调用 `request_workspace` 后创建 worktree。子 Agent 可以通过
`request_parent_input` 或 `request_capability` 唤醒主 Agent，唤醒请求会持久化并在当前
父 turn 结束后调度。MCP 工具以 `mcp:<exact_tool_name>` 精确授权，未授权的同服工具仍会
被 provider 拒绝。分析结果可以不带 commit，代码结果验收通过后才将 commit diff 应用到
父工作区。

Web Host 为每个 session 维护独立的运行槽，同一 session 串行、不同 session 最多两个
并行。运行中的 session 可以切换离开并稍后通过 SSE 重放恢复；Stop 按钮使用协作式取消，
会同时终止模型后续调度、审批等待和该 turn 的后台 ToolRun。共享工作区写操作通过 mutation
lease 串行化到 turn snapshot 完成。

子 Agent 的正式输出使用结构化 `ResultEnvelope`。摘要、检查、证据与 provenance 分开
保存和展示；无法解析的旧式自由文本只保存在 `legacy_raw_output`，不会再直接污染摘要。

## CLI

- `/status`：显示当前 session、timeline 和 workspace。
- `/history`：显示当前 timeline 已提交轮次。
- `/trace N`：显示第 N 轮的本地 Trace、Span 和 artifact 目录。
- `/restore N`：确认后从第 N 轮创建新 timeline，并恢复 Agent 状态和文件。
- `/quit`：退出。

本地数据写入实际 Git 目录下的 `coding-agent/`，不会污染工作树。Span 和小型
payload 保存在 `agent.db`；超过 16 KiB 的 payload、完整命令输出及 diff 使用
SHA256 内容寻址写入 `artifacts/`。API key、Authorization、cookie、密码和常见
token 格式在落盘前脱敏。

LangSmith 导出是独立的合成 run，并显式关闭 Agent 原始链路的全局 LangSmith
tracer。导出失败不会影响已提交轮次，失败状态保存在本地，并在后续启动时重试。

长时命令和 MCP 调用以当前 turn 内的后台 `ToolRun` 执行。模型通过
`inspect_tool_run`、`wait_for_tools` 和 `cancel_tool_run` 检查增量输出、等待或取消任务，
也可以在并发上限内启动独立任务。Scheduler 会按 `tool_probe_interval_seconds` 唤醒提前
结束规划但仍有任务运行的模型；所有 ToolRun 必须在 turn 提交和 workspace snapshot 前
完成或取消。`max_parallel_tools` 和 `max_tool_scheduler_wakes` 可在配置文件中调整。
Session operation 使用独立线程池执行，`max_parallel_sessions` 控制同一工作区的
并行 session 数量，默认 4，也可在 Web Settings 中修改。超过上限的任务进入线程池队列。
Web 工作台通过可重放 SSE 为每个活跃 ToolRun 展示独立状态卡片。Session 侧栏和当前
会话历史分别按 30 条分页；session 标题与轮数使用持久化摘要，旧数据只在进入对应分页时
惰性回填，Host 启动仅查询最近一个 session。

## MVP 延后项

自动上下文摘要、细粒度命令风险等级、完整崩溃恢复状态机、强沙箱、附件、应用预览、
交互终端和自动 Git commit 留待后续版本。核心实现通过
`ExecutionBackend`、`SnapshotStore`、`CheckpointRepository`、`ArtifactStore` 和
`TraceExporter` 等边界保留替换能力。
