# Coding Agent Web UI 设计

状态：设计草案  
目标版本：Web UI V1  
适用目录：`apps/coding-agent`  
前提：Web UI 与现有 CLI 并存，共享 Agent 内核、会话、Checkpoint、Trace 和工作区状态。

## 1. 文档目标

本文档根据原型图提取页面结构，并结合当前 Coding Agent 实现，定义可落地的
Web UI 产品范围、技术选型、服务端接口、数据一致性和兼容方案。

这里的“并存”包含两层含义：

1. CLI 和 Web 是同一套应用能力的两个入口，不复制 Agent 逻辑。
2. 需要同时打开 CLI 和 Web 时，由一个 Agent Host 独占仓库，两个入口作为客户端接入；
   不允许两个独立进程同时直接修改同一工作区。

## 2. 原型图元素提取

原型是一个桌面优先的三栏 Coding Agent 工作台，整体由顶部上下文栏、左侧导航、
中间任务执行区、右侧工作区检查区和底部状态栏组成。

### 2.1 页面区域

| 区域 | 原型元素 | 对应领域对象 | V1 行为 |
|---|---|---|---|
| 顶部栏 | Coding Agent、项目名、分支、设置 | Workspace、Git branch、配置 | 显示当前 workspace、分支和服务状态 |
| 左侧栏 | 新建任务、项目、最近任务、用户、设置 | Session、Timeline | 创建/切换 session；显示历史任务 |
| 中间主区 | 任务标题、状态、消息、执行步骤、结果摘要 | Turn、Span/Event、Approval | 流式对话、步骤状态、审批与错误展示 |
| 输入区 | 文本输入、Agent 模式、自动模式、附件、发送 | Turn request | V1 支持多行文本和发送；附件、模式切换延后 |
| 右侧上区 | 改动、文件、预览 Tab | Diff、Workspace file | V1 支持改动和只读文件；预览延后 |
| 右侧下区 | 终端、问题 Tab | CommandResult、diagnostic | 显示命令及输出；不提供交互式 shell |
| 右下操作区 | 撤销改动、提交改动 | Restore、Git operation | “撤销”映射为恢复历史轮；Git commit 延后 |
| 底部状态栏 | 分支、本地工作区、变更数 | Git status | 显示分支、工作区和变更统计 |

### 2.2 关键组件

- 项目/工作区导航：当前服务实例只绑定一个启动时确定的 workspace。
- 任务列表：以 session 为任务，以 active timeline 表示当前历史分支。
- 对话消息：用户消息、Agent 消息、错误消息、运行中状态。
- 执行步骤：文件读取、搜索、修改、命令执行、Checkpoint、Snapshot 等事件。
- 审批面板：展示精确 `argv`、`cwd`、超时，支持批准或拒绝。
- 完成摘要：变更文件数、增删行数、测试结果和耗时。
- Diff Viewer：文件列表、增删统计、统一或并排 Diff。
- 文件查看器：只读文本、行号、语法高亮、超限和二进制状态。
- 命令输出：命令、退出码、耗时、stdout/stderr、截断与 Artifact 下载。
- 恢复确认框：说明将 fork 新 timeline，且 ignored 文件和外部副作用不会恢复。

### 2.3 状态设计

页面不能只设计成功状态，至少需要覆盖：

| 状态 | 页面表现 |
|---|---|
| 空会话 | 中间显示输入区，右侧显示当前工作区状态 |
| 运行中 | 输入框禁用或进入排队状态，步骤持续追加，Agent 文本流式出现 |
| 等待审批 | 页面显示高优先级审批面板；断线重连后仍可继续处理 |
| 完成 | 展示结果、测试状态和变更摘要 |
| 失败 | 展示稳定错误码、用户可读说明和可重试动作 |
| 连接中断 | 保留已有内容，自动按事件游标重连 |
| 仓库被占用 | 禁止启动第二个写入者，展示持锁进程信息 |
| 历史恢复后 | 切换到新 timeline，旧 timeline 只读保留 |
| 文件发生外部变化 | 刷新 Diff；Agent patch 继续由 SHA256 乐观锁拦截 |

## 3. 当前实现评估

### 3.1 可直接复用

| 能力 | 当前模块 | Web 复用方式 |
|---|---|---|
| Agent 装配和流式模型输出 | `coding_agent/runtime.py` | 保持 `AgentRuntime` 为唯一运行内核 |
| 联合提交 Checkpoint 与 Git Snapshot | `coding_agent/coordinator.py` | Web API 调用同一个 `TurnCoordinator` |
| Session、Timeline、Turn | `coding_agent/repository.py` | 增加查询接口，不改变现有表语义 |
| 工作区约束 | `coding_agent/workspace/paths.py` | 所有 Web 文件接口继续经过 `PathGuard` |
| Patch 乐观锁与回滚 | `coding_agent/workspace/patch.py` | 不允许前端绕过 PatchService 写文件 |
| Git Snapshot 和 Restore | `coding_agent/workspace/git.py` | 恢复仍使用新 timeline 和新 thread |
| 命令策略与审批 | `coding_agent/execution/`、`runtime.py` | 将同步审批回调桥接为持久化审批请求 |
| 本地 Trace 和 Artifact | `coding_agent/tracing/` | 用于诊断详情，不作为 UI 事件总线 |
| 配置和脱敏 LangSmith | `coding_agent/config.py`、`tracing/exporter.py` | 保持现有配置优先级和隐私边界 |

### 3.2 需要重构

当前 `cli.py` 同时负责依赖装配、输入输出和生命周期管理。Web 接入前应拆出应用层，
否则 CLI 和 API 会形成两套启动与错误处理逻辑。

建议新增：

```text
coding_agent/
├── application/
│   ├── host.py              # 依赖装配、启动、关闭、仓库锁
│   ├── service.py           # session/turn/restore/query 用例
│   ├── events.py            # 稳定的 UI 事件模型和 EventSink
│   ├── operations.py        # 长操作、幂等和状态机
│   └── approvals.py         # 审批请求、等待和决议
├── api/
│   ├── app.py               # FastAPI app factory
│   ├── routes_*.py          # REST 资源
│   ├── schemas.py           # 外部 API schema
│   └── security.py          # 本地访问、Origin、会话令牌
└── cli.py                   # 仅保留终端适配器

web/
├── src/
│   ├── app/
│   ├── components/
│   ├── features/
│   │   ├── sessions/
│   │   ├── conversation/
│   │   ├── approvals/
│   │   ├── changes/
│   │   └── terminal-output/
│   └── api/
├── package.json
└── vite.config.ts
```

`AgentRuntime` 当前只通过 `on_token` 暴露文本增量。为支持步骤 UI，应增加通用
`EventSink`，由 Coordinator、Runtime、审批器和 Workspace 服务显式发布领域事件。
不得通过轮询或解析 Trace 文本推断业务状态；Trace 是观测数据，不是控制面。

## 4. 总体架构

```text
┌─────────────── Browser / CLI attach ───────────────┐
│ React Web UI          Terminal UI                  │
└───────────────┬───────────────┬───────────────────┘
                │ REST          │ SSE
┌───────────────▼───────────────▼───────────────────┐
│ Local Agent Host (FastAPI, 127.0.0.1 only)        │
│ API Adapter / Operation Manager / Event Journal   │
│ Approval Broker / Application Service             │
└───────────────────────┬───────────────────────────┘
                        │
┌───────────────────────▼───────────────────────────┐
│ TurnCoordinator / AgentRuntime                    │
│ Tools / Policy / PathGuard / Snapshot / Trace     │
└─────────────┬──────────────────────┬──────────────┘
              │                      │
┌─────────────▼────────────┐ ┌───────▼──────────────┐
│ agent.db/checkpoints.db  │ │ Git worktree/objects │
│ artifacts/event journal │ │ private timeline refs│
└──────────────────────────┘ └──────────────────────┘
```

### 4.1 单一所有者原则

`RepositoryLock` 当前要求同一 Git 仓库只能有一个 Agent 进程。该约束必须保留。

- `coding-agent web --workspace ...` 启动 Agent Host、API 和静态前端，并持有仓库锁。
- 浏览器只连接该 Host，不直接访问数据库或文件系统。
- 现有 `coding-agent --workspace ...` 保留独立 CLI 模式，启动行为不变。
- 可增加 `coding-agent attach --url ...`，让 CLI 连接已有 Host，从而和 Web 同时使用。
- 独立 CLI 与 Web Host 不能同时持有同一仓库；第二个入口必须 attach 或退出。

同一 workspace 任意时刻只允许一个变更型操作，包括 turn、restore 和未来的 commit。
只读查询可以并发，但结果必须携带 `timeline_id` 和版本信息，避免展示过期状态。

### 4.2 线程模型

当前 `SqliteCheckpointRepository` 使用 SQLite 默认线程约束，不能把连接任意传给
FastAPI worker。V1 采用一个 workspace worker：

1. Host 主线程处理 HTTP 和 SSE。
2. 专用 worker 线程创建并持有 Repository、Runtime、TraceStore 和 SnapshotStore。
3. API 将变更型命令投递到 worker 队列。
4. worker 串行执行并发布事件。
5. 审批回调通过 `ApprovalBroker` 等待对应 REST 决议。

V1 必须使用单个 Uvicorn worker，不能通过 `--workers > 1` 横向扩展。未来如需多
workspace，可使用“一 workspace 一子进程”，而不是让多个 worker 共享运行时对象。

## 5. 技术选型

### 5.1 服务端

选择 **FastAPI + Uvicorn + Pydantic**：

- 与现有 Python/Pydantic 代码一致，模型转换成本低。
- 原生支持 OpenAPI、流式响应、依赖注入和生命周期管理。
- API schema 可生成 TypeScript 类型，降低前后端字段漂移。
- 保持本地单进程部署，不引入独立网关或消息队列。

不选择 Flask：长连接、schema 和生命周期需要额外拼装。  
不选择 Django：当前没有账号、后台管理和复杂 ORM 需求，体量过重。

### 5.2 前端

选择 **React + TypeScript + Vite**：

- 适合对话、事件流、Diff、文件树和多面板状态组合。
- Vite 可生成静态资源，由 FastAPI 同源托管，发布时无需单独 Node 服务。
- TypeScript 可基于 OpenAPI 生成 API 类型。

建议依赖：

| 用途 | 选择 | 原因 |
|---|---|---|
| 服务端状态 | TanStack Query | 查询缓存、失效和错误重试清晰 |
| 局部 UI 状态 | React hooks；必要时 Zustand | 避免把流式内容塞入全局复杂状态 |
| Diff | `@monaco-editor/react` 的 DiffEditor | 行号、语法高亮和并排 Diff 成熟 |
| 虚拟列表 | `@tanstack/react-virtual` | 长对话和长事件列表保持性能 |
| 图标 | Lucide React | 与按钮、文件和状态图标需求匹配 |
| 测试 | Vitest + Testing Library + Playwright | 单元、组件和浏览器链路覆盖 |

不建议 V1 引入 Next.js：页面是本地工具，没有 SSR、SEO 和服务端组件收益。  
不建议前端自行维护消息真相：刷新后必须以服务端 Session、Turn、Operation 为准。

### 5.3 实时协议

选择 **REST 命令 + SSE 事件流**：

- REST 负责创建 turn、审批、恢复等有副作用动作。
- SSE 负责单向推送 token、步骤、审批请求和完成状态。
- SSE 自带事件 ID 和断线重连语义，比仅使用 WebSocket 更适合可重放的执行日志。
- 审批通过独立 REST 请求返回，不需要双向长连接。

事件必须先写入 `operation_events` 再发布。客户端使用 `Last-Event-ID` 重连，服务端从
持久化游标补发，不能只依赖内存队列。

## 6. 页面与交互设计

### 6.1 桌面布局

- 顶部栏高度固定，显示产品名、workspace basename、Git 分支和设置入口。
- 左侧栏建议宽度 `240px`，支持收起；展示新建任务和 session 列表。
- 中间栏使用 `minmax(420px, 1fr)`，承载对话和执行过程。
- 右侧栏使用 `minmax(480px, 44vw)`，承载 Diff、文件和命令输出。
- 输入区固定在中间栏底部，但不覆盖消息；消息区独立滚动。
- 右侧上下区域通过可拖动分隔条调整，默认约为 `60% / 40%`。

### 6.2 响应式布局

| 宽度 | 布局 |
|---|---|
| `>= 1280px` | 完整三栏 |
| `768px - 1279px` | 左栏可收起，右栏作为抽屉或可切换面板 |
| `< 768px` | 单栏；任务、对话、改动通过顶层 Tab 切换 |

移动端仍可完成对话和审批，但不以复杂并排 Diff 为主要体验。所有固定面板应使用
`min-width: 0`、受控滚动和稳定高度，避免长路径、命令或代码撑破布局。

### 6.3 原型到能力的边界修正

- “终端”只显示结构化命令及结果，不暴露 PTY 或任意 shell。
- “撤销改动”不能直接执行 `git checkout`，应选择历史轮并调用现有 restore 语义。
- “提交改动”在当前 MVP 没有对应能力，V1 应隐藏或禁用并明确状态，不伪造完成。
- “预览”可能执行不可信仓库代码，V1 不实现。后续必须依赖沙箱和隔离 Origin。
- “文件”默认只读，且复用 PathGuard 的敏感文件、`.git` 和符号链接限制。
- “项目列表”V1 只表示当前 workspace；不能由浏览器传任意绝对路径打开本机目录。

## 7. API 设计

统一前缀：`/api/v1`。响应错误格式：

```json
{
  "error": {
    "code": "REPOSITORY_LOCKED",
    "message": "Another coding-agent process owns this repository.",
    "request_id": "..."
  }
}
```

### 7.1 查询接口

| Method | Path | 用途 |
|---|---|---|
| GET | `/status` | Host、workspace、branch、active operation 状态 |
| GET | `/sessions` | 当前 workspace 的 session 列表 |
| GET | `/sessions/{session_id}` | session 和 active timeline |
| GET | `/sessions/{session_id}/turns` | 可见历史，包含继承的 turn |
| GET | `/turns/{turn_id}/trace` | Trace 和 Span 摘要 |
| GET | `/workspace/status` | Git 状态和变更统计 |
| GET | `/workspace/diff` | 当前 Diff；支持按 path 过滤 |
| GET | `/workspace/files` | 受限文件树 |
| GET | `/workspace/files/content?path=...` | 受限只读文件内容 |
| GET | `/artifacts/{artifact_id}` | 鉴权后读取脱敏 Artifact |

文件响应应返回 `sha256`、大小、媒体类型和是否截断。Diff 响应应使用结构化文件列表，
并可按需获取单文件 patch，避免一次传输整个大型仓库 Diff。

### 7.2 命令接口

| Method | Path | 语义 |
|---|---|---|
| POST | `/sessions` | 创建 session 和 baseline |
| POST | `/sessions/{session_id}/turns` | 创建异步 turn operation |
| POST | `/sessions/{session_id}/restore` | 创建 restore operation |
| POST | `/approvals/{approval_id}/decision` | 批准或拒绝精确命令 |
| POST | `/operations/{operation_id}/cancel` | 尽力取消尚未进入不可中断阶段的操作 |

创建 turn 示例：

```json
{
  "message": "修复登录表单校验",
  "client_request_id": "019...",
  "expected_timeline_id": "..."
}
```

返回 `202 Accepted`：

```json
{
  "operation_id": "...",
  "status": "queued",
  "events_url": "/api/v1/operations/.../events"
}
```

`client_request_id` 用作提交幂等键。`expected_timeline_id` 防止一个旧浏览器页在 restore
后继续向失效 timeline 写入；不匹配时返回 `409 TIMELINE_CHANGED`。

审批决议必须绑定 `approval_id`、`operation_id` 和请求摘要哈希。重复决议返回首次结果，
不能对后来出现的命令生效。

### 7.3 SSE 事件

```text
id: 42
event: assistant.delta
data: {"operation_id":"...","sequence":42,"text":"正在检查"}
```

V1 事件类型：

- `operation.queued`
- `turn.started`
- `user.message`
- `assistant.delta`
- `assistant.completed`
- `step.started`
- `step.completed`
- `approval.required`
- `approval.resolved`
- `workspace.changed`
- `turn.committed`
- `operation.failed`
- `operation.cancelled`
- `heartbeat`

所有事件包含 `operation_id`、单调递增 `sequence`、时间戳和 schema version。
`assistant.delta` 可以批量合并后落事件，避免逐 token 写 SQLite。`turn.committed` 必须在
Checkpoint、Snapshot 和 Turn metadata 全部成功后发布。

## 8. 数据模型扩展

保留现有 `sessions`、`timelines`、`turns`、`traces`、`spans` 和 `artifacts`，只做增量迁移。

建议新增：

```text
operations
  operation_id, session_id, timeline_id, kind, status,
  client_request_id, started_at, ended_at, error_code

operation_events
  operation_id, sequence, event_type, payload_json, created_at
  PRIMARY KEY (operation_id, sequence)

approvals
  approval_id, operation_id, request_hash, request_json,
  status, decision, created_at, decided_at
```

约束：

- schema migration 只能向前增量执行，旧数据库必须能原地打开。
- Web 数据继续写入 Git 目录下既有 `coding-agent/`，不建立第二套会话库。
- API schema 和内部 Pydantic model 分离，避免未来内部字段变化直接破坏浏览器协议。
- Artifact 仍按 SHA256 内容寻址，并沿用现有权限和脱敏规则。
- Event Journal 是 UI 恢复依据；Trace 是诊断依据；两者职责不能混用。

## 9. 一致性、并发与恢复

### 9.1 写操作串行化

一个 workspace 只运行一个 mutation operation。运行 turn 时：

1. 校验 session、active timeline 和幂等键。
2. 创建 operation，发布 `turn.started`。
3. 调用原有 `TurnCoordinator.run_turn()`。
4. 流式发布 Agent 和步骤事件。
5. 遇到命令时持久化 approval，再等待用户决议。
6. 创建 LangGraph checkpoint 和 Git snapshot。
7. 写入 committed turn。
8. 发布 `turn.committed`，刷新 history、diff 和 status 查询。

restore 运行期间拒绝新 turn。浏览器多标签同时提交时，第一个进入 worker，后续请求返回
`409 WORKSPACE_BUSY`；V1 不自动排队执行用户任务，避免排队期间工作区基线变化。

### 9.2 断线与进程退出

- 浏览器断线不取消正在执行的 Agent。
- 等待审批时断线，审批记录保持 pending；重连后补发 `approval.required`。
- Host 正常退出时停止接收新操作，并等待当前安全点。
- Host 异常退出后，启动时检查 running operation、Trace、Checkpoint 和最后 committed
  snapshot，标记为 `recovery_required`，不得把半完成操作显示为成功。
- Agent 回复只有在 `turn.committed` 后进入正式 history；流式草稿属于 operation。

完整崩溃恢复状态机仍是当前项目延后项，因此 Web V1 上线前至少要实现“识别并阻止误报
成功”。自动续跑可以后续增加。

## 10. CLI 兼容方案

### 10.1 保持不变

- 原命令 `uv run coding-agent --workspace ...` 继续可用。
- `/status`、`/history`、`/trace N`、`/restore N`、`/quit` 语义不变。
- 配置优先级、环境变量和 API key 读取方式不变。
- 现有 Session、Timeline、Turn ID 和 Git private ref 不变。
- Checkpoint 仍使用 `durability="sync"`。
- LangSmith 仍只上传固定脱敏指标。

### 10.2 共享应用层

CLI 不再直接拼装所有依赖，而是调用 `CodingAgentService`：

```text
CLI standalone ─┐
                ├─ CodingAgentService ─ TurnCoordinator ─ AgentRuntime
FastAPI Host ───┘
```

CLI 的 `_InputReader` 和 `_StreamingOutput` 保留为终端适配器。Web 的 SSE adapter 消费相同
领域事件。这样可以用契约测试保证两个入口对于同一输入产生相同的 turn、checkpoint、
snapshot 和错误码。

### 10.3 兼容矩阵

| 场景 | 结果 |
|---|---|
| 仅独立 CLI | 与当前行为一致 |
| 仅 Web Host | Host 持锁，浏览器通过 API 使用 |
| Web Host + attach CLI | 支持，共享同一 operation 和 session 数据 |
| 独立 CLI + Web Host 操作同仓库 | 后启动者收到 `REPOSITORY_LOCKED` |
| 两个浏览器页发起 turn | 一个执行，另一个收到 `WORKSPACE_BUSY` |
| Web 创建 session，之后独立 CLI 恢复 | 支持，数据库 schema 向后兼容 |
| CLI 创建 session，Web 打开 | 支持，Web 读取既有 session |
| restore 后旧页面继续发送 | 返回 `TIMELINE_CHANGED` |

## 11. 安全设计

Web 引入浏览器攻击面，但不能降低当前本地安全约束。

- 默认只监听 `127.0.0.1`，不监听 `0.0.0.0`。
- 前端静态资源与 API 同源托管。
- 校验 `Host` 和 `Origin`，防止恶意网页和 DNS rebinding 访问本地 API。
- 启动时生成高熵会话令牌，使用 `HttpOnly`、`SameSite=Strict` Cookie；所有写请求校验
  CSRF token。
- 设置严格 CSP：默认禁止外部脚本、对象和 frame。
- 浏览器不能提交 workspace 绝对路径；workspace 只能来自 Host 启动参数。
- 文件、Diff 和 Artifact 接口执行路径校验、大小限制和敏感内容保护。
- 命令继续经过 `CommandPolicy` 和人工审批，不提供 shell 字符串接口。
- 前端对模型输出、文件内容、ANSI 输出一律按不可信文本渲染，禁止直接注入 HTML。
- 日志不得记录 API key、Cookie、Authorization、完整源码或未脱敏 Artifact。
- 远程访问、多人共享和公网部署不属于 V1；如后续支持，必须增加 TLS 和真正的身份权限。

## 12. 性能与可用性

- 首屏 JavaScript gzip 后目标小于 `500 KiB`；Monaco 按需加载。
- 对话和事件超过 500 项时启用虚拟列表。
- `assistant.delta` 在前端按 animation frame 合并渲染，避免逐 token 重排。
- 单文件 Diff 默认限制 1 MiB；超限时提供受控 Artifact 查看。
- SSE 心跳建议 15 秒，客户端指数退避重连，最大间隔 10 秒。
- 查询接口使用 ETag 或 `snapshot_oid`；工作区变化后精确失效缓存。
- 页面刷新后 2 秒内恢复 committed history，并从 event cursor 恢复未完成 operation。

## 13. 测试策略

### 13.1 后端

- Application Service 单元测试：不启动 HTTP，不调用真实模型。
- API schema 测试：状态码、错误码、幂等键和 timeline 乐观锁。
- SSE 测试：顺序、断线补发、重复连接、心跳和 terminal event。
- 审批测试：批准、拒绝、重复提交、过期审批和断线重连。
- 并发测试：双 turn、turn/restore 冲突、多浏览器页。
- 数据迁移测试：现有 `agent.db` 原地升级后仍可被 CLI 读取。
- 安全测试：Origin、Host、路径逃逸、符号链接、敏感文件、XSS payload。

### 13.2 前端

- 组件测试：消息、步骤、审批、错误、Diff 和命令输出。
- 状态测试：SSE 重连、事件去重、乱序拒绝、刷新恢复。
- Playwright E2E：
  - 创建 session 并完成流式 turn。
  - 收到命令审批，批准和拒绝后结果正确。
  - 查看 Diff、文件和命令输出。
  - restore 后切换 timeline，旧页面写入被拒绝。
  - 断开 SSE 后按游标补齐事件。
- 在 Chromium、WebKit 和 Firefox 上覆盖桌面与移动视口。
- 截图检查三栏布局、长路径、中文输入、长命令和窄屏不重叠。

现有 Python 测试、Ruff 和 Mypy 必须保持通过；新增前端执行 TypeScript、ESLint、
Vitest 和 Playwright 检查。

## 14. 实施顺序

### Phase 1：应用层解耦

1. 从 `cli.py` 提取 Host 装配和 `CodingAgentService`。
2. 定义稳定领域事件和 EventSink。
3. CLI 改用共享 Service，保证现有测试和交互不变。
4. 增加 operation、event、approval 的增量数据库迁移。

### Phase 2：本地 API

1. 实现单 workspace worker 和生命周期管理。
2. 实现 session、history、turn、approval、restore API。
3. 实现可重放 SSE。
4. 实现受 PathGuard 保护的 Diff、文件和 Artifact 查询。

### Phase 3：Web UI

1. 完成三栏壳层、session 列表、对话和输入。
2. 接入流式事件、步骤和审批。
3. 完成 Diff、文件和命令输出。
4. 完成恢复操作、连接状态和错误状态。

### Phase 4：兼容与发布

1. 增加 `coding-agent web`，保留原 CLI 命令。
2. 增加可选 `coding-agent attach`。
3. 将前端构建产物打包进 Python wheel。
4. 完成旧数据库迁移、双入口契约和跨浏览器 E2E。

## 15. V1 验收标准

1. Web 与 CLI 调用同一个 `TurnCoordinator` 和 `AgentRuntime`，没有复制 Agent loop。
2. 既有 CLI 命令、配置、数据库和 restore 行为保持兼容。
3. Web 能创建/恢复 session、流式执行 turn、处理审批并展示最终结果。
4. Web 能查看当前 Diff、受限文件内容、命令输出和 Trace 摘要。
5. 刷新或 SSE 断线不会丢失已持久化事件，也不会重复提交 turn。
6. 同一 workspace 不会出现两个并发写入者；冲突返回稳定错误码。
7. `turn.committed` 只在 Checkpoint、Snapshot 和 Turn metadata 均成功后出现。
8. Web 不暴露交互式 shell，不绕过 PathGuard、CommandPolicy、审批和脱敏。
9. 默认服务仅本机可访问，并通过 Host、Origin、Cookie 和 CSRF 校验。
10. Python 现有测试及新增 API、前端和 Playwright 测试全部通过。

## 16. 延后项

- 交互式终端和 PTY。
- 执行仓库应用的实时预览。
- 浏览器上传附件和图片。
- 多 Agent、子 Agent 和并行任务。
- 多 workspace 同进程管理。
- 多用户、远程访问和团队协作。
- 自动 Git commit、push 和 Pull Request。
- 崩溃后自动续跑未完成 Agent；V1 只要求准确识别和安全恢复。

