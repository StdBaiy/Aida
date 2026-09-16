# Coding Agent 技术设计

状态：待实现

目标版本：V1

实现位置：`apps/coding-agent`

运行时：Python 3.11+

核心依赖：LangChain、LangGraph、LangGraph SQLite Checkpoint、LangSmith

## 1. 文档目标

本文档定义一个可在本地 Git 仓库中工作的交互式 Coding Agent。实现者应能直接依据本文档创建模块、数据结构、接口、测试和 CLI，无需再补充产品语义。

本文档中的“必须”“不得”“应”分别表示：

- **必须**：V1 验收条件。
- **不得**：安全或数据一致性硬约束。
- **应**：默认实现；只有出现明确技术阻塞时才能调整，并应记录原因。

## 2. 已确认决策

| 决策项 | 结论 |
|---|---|
| 项目形态 | LangChain 仓库中的独立应用 |
| 项目路径 | `apps/coding-agent` |
| 用户入口 | 交互式 CLI |
| 工作目录 | 启动时显式传入 |
| 工作目录要求 | 必须属于 Git 仓库 |
| Git 子目录 | 允许将 Git 仓库子目录指定为工作目录 |
| 文件恢复 | Agent 状态与工作区文件联合恢复 |
| 恢复历史 | 从目标轮 fork 新时间线，旧时间线只读保留 |
| 轮次 checkpoint | 表示该轮执行完成后的状态 |
| 初始脏工作区 | 纳入 `turn 0` 基线快照 |
| ignored 文件 | 不纳入快照，不恢复，不删除 |
| 文件编辑 | 使用结构化 patch 工具 |
| 命令安全 | 风险分级；可恢复的本地操作审批，高危外部副作用拒绝 |
| 模型 | 通过 LangChain `init_chat_model()` 配置 |
| Trace | 本地保存完整 Trace；LangSmith 只上传脱敏指标与引用 |
| 上下文管理 | 到达阈值时自动摘要，完整历史仍保存在本地 |
| 沙箱 | V1 不实现，但执行后端必须可替换 |

## 3. 范围

### 3.1 V1 必须实现

| ID | 功能 |
|---|---|
| F-001 | 通过 CLI 指定工作目录并创建会话 |
| F-002 | 以多轮对话形式接收用户任务 |
| F-003 | 搜索、读取和检查工作目录内的代码 |
| F-004 | 通过结构化 patch 新增、修改、删除文本文件 |
| F-005 | 执行经过策略检查和用户审批的本地命令 |
| F-006 | 流式显示 Agent 回复和工具状态 |
| F-007 | 每轮完成后创建 Agent checkpoint 和 Git 文件快照 |
| F-008 | 从指定历史轮恢复，并 fork 新时间线 |
| F-009 | 上下文接近模型限制时自动压缩 |
| F-010 | 本地记录完整 Trace、命令输出和 diff artifact |
| F-011 | 向 LangSmith 上传不含源码和对话正文的指标 Trace |
| F-012 | 进程崩溃后检测未完成轮次并恢复一致状态 |
| F-013 | 支持查看会话、时间线、轮次、checkpoint 和变更摘要 |
| F-014 | 对路径逃逸、符号链接逃逸和陈旧 patch 进行拦截 |
| F-015 | 对同一 Git 仓库加进程级排他锁 |

### 3.2 V1 不实现

- Docker、Codex Sandbox 或远程沙箱执行。
- 多 Agent、子 Agent 或并行任务。
- IDE、Web UI 或服务端 API。
- 跨机器同步 checkpoint。
- 自动提交到用户分支、自动 push 或创建 Pull Request。
- workspace 外文件恢复。
- 数据库、远程 API、消息队列等外部副作用补偿。
- 语义代码索引和向量检索。
- 多用户权限体系。
- Windows 支持。

### 3.3 V1 安全限制

无沙箱时，下列行为必须默认拒绝，不能仅靠一次确认放行：

- 访问公网或局域网的命令。
- `git push`、远程分支删除和远程仓库配置修改。
- 数据库客户端、云平台 CLI 和基础设施管理 CLI。
- 写入 workspace 外路径。
- `sudo`、`su`、系统服务管理、磁盘管理和用户管理。
- 启动交互式 shell、`bash -c`、`sh -c`、`zsh -c` 等绕过结构化命令检查的形式。
- 从 stdin、文件或编码内容动态执行脚本，例如 `python -c`、`node -e`、`eval`。

上述限制只降低风险，不构成安全隔离。执行任意仓库测试仍可能运行不可信代码，因此每次测试或构建都必须审批。

## 4. 总体架构

```text
┌──────────────────────── CLI ─────────────────────────┐
│ 会话创建 / 对话 / 审批 / history / restore / quit    │
└──────────────────────────┬───────────────────────────┘
                           │
┌────────────────── Turn Coordinator ──────────────────┐
│ 轮次状态机 / 联合 checkpoint / 崩溃恢复 / 仓库锁     │
└──────────────┬────────────┬──────────────┬───────────┘
               │            │              │
       ┌───────▼──────┐ ┌───▼────────┐ ┌──▼───────────┐
       │ Agent Runtime│ │ Workspace  │ │ Trace System │
       │ LangChain    │ │ Git/Patch  │ │ Local/Remote │
       │ LangGraph    │ │ Path Guard │ │ Artifacts    │
       └───────┬──────┘ └────────────┘ └──────────────┘
               │
       ┌───────▼───────────────────────────────────────┐
       │ Tools: list/read/glob/search/patch/diff/run   │
       └───────────────────┬───────────────────────────┘
                           │
                 ┌─────────▼─────────┐
                 │ Execution Backend │
                 │ V1: Host          │
                 │ Future: Sandbox   │
                 └───────────────────┘
```

### 4.1 关键原则

1. LangGraph checkpoint 只负责 Agent 状态，不负责文件系统。
2. Git snapshot 只负责 Git 仓库内容，不负责外部副作用。
3. 用户可恢复 checkpoint 必须同时关联最终 LangGraph checkpoint 和 post-turn Git snapshot。
4. Git 与 SQLite 不具备跨系统原子提交能力，因此必须使用协调状态和崩溃恢复，而不是伪装成 ACID 事务。
5. 模型不得直接访问 Python 文件 API、`subprocess` 或 Git snapshot 内部接口，只能调用注册工具。
6. 完整历史与模型当前上下文分离；上下文压缩不得删除本地审计记录。
7. 工作区边界在工具层和执行策略层同时校验。

## 5. 项目结构

实现应使用以下目录结构：

```text
apps/coding-agent/
├── pyproject.toml
├── README.md
├── TECHNICAL_DESIGN.md
├── coding_agent/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── config.py
│   ├── errors.py
│   ├── ids.py
│   ├── models.py
│   ├── prompts.py
│   ├── runtime.py
│   ├── state.py
│   ├── turn_coordinator.py
│   ├── context/
│   │   ├── __init__.py
│   │   ├── journal.py
│   │   └── summarization.py
│   ├── checkpoint/
│   │   ├── __init__.py
│   │   ├── graph.py
│   │   ├── repository.py
│   │   └── recovery.py
│   ├── workspace/
│   │   ├── __init__.py
│   │   ├── git.py
│   │   ├── lock.py
│   │   ├── paths.py
│   │   └── patch.py
│   ├── execution/
│   │   ├── __init__.py
│   │   ├── backend.py
│   │   ├── host.py
│   │   ├── policy.py
│   │   └── process.py
│   ├── tools/
│   │   ├── __init__.py
│   │   ├── files.py
│   │   ├── patch.py
│   │   └── command.py
│   └── tracing/
│       ├── __init__.py
│       ├── callbacks.py
│       ├── exporter.py
│       ├── artifacts.py
│       └── redaction.py
└── tests/
    ├── conftest.py
    ├── unit/
    ├── integration/
    └── e2e/
```

生产代码必须有类型注解。公共类和函数必须使用 Google 风格 docstring。

## 6. 依赖与启动入口

### 6.1 `pyproject.toml`

包名使用 `langchain-coding-agent`，不发布到 PyPI。最低依赖：

```toml
[project]
name = "langchain-coding-agent"
version = "0.1.0"
requires-python = ">=3.11,<4.0"
dependencies = [
    "langchain",
    "langgraph",
    "langgraph-checkpoint-sqlite",
    "langsmith",
    "pydantic>=2.7,<3",
]

[project.scripts]
coding-agent = "coding_agent.cli:main"

[tool.uv.sources]
langchain = { path = "../../libs/langchain_v1", editable = true }
```

测试和 lint 依赖使用独立 dependency groups，至少包含 `pytest`、`pytest-asyncio`、`pytest-mock`、`ruff` 和 `mypy`。

### 6.2 CLI 启动

```bash
uv run coding-agent \
  --workspace /absolute/path/to/repository/or/subdirectory \
  --model openai:<configured-model-id>
```

模型 ID 不得硬编码在源码或本文档中。默认模型通过 `CODING_AGENT_MODEL` 配置；若 CLI 和环境变量均未提供，启动必须失败并给出明确错误。

配置优先级：

```text
CLI 参数 > CODING_AGENT_* 环境变量 > config.json > 内置默认值
```

配置文件默认路径：

```text
~/.config/langchain-coding-agent/config.json
```

密钥只从模型 SDK 支持的环境变量或系统凭据读取，不写入配置文件和 Trace。

## 7. 标识模型

所有 ID 使用小写 UUIDv7 字符串；若运行时标准库不支持 UUIDv7，则内部实现单调 ULID，但字段名和外部格式保持字符串。

| 字段 | 生命周期 | 含义 |
|---|---|---|
| `session_id` | 一次 CLI 会话 | 用户进入一个工作目录后创建 |
| `timeline_id` | 一条历史分支 | restore 时创建新值 |
| `turn_id` | 一轮用户输入 | 全局唯一 |
| `turn_number` | 时间线内轮次 | `turn 0` 是基线；用户轮从 1 开始 |
| `thread_id` | LangGraph 线程 | 每条 timeline 一个 |
| `checkpoint_id` | LangGraph 物理快照 | 取最终 `StateSnapshot.config` |
| `snapshot_oid` | Git commit OID | 文件系统快照 |
| `trace_id` | 一轮物理执行 | 每次执行或重试生成 |
| `span_id` | Trace 节点 | 每个操作生成 |
| `logical_run_id` | 逻辑任务 | fork 和重试之间用于关联 |
| `attempt` | 执行尝试次数 | 从 1 开始 |

不得把 `trace_id` 当作会话、轮次或幂等键。

## 8. 核心领域模型

`models.py` 必须定义以下枚举和 Pydantic 模型。

### 8.1 枚举

```python
class TurnStatus(StrEnum):
    PREPARING = "preparing"
    RUNNING = "running"
    FINALIZING = "finalizing"
    SNAPSHOT_CREATED = "snapshot_created"
    COMMITTED = "committed"
    RECOVERY_REQUIRED = "recovery_required"


class TurnOutcome(StrEnum):
    BASELINE = "baseline"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TimelineStatus(StrEnum):
    ACTIVE = "active"
    READ_ONLY = "read_only"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"


class ToolOutcome(StrEnum):
    SUCCESS = "success"
    ERROR = "error"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
```

### 8.2 会话与轮次

```python
class SessionRecord(BaseModel):
    session_id: str
    repo_root: str
    workspace_root: str
    workspace_relative_path: str
    model_id: str
    active_timeline_id: str
    created_at: datetime
    updated_at: datetime


class TimelineRecord(BaseModel):
    timeline_id: str
    session_id: str
    thread_id: str
    status: TimelineStatus
    forked_from_timeline_id: str | None
    forked_from_turn_id: str | None
    head_turn_id: str
    created_at: datetime


class TurnRecord(BaseModel):
    turn_id: str
    session_id: str
    timeline_id: str
    turn_number: int
    parent_turn_id: str | None
    logical_run_id: str
    attempt: int
    status: TurnStatus
    outcome: TurnOutcome | None
    task_complete: bool | None
    user_message_artifact_id: str | None
    assistant_message_artifact_id: str | None
    pre_snapshot_oid: str
    post_snapshot_oid: str | None
    checkpoint_id: str | None
    trace_id: str
    error_code: str | None
    created_at: datetime
    committed_at: datetime | None
```

### 8.3 Agent State 与 Runtime Context

```python
class CodingAgentState(AgentState):
    task_summary: NotRequired[str]
    inspected_paths: NotRequired[list[str]]
    changed_paths: NotRequired[list[str]]
    last_test_summary: NotRequired[str]
    last_error: NotRequired[str]


@dataclass(frozen=True)
class CodingAgentContext:
    session_id: str
    timeline_id: str
    turn_id: str
    logical_run_id: str
    trace_id: str
    repo_root: Path
    workspace_root: Path
```

不可变的路径和 ID 必须通过 `context_schema` 注入，不允许模型或 reducer 修改。大段源码、命令输出和 diff 不得写入 `CodingAgentState`。

## 9. 本地存储

### 9.1 存储位置

所有本地运行数据保存在：

```text
<git-dir>/coding-agent/
├── agent.db
├── checkpoints.db
├── artifacts/
│   └── <sha256-prefix>/<sha256>
├── temp/
└── locks/
    └── repository.lock
```

目录权限必须为 `0700`，数据库和 artifact 文件权限必须为 `0600`。不得在工作树中创建 `.coding-agent`，避免污染用户 diff。

### 9.2 数据库

`checkpoints.db` 只由 `SqliteSaver` 管理。`agent.db` 由应用管理，必须开启：

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
```

应用迁移表：

```sql
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    repo_root TEXT NOT NULL,
    workspace_root TEXT NOT NULL,
    workspace_relative_path TEXT NOT NULL,
    model_id TEXT NOT NULL,
    active_timeline_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE timelines (
    timeline_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    thread_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('active', 'read_only')),
    forked_from_timeline_id TEXT,
    forked_from_turn_id TEXT,
    head_turn_id TEXT,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX one_active_timeline_per_session
ON timelines(session_id)
WHERE status = 'active';

CREATE TABLE turns (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    timeline_id TEXT NOT NULL REFERENCES timelines(timeline_id),
    turn_number INTEGER NOT NULL CHECK (turn_number >= 0),
    parent_turn_id TEXT,
    logical_run_id TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    status TEXT NOT NULL CHECK (
        status IN (
            'preparing',
            'running',
            'finalizing',
            'snapshot_created',
            'committed',
            'recovery_required'
        )
    ),
    outcome TEXT CHECK (
        outcome IS NULL OR outcome IN ('baseline', 'completed', 'failed', 'cancelled')
    ),
    task_complete INTEGER CHECK (task_complete IS NULL OR task_complete IN (0, 1)),
    user_message_artifact_id TEXT,
    assistant_message_artifact_id TEXT,
    pre_snapshot_oid TEXT NOT NULL,
    post_snapshot_oid TEXT,
    checkpoint_id TEXT,
    trace_id TEXT NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL,
    committed_at TEXT,
    UNIQUE(timeline_id, turn_number)
);

CREATE TABLE traces (
    trace_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    root_span_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd TEXT,
    langsmith_run_id TEXT,
    langsmith_export_status TEXT NOT NULL DEFAULT 'pending',
    langsmith_export_attempts INTEGER NOT NULL DEFAULT 0,
    langsmith_export_last_error TEXT
);

CREATE TABLE spans (
    span_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL REFERENCES traces(trace_id),
    parent_span_id TEXT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    input_artifact_id TEXT,
    output_artifact_id TEXT,
    attributes_json TEXT NOT NULL,
    error_type TEXT,
    error_message TEXT
);

CREATE INDEX spans_by_trace ON spans(trace_id, started_at);

CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    relative_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE approvals (
    approval_id TEXT PRIMARY KEY,
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    tool_call_id TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    reason TEXT NOT NULL,
    requested_args_artifact_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    decided_args_artifact_id TEXT,
    created_at TEXT NOT NULL,
    decided_at TEXT NOT NULL
);

CREATE TABLE journal_entries (
    entry_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(session_id),
    timeline_id TEXT NOT NULL REFERENCES timelines(timeline_id),
    turn_id TEXT NOT NULL REFERENCES turns(turn_id),
    sequence INTEGER NOT NULL,
    role TEXT NOT NULL,
    content_artifact_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(turn_id, sequence)
);
```

时间统一存储 UTC RFC 3339 字符串。数据库层必须提供事务上下文管理器，禁止业务代码散落 SQL。

## 10. Workspace 初始化

### 10.1 路径解析

初始化顺序：

1. `workspace_arg.expanduser().resolve(strict=True)`。
2. 验证为目录。
3. 执行 `git -C <workspace> rev-parse --show-toplevel` 获取 `repo_root`。
4. 验证 `workspace_root.is_relative_to(repo_root)`。
5. 计算 POSIX 格式的 `workspace_relative_path`；仓库根使用 `"."`。
6. 获取实际 Git 目录：`git -C <repo_root> rev-parse --absolute-git-dir`。
7. 获取仓库排他锁。
8. 检测未完成轮次并进入恢复流程。
9. 新会话创建 `turn 0` 基线；恢复已有会话则校验 repo/workspace 路径一致。

V1 不支持 bare repository、submodule 内外混合 workspace 或多个 worktree 共用同一会话。

### 10.2 仓库锁

`RepositoryLock` 使用 `fcntl.flock(fd, LOCK_EX | LOCK_NB)`。锁文件写入 PID、启动时间、session ID 和 workspace。拿锁失败时启动必须退出，不允许两个 Agent 同时操作同一 Git 仓库。

虽然工具只访问指定子目录，但 Git snapshot 覆盖整个仓库，因此锁粒度必须是 Git 仓库，而不是子目录。

## 11. Git Snapshot

### 11.1 目标

Git snapshot 必须：

- 捕获 tracked 文件的修改、删除、重命名和权限位。
- 捕获未被 `.gitignore` 忽略的 untracked 文件。
- 保留启动时已有的脏状态。
- 不修改用户 HEAD、当前分支和真实 index。
- 不执行 hooks。
- 不把 ignored untracked 文件纳入快照。
- 能以指定 workspace 子目录为边界恢复。

### 11.2 创建快照

`GitSnapshotStore.create_snapshot()` 必须使用临时 index：

```python
class GitSnapshotStore(Protocol):
    def create_snapshot(
        self,
        *,
        session_id: str,
        timeline_id: str,
        turn_id: str,
        parent_oid: str | None,
        reason: str,
    ) -> str: ...

    def restore_workspace(self, *, target_oid: str, safety_oid: str) -> None: ...

    def diff(self, *, from_oid: str, to_oid: str) -> WorkspaceDiff: ...
```

等价 Git 算法：

```text
temp_index = <git-dir>/coding-agent/temp/index-<uuid>
GIT_INDEX_FILE=temp_index git read-tree HEAD
GIT_INDEX_FILE=temp_index git add -A -- .
tree_oid = GIT_INDEX_FILE=temp_index git write-tree
commit_oid = git commit-tree <tree_oid> [-p <parent_oid>]
git update-ref refs/coding-agent/timelines/<session_id>/<timeline_id> <commit_oid>
```

若仓库为 unborn branch，第一步改为 `git read-tree --empty`。

`git add` 必须从 `repo_root` 执行，因此 commit 表示整个仓库。它不会加入 ignored untracked 文件，但会保留已经 tracked 后又被 ignore 的文件。`commit-tree` 必须显式设置应用自己的 author/committer identity，并通过 stdin 写入只含 ID 和 reason 的提交消息，不调用用户 hooks。

临时 index 必须在 `finally` 中删除。任何 Git 命令失败都不得更新 timeline ref。

### 11.3 Ref 规则

```text
refs/coding-agent/timelines/<session_id>/<timeline_id>
refs/coding-agent/recovery/<session_id>/<recovery_id>
```

每条 timeline 的快照 commit 形成 parent 链。fork 时新 timeline ref 初始指向来源 turn 的 `post_snapshot_oid`；第一次新快照以该 OID 为 parent。

### 11.4 恢复算法

恢复前必须创建 recovery safety snapshot，并更新 recovery ref。恢复只影响 `workspace_root`，不得影响仓库其他子目录和真实 index。

算法：

1. 创建当前仓库 safety snapshot。
2. 分别读取 safety 和 target 在 workspace 下的 manifest：
   `git ls-tree -r -z <oid> -- <workspace_path>`。
3. 对 safety 中存在、target 中不存在的路径执行受控删除。
4. 自底向上删除步骤 3 产生的空目录，处理 `a/b.py -> a` 这类目录变文件的情况。
5. 创建临时 index 并执行 `GIT_INDEX_FILE=<temp> git read-tree <target_oid>`。
6. 从 target manifest 提取 NUL 分隔的文件路径，传给
   `GIT_INDEX_FILE=<temp> git checkout-index --force -z --stdin`。不得依赖目录
   pathspec，也不得使用 `--all`。
7. 对符号链接、普通文件和可执行位逐项校验。
8. 重新创建 workspace snapshot，验证 tree 中 workspace 子树 OID 等于目标子树 OID。
9. 失败时使用同一算法恢复 safety snapshot，并将操作标记为 `RECOVERY_REQUIRED`。

删除路径前必须经过 `PathGuard`。不得执行 `git reset --hard`、`git checkout -- .` 或修改真实 index。ignored untracked 文件不在 manifest 中，因此必须保持原样。

V1 对包含 Git LFS pointer 的文件按普通 Git blob 恢复，不主动下载 LFS 对象。

## 12. PathGuard

所有文件工具共享一个 `PathGuard` 实例：

```python
class PathGuard:
    def resolve_for_read(self, relative_path: str) -> Path: ...
    def resolve_for_write(self, relative_path: str) -> Path: ...
    def validate_cwd(self, relative_path: str | None) -> Path: ...
```

规则：

1. 工具参数只接受相对路径。
2. 拒绝空字节、绝对路径和 `..` 路径段。
3. 已存在路径调用 `resolve()` 后必须仍在 workspace 内。
4. 新文件从最近的已存在父目录开始解析，父目录必须在 workspace 内。
5. 若目标或任一父级符号链接指向 workspace 外，拒绝操作。
6. `.git` 目录和实际 git-dir 永远禁止读写。
7. 默认拒绝读取常见密钥文件：`.env*`、私钥、credential 文件；可通过显式配置增加规则，不允许降低内置规则。
8. 默认最大读取文件为 1 MiB；超过限制返回元数据和错误，不截断后冒充完整内容。

## 13. 文件工具

所有工具返回结构化 JSON 可序列化对象。工具异常转换成带稳定 `error_code` 的 `ToolMessage`，不得把 Python traceback 发给模型。

### 13.1 `list_files`

```python
class ListFilesInput(BaseModel):
    path: str = "."
    depth: int = Field(default=2, ge=1, le=6)
    limit: int = Field(default=500, ge=1, le=2000)
```

返回相对路径、类型和大小；默认跳过 `.git`、ignored 文件和配置中的排除目录。达到 limit 时必须设置 `truncated=true`。

### 13.2 `read_file`

```python
class ReadFileInput(BaseModel):
    path: str
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)
```

要求：

- 只支持 UTF-8 文本；解码失败返回 `BINARY_OR_NON_UTF8_FILE`。
- `end_line - start_line + 1` 最大 500。
- 返回行号、内容、文件总行数和内容 `sha256`。
- `sha256` 基于读取时完整文件字节，用于 patch 乐观并发控制。

### 13.3 `glob_files`

```python
class GlobFilesInput(BaseModel):
    pattern: str
    limit: int = Field(default=500, ge=1, le=2000)
```

优先使用 `rg --files -g <pattern>`，不可用时使用 Python fallback。结果只能包含 workspace 内路径。

### 13.4 `search_code`

```python
class SearchCodeInput(BaseModel):
    query: str
    path: str = "."
    include: str | None = None
    regex: bool = False
    max_results: int = Field(default=200, ge=1, le=1000)
```

优先使用 ripgrep。返回路径、行号和单行匹配上下文；结果必须进行总字节限制。

### 13.5 `apply_patch`

```python
class AddFileOperation(BaseModel):
    op: Literal["add"]
    path: str
    content: str


class UpdateFileOperation(BaseModel):
    op: Literal["update"]
    path: str
    expected_sha256: str
    replacements: list[Replacement]


class Replacement(BaseModel):
    old_text: str
    new_text: str
    expected_occurrences: int = Field(default=1, ge=1)


class DeleteFileOperation(BaseModel):
    op: Literal["delete"]
    path: str
    expected_sha256: str


class ApplyPatchInput(BaseModel):
    operations: list[
        Annotated[
            AddFileOperation | UpdateFileOperation | DeleteFileOperation,
            Field(discriminator="op"),
        ]
    ] = Field(min_length=1, max_length=50)
```

执行规则：

1. 先验证所有路径、hash、存在性和 replacement 次数。
2. 在内存中计算全部结果。
3. 任一 operation 校验失败时整个 patch 不写入任何文件。
4. 写入使用同目录临时文件、`fsync` 和 `os.replace`。
5. 保留已有文件 mode；新增文件 mode 为 `0644`。
6. delete 只删除普通文件或 workspace 内符号链接，不递归删除目录。
7. 单次 patch 总输入和输出各不得超过 2 MiB。
8. 成功后返回每个文件的新 hash 和 unified diff artifact ID。

跨多个文件无法获得真正文件系统原子性。实现必须先为受影响文件创建内存或临时备份；写入中途失败时逐个恢复，并在恢复失败时将 turn 标记为 `RECOVERY_REQUIRED`。

### 13.6 `show_diff` 与 `workspace_status`

`show_diff` 对比当前工作树快照与本轮 `pre_snapshot_oid`。输出正文受限于 200 KiB，完整 diff 写 artifact。

`workspace_status` 返回新增、修改、删除、重命名文件以及 ignored 文件数量，不返回 ignored 文件名。

## 14. 命令执行

### 14.1 后端接口

```python
class ExecutionBackend(Protocol):
    def run(self, request: CommandRequest) -> CommandResult: ...


class CommandRequest(BaseModel):
    argv: list[str] = Field(min_length=1, max_length=64)
    cwd: str = "."
    timeout_seconds: int = Field(default=120, ge=1, le=1800)
    env: dict[str, str] = Field(default_factory=dict)


class CommandResult(BaseModel):
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int
    output_artifact_id: str
```

V1 实现 `HostExecutionBackend`。未来的 `DockerExecutionBackend`、`CodexSandboxExecutionBackend` 必须遵循同一接口。

### 14.2 进程约束

- 使用 `subprocess.Popen(argv, shell=False)`。
- `cwd` 必须通过 `PathGuard.validate_cwd()`。
- 启动独立 process group。
- 超时时先发送 `SIGTERM`，等待 3 秒，再发送 `SIGKILL`。
- stdout/stderr 并发读取，避免 pipe deadlock。
- 返回模型的 stdout 和 stderr 各最多 64 KiB、2000 行。
- 完整输出写本地 artifact，但环境变量值永不写入 Trace。
- 只继承最小环境变量集合：`PATH`、`HOME`、`TMPDIR`、locale 和模型运行不需要的安全系统变量。
- 工具传入的 env key 必须匹配 `[A-Z_][A-Z0-9_]*`，值不得覆盖密钥黑名单。

### 14.3 风险策略

```python
class CommandPolicy(Protocol):
    def assess(self, request: CommandRequest) -> PolicyDecision: ...


class PolicyDecision(BaseModel):
    allowed: bool
    approval_required: bool
    risk_level: RiskLevel
    reason_code: str
    explanation: str
```

判定顺序：

1. 命中硬拒绝规则：`allowed=false`。
2. 命中精确只读 allowlist：LOW，自动执行。
3. 命中本地测试、构建、lint allowlist：MEDIUM，要求审批。
4. 命中本地可恢复写操作：HIGH，要求审批。
5. 未知命令：HIGH，默认拒绝；不能用审批绕过。

LOW allowlist 只包含无扩展、无脚本执行能力的精确形式，例如：

- `git status --short`
- `git diff` 的受控参数子集
- `git log` 的受控参数子集
- `rg`、`find`、`ls`、`pwd` 的受控参数子集

MEDIUM 包含已识别的测试和静态检查入口，例如 `pytest`、`ruff`、`mypy` 和仓库内 `make` target。包管理、安装依赖和任意脚本不属于 MEDIUM。

参数中出现 shell 元字符不会被 shell 解释，但仍应拒绝控制字符。可执行文件必须解析为受信 PATH 中的真实路径，不允许 workspace 内同名程序冒充系统命令，除非它是经审批的明确项目入口。

### 14.4 审批

命令工具在需要审批时通过 `HumanInTheLoopMiddleware` 中断。CLI 必须展示：

- 完整 argv 的 shell-escaped 展示形式。
- cwd。
- 风险等级和理由。
- timeout。
- 当前 workspace diff 摘要。

用户可选择 `approve`、`edit` 或 `reject`。编辑后必须重新进行策略评估，不能继承原审批结果。审批只对单个 `tool_call_id` 有效。

## 15. Agent 装配

### 15.1 模型初始化

```python
model = init_chat_model(config.model_id)
```

启动时应验证模型支持 tool calling。模型实例通过构造参数注入，单元测试必须使用 fake chat model，不得访问网络。

### 15.2 Middleware 顺序

`create_agent()` 中 middleware 按以下顺序注册，靠前者为外层：

```python
middleware = [
    LocalTraceMiddleware(...),
    ModelRetryMiddleware(max_retries=2),
    ToolErrorMiddleware(...),
    ToolCallLimitMiddleware(...),
    ContextBudgetMiddleware(...),
    SummarizationMiddleware(...),
    CommandPolicyMiddleware(...),
    HumanInTheLoopMiddleware(...),
]
```

要求：

- Trace 必须覆盖重试、摘要、审批和实际工具执行。
- 模型重试只处理明确的瞬时错误，最多 2 次，指数退避。
- patch 冲突、策略拒绝和命令失败不得自动重试。
- 每轮工具调用默认最多 50 次；模型调用默认最多 20 次。
- 达到限制时 Agent 必须停止并向用户说明，不得继续循环。

### 15.3 `create_agent`

```python
agent = create_agent(
    model=model,
    tools=[
        list_files,
        read_file,
        glob_files,
        search_code,
        apply_patch,
        show_diff,
        workspace_status,
        run_command,
    ],
    system_prompt=CODING_AGENT_SYSTEM_PROMPT,
    middleware=middleware,
    state_schema=CodingAgentState,
    context_schema=CodingAgentContext,
    checkpointer=sqlite_saver,
    name="coding_agent",
)
```

`SqliteSaver` 使用 `sqlite3.connect(path, check_same_thread=False)`。V1 CLI 使用同步执行接口，避免同步和异步 saver 混用。

### 15.4 System Prompt 必须包含

- Agent 只能操作 workspace 内文件。
- 修改前必须读取目标文件并使用返回 hash。
- 优先使用搜索和读取工具理解现有实现。
- 所有文件修改必须使用 `apply_patch`。
- 不得通过命令工具编辑文件。
- 修改后应运行最小且相关的测试；命令需审批时等待用户。
- 不得声称测试通过，除非工具结果 exit code 为 0。
- 工具失败时分析具体错误，不得无条件重复调用。
- 不得尝试规避命令策略或访问 `.git`。
- 最终回复必须列出变更、验证结果和未完成风险。

## 16. Turn 状态机

```text
PREPARING
    │ 创建 pre-turn snapshot、TurnRecord、root span
    ▼
RUNNING
    │ 调用 LangGraph Agent
    │ 正常返回、业务失败、用户取消或可处理异常
    ▼
FINALIZING
    │ 固化最终 Agent 状态与 checkpoint_id
    ▼
SNAPSHOT_CREATED
    │ 创建 post-turn snapshot
    ▼
COMMITTED

任一持久化或文件回滚步骤无法确认结果
    └───────────────────────────────► RECOVERY_REQUIRED
```

`status` 只表示持久化生命周期，`outcome` 表示本轮任务结果。任务失败不等于
checkpoint 失败。

### 16.1 开始一轮

`TurnCoordinator.start_turn(user_text)`：

1. 验证 active timeline。
2. 验证无未完成 turn。
3. 创建 `turn_id`、`trace_id`、`logical_run_id`。
4. 创建 pre-turn snapshot，其 parent 为 timeline head snapshot。
5. 将用户消息写 artifact 和 journal。
6. 在一个 `agent.db` 事务内插入 `PREPARING` TurnRecord 和 Trace。
7. 更新 turn 为 `RUNNING`。
8. 调用 Agent：

```python
config = {
    "configurable": {"thread_id": timeline.thread_id},
    "metadata": {
        "session_id": session_id,
        "timeline_id": timeline_id,
        "turn_id": turn_id,
        "trace_id": trace_id,
        "logical_run_id": logical_run_id,
    },
    "tags": ["coding-agent", "local-cli"],
}

agent.invoke(
    {"messages": [{"role": "user", "content": user_text}]},
    config=config,
    context=runtime_context,
    durability="sync",
)
```

CLI 实现可将 `invoke` 替换成具有相同 `config`、`context` 和
`durability="sync"` 参数的 `stream` 调用，以显示流式事件。无论使用哪种入口，ID、
metadata、context 和同步持久化均不得省略。

### 16.2 完成一轮

Agent 正常结束后：

1. 调用 `agent.get_state({"configurable": {"thread_id": thread_id}})`。
2. 从 `state.config["configurable"]["checkpoint_id"]` 获取最终 checkpoint ID。
3. 写 assistant message 和最终状态摘要到 journal/artifact。
4. 状态改为 `FINALIZING`，`outcome=COMPLETED`。
5. 创建 post-turn snapshot，parent 为 pre-turn snapshot。
6. 状态改为 `SNAPSHOT_CREATED`。
7. 在单个 `agent.db` 事务内：
   - 写入 `post_snapshot_oid` 和 `checkpoint_id`。
   - 将 turn 标记为 `COMMITTED`。
   - 更新 timeline `head_turn_id`。
   - 更新 session `updated_at`。
8. 完成 Trace，异步尝试上传 LangSmith 指标。

只有 `COMMITTED` turn 才能显示在普通 `/history` 和作为 `/restore` 目标。

### 16.3 失败

- 模型错误、工具错误或命令失败后，协调器必须将稳定错误摘要写入
  `CodingAgentState.last_error`，通过 `agent.update_state()` 生成终态 checkpoint，
  创建当前文件状态的 post snapshot，并以 `outcome=FAILED` 提交本轮。
- 用户中断 `Ctrl-C` 后必须终止活动子进程，再用相同流程提交
  `outcome=CANCELLED` 的 checkpoint 和 snapshot。
- 用户拒绝单个工具不是系统失败；Agent 可以继续推理。若用户取消整轮，则 outcome
  为 `CANCELLED`。
- 只有无法确认 checkpoint、snapshot 或回滚结果时才标记 `RECOVERY_REQUIRED`。
- 即使 outcome 为 `FAILED` 或 `CANCELLED`，提交成功的 turn 仍是可恢复 checkpoint，
  用户可以在下一轮继续或恢复到它。

## 17. Checkpoint 与恢复

### 17.1 用户 checkpoint

一轮执行期间 LangGraph 可以生成多个内部 checkpoint。V1 只把该轮正常结束后的最终 checkpoint 暴露为“第 N 轮 checkpoint”。

逻辑约束：

```text
restorable(turn) ==
    turn.status == COMMITTED
    and turn.checkpoint_id is not None
    and turn.post_snapshot_oid is not None
```

### 17.2 `turn 0`

新会话初始化时：

1. 创建 timeline 和 thread。
2. 创建当前工作树 snapshot，作为 baseline。
3. 创建空 Agent 初始状态 checkpoint。
4. 插入 `turn_number=0`、`status=COMMITTED`、`outcome=BASELINE` 的 TurnRecord；
   `pre_snapshot_oid` 与 `post_snapshot_oid` 都等于 baseline OID。
5. `turn 0` 没有用户或 assistant 消息。

初始状态 checkpoint 可以通过对新 thread 调用 `agent.update_state(config, {"messages": []})` 创建。返回 config 中的 checkpoint ID 必须持久化。

### 17.3 恢复命令

```text
/restore <turn-number>
```

恢复前 CLI 必须展示：

- 来源 timeline 和 turn。
- 当前 turn 与目标 turn 的文件变更摘要。
- ignored 文件不会恢复的提示。
- 外部副作用不会恢复的提示。
- 将创建新 timeline，而不是删除当前历史。

用户明确确认后执行：

1. 验证目标 turn 为 `COMMITTED`。
2. 停止并确认无活动子进程。
3. 创建 recovery safety snapshot。
4. 将当前 timeline 标记为 `READ_ONLY`。
5. 创建新 `timeline_id` 和新 `thread_id`。
6. 读取来源 Agent 状态：

```python
source_config = {
    "configurable": {
        "thread_id": source_thread_id,
        "checkpoint_id": source_checkpoint_id,
    }
}
source_state = agent.get_state(source_config)
```

7. 向新 thread 写入来源状态：

```python
new_config = {"configurable": {"thread_id": new_thread_id}}
fork_config = agent.update_state(new_config, source_state.values)
```

8. 保存 `fork_config["configurable"]["checkpoint_id"]` 作为新 timeline 的 `turn 0` checkpoint。
9. 恢复来源 `post_snapshot_oid` 对应的 workspace。
10. 新 timeline ref 指向来源 snapshot。
11. 创建新 timeline 的 `turn 0`，其 `parent_turn_id` 指向来源 turn，并记录 `forked_from_*`。
12. 切换 session 的 active timeline。
13. 原 timeline 及其 turn 保持只读。

步骤 4 到 12 由恢复协调器记录中间状态。任一步失败时必须恢复 safety snapshot，并保持原 timeline active。若自动回滚失败，禁止继续运行 Agent并输出人工恢复指令。

### 17.4 为什么使用新 thread

LangGraph 在相同 `thread_id + checkpoint_id` 上继续执行会在同一 checkpoint 历史中形成框架级 fork。V1 仍选择新 `thread_id`，原因是：

- timeline 与 thread 一一对应，更容易查询和删除。
- 避免不同用户可见分支共享同一 checkpoint 命名空间。
- LangSmith 和本地 Trace 能直接按 timeline 过滤。
- 恢复语义不依赖内部 checkpoint parent 链。

### 17.5 启动时崩溃恢复

启动时查询所有非终态 turn：

| 状态 | 动作 |
|---|---|
| `PREPARING` | 补建 cancelled checkpoint 和当前 snapshot，提交为 `CANCELLED` |
| `RUNNING` | 比较当前 snapshot 与 pre snapshot |
| `FINALIZING` | 若 checkpoint 存在，尝试创建 post snapshot 并提交 |
| `SNAPSHOT_CREATED` | 校验两个 ID 后完成数据库提交 |
| `RECOVERY_REQUIRED` | 禁止进入对话，要求选择恢复 pre snapshot 或保留现场 |

`RUNNING` 状态下若能读取最新 LangGraph state，则追加稳定的
`PROCESS_INTERRUPTED` 错误状态并生成新 checkpoint；否则以本地 journal 重建最小
Agent state 后生成 checkpoint。随后保存当前 workspace snapshot，以
`outcome=CANCELLED` 提交。这样每个已接收的用户轮最终都有可恢复 checkpoint。

自动恢复不得覆盖未知的并发用户修改。由于运行期间持有 repo lock，正常情况下不存在另一个 Agent 修改；若当前 tree 与记录状态不匹配，必须要求用户确认。

## 18. 上下文管理

### 18.1 双层历史

完整历史写入 `journal_entries + artifacts`，模型上下文保存在 LangGraph `messages` channel。摘要只改变模型上下文，不删除 journal。

### 18.2 压缩触发

使用 `SummarizationMiddleware`，默认：

```python
SummarizationMiddleware(
    model=summary_model,
    trigger=[("fraction", 0.72), ("messages", 80)],
    keep=("fraction", 0.30),
    summary_prompt=CODING_SUMMARY_PROMPT,
)
```

若模型 profile 没有可靠的 context window，使用配置项 `context_window_tokens`；两者都没有时启动失败，不允许猜测窗口大小。

在模型调用前，`ContextBudgetMiddleware` 还必须预留：

- 最大输出 token。
- 工具 schema token。
- 10% 安全余量。

若即使摘要后仍超限，停止本轮并返回 `CONTEXT_LIMIT_EXCEEDED`，不得截断最新用户消息或安全提示。

### 18.3 摘要内容

摘要 prompt 必须要求保留：

- 用户当前目标和明确约束。
- 已做出的技术决策。
- 已检查的文件、符号及其用途。
- 已应用的变更和文件路径。
- 测试命令及真实结果。
- 当前错误、未完成项和下一步。
- 重要 artifact ID、snapshot OID 和 turn ID。

摘要不得嵌入：

- 大段源码。
- 完整命令输出。
- 完整 diff。
- API key、token 或环境变量值。

每次摘要调用必须创建 `context.compression` span，记录压缩前后 token、消息数和摘要 artifact ID。

### 18.4 恢复后的上下文

恢复必须使用来源 checkpoint 中已经存在的摘要和消息。不得用当前 timeline 的摘要覆盖来源状态。完整 journal 在新 timeline 中不复制物理 artifact，只通过来源 turn 关系查询。

## 19. Trace 与评估

### 19.1 Trace 层级

```text
turn
├── agent.invoke
│   ├── model.call
│   ├── tool.call
│   │   ├── policy.assess
│   │   ├── approval.wait
│   │   └── tool.execute
│   ├── context.compression
│   └── model.retry
├── checkpoint.read
├── workspace.snapshot
└── langsmith.export
```

### 19.2 本地完整 Trace

实现 `LocalTraceCallbackHandler(BaseCallbackHandler)` 捕获 LangChain model、tool、chain 事件。应用级操作通过显式 `TraceRecorder.span()` 上下文管理器记录。

本地允许记录：

- 用户和 assistant 完整消息。
- 实际发送给模型的消息。
- 模型输出、tool call 和 usage metadata。
- 文件读取内容、patch、diff。
- 命令 argv、stdout、stderr 和 exit code。
- 审批请求与决策。
- 异常和 traceback。

以下内容即使在本地也不得记录：

- API key、Authorization header。
- 完整继承环境变量。
- 明确匹配 secret redaction 规则的值。

大于 16 KiB 的 payload 必须写 artifact，Span 只保存 artifact ID。artifact 采用内容寻址和原子写入。

Trace 失败必须 fail-open：记录警告但不得使 Agent 主流程失败。审计审批记录和 TurnRecord 不属于可丢弃 Trace，写入失败时必须 fail-closed。

### 19.3 LangSmith 脱敏导出

不得通过 `LANGSMITH_TRACING=true` 或全局 LangChain tracer直接上传 Agent 执行，因为这会包含消息和源码。

`MetricsLangSmithExporter` 在 turn 结束后创建一个独立、合成的 LangSmith run，只上传：

```python
{
    "session_id_hash": "...",
    "timeline_id_hash": "...",
    "turn_id": "...",
    "trace_id": "...",
    "logical_run_id": "...",
    "status": "...",
    "model_id": "...",
    "input_tokens": 0,
    "output_tokens": 0,
    "model_call_count": 0,
    "tool_call_count": 0,
    "command_count": 0,
    "command_failure_count": 0,
    "approval_count": 0,
    "changed_file_count": 0,
    "added_lines": 0,
    "deleted_lines": 0,
    "context_compression_count": 0,
    "duration_ms": 0,
    "error_code": None,
    "local_artifact_manifest_hash": "...",
}
```

`inputs` 和 `outputs` 只允许固定 schema 的数值、布尔值、枚举和不可逆 hash。禁止路径、源码、消息、命令文本、diff 和异常正文。

导出失败只更新本地 Trace，不影响 turn 的 `COMMITTED` 状态。后续启动可重试尚未导出的指标。

### 19.4 评估指标

V1 必须能从本地数据库计算：

- turn 成功率和失败类型分布。
- 每轮模型调用、工具调用、Token、耗时和费用。
- patch 成功率、冲突率和变更文件数。
- 命令成功率、超时率和用户拒绝率。
- 首次测试成功率。
- 上下文压缩次数及压缩后错误率。
- restore/fork 次数及恢复后首轮成功率。
- Agent 声称完成但无成功验证命令的比例。

最后一项通过结构化最终响应字段计算，而不是分析自然语言。

## 20. 最终响应

Agent 最终输出使用内部结构化模型，CLI 再渲染为文本：

```python
class FinalResponse(BaseModel):
    summary: str
    changed_files: list[str]
    verification: list[VerificationResult]
    unresolved_risks: list[str]
    task_complete: bool


class VerificationResult(BaseModel):
    command_display: str
    exit_code: int | None
    status: Literal["passed", "failed", "not_run"]
```

`task_complete=true` 不要求一定运行测试，但若代码已改变且没有成功验证命令，`unresolved_risks` 必须明确说明。CLI 不得把审批拒绝显示为测试通过。

## 21. CLI 规格

### 21.1 启动参数

```text
coding-agent
  --workspace PATH                 必填
  --model PROVIDER:MODEL           可选
  --session SESSION_ID             恢复已有会话
  --config PATH                    配置文件
  --no-langsmith                   禁止指标上传
  --context-window-tokens INT      profile 缺失时使用
```

### 21.2 交互命令

| 命令 | 行为 |
|---|---|
| `/help` | 显示命令 |
| `/status` | 显示 session、timeline、turn、snapshot 和 workspace 状态 |
| `/history [limit]` | 显示 active timeline 的 committed turns |
| `/timelines` | 显示时间线及 fork 来源 |
| `/diff [turn]` | 显示指定 turn 相对其 parent 的 diff |
| `/restore <turn>` | 确认后 fork 并恢复 |
| `/trace <turn>` | 显示本地 Trace 摘要和 artifact 路径 |
| `/cancel` | 取消正在等待的审批或活动命令 |
| `/quit` | 安全退出 |

普通非 `/` 输入作为用户消息。未知 slash command 不发送给模型。

### 21.3 输出要求

- 模型文本支持流式输出。
- 工具开始、结束、耗时和结果状态可见，但不输出内部思维过程。
- patch 应显示文件列表和 diff 摘要。
- 命令必须分别标识 stdout、stderr 和 exit code。
- 被截断输出必须明确提示完整 artifact 路径。
- 所有错误必须带稳定 error code。

## 22. 错误模型

定义基类：

```python
class CodingAgentError(Exception):
    code: str
    user_message: str
    retryable: bool
```

至少实现：

| Error Code | 场景 |
|---|---|
| `INVALID_WORKSPACE` | 路径不存在或不是目录 |
| `NOT_A_GIT_REPOSITORY` | 不属于 Git 仓库 |
| `REPOSITORY_LOCKED` | 另一个进程持锁 |
| `PATH_OUTSIDE_WORKSPACE` | 路径逃逸 |
| `PROTECTED_PATH` | 访问 `.git` 或密钥文件 |
| `FILE_TOO_LARGE` | 超过读取或 patch 限制 |
| `BINARY_OR_NON_UTF8_FILE` | 不支持的文件 |
| `STALE_FILE` | hash 不一致 |
| `PATCH_CONFLICT` | replacement 次数不符 |
| `COMMAND_DENIED` | 策略拒绝 |
| `COMMAND_REJECTED` | 用户拒绝 |
| `COMMAND_TIMEOUT` | 命令超时 |
| `CONTEXT_LIMIT_EXCEEDED` | 摘要后仍超限 |
| `CHECKPOINT_NOT_FOUND` | checkpoint 缺失 |
| `SNAPSHOT_NOT_FOUND` | Git 对象缺失 |
| `TURN_NOT_RESTORABLE` | turn 未提交 |
| `RECOVERY_REQUIRED` | 联合状态不一致 |
| `MODEL_CONFIGURATION_ERROR` | 模型缺失或不支持工具 |

用户输出不得包含凭据、完整 traceback 或数据库内部结构。完整 traceback只写脱敏后的本地 artifact。

## 23. 配置 Schema

`config.py` 使用 Pydantic：

```python
class AgentConfig(BaseModel):
    model_id: str
    summary_model_id: str | None = None
    context_window_tokens: int | None = Field(default=None, ge=4096)
    max_output_tokens: int = Field(default=4096, ge=256)
    context_trigger_fraction: float = Field(default=0.72, gt=0.0, lt=1.0)
    context_keep_fraction: float = Field(default=0.30, gt=0.0, lt=1.0)
    max_model_calls_per_turn: int = Field(default=20, ge=1, le=100)
    max_tool_calls_per_turn: int = Field(default=50, ge=1, le=200)
    default_command_timeout_seconds: int = Field(default=120, ge=1, le=1800)
    max_command_output_bytes: int = Field(default=65536, ge=1024)
    langsmith_enabled: bool = True
    langsmith_project: str = "coding-agent-evaluation"
```

配置校验必须保证 trigger fraction 大于 keep fraction，并为所有不合法值给出字段级错误。

## 24. 可扩展接口

以下接口必须在 V1 中定义并使用 Protocol 或 ABC，不能直接把 host 实现写死在工具内：

```python
class ExecutionBackend(Protocol): ...
class CommandPolicy(Protocol): ...
class GitSnapshotStore(Protocol): ...
class CheckpointRepository(Protocol): ...
class TraceExporter(Protocol): ...
class ArtifactStore(Protocol): ...
class ApprovalProvider(Protocol): ...
```

未来扩展方向：

- `DockerExecutionBackend` 和 `CodexSandboxExecutionBackend`。
- Postgres checkpoint 和多进程服务。
- OpenTelemetry exporter。
- MCP 工具。
- 子 Agent 和任务委派。
- AST/LSP 级结构化编辑。
- 代码语义索引和 RAG。
- 用户级预算、组织策略和多方审批。
- 远程 workspace 与临时 Git worktree。
- 外部副作用幂等键、补偿任务和审计工作流。

## 25. 测试策略

所有单元测试必须断网。测试不能依赖真实模型 API 或 LangSmith。

### 25.1 单元测试

**PathGuard**

- 拒绝绝对路径和 `..`。
- 拒绝 workspace 内指向外部的符号链接。
- 允许普通 workspace 子路径。
- 拒绝 `.git` 和密钥文件。

**Patch**

- 单文件、多文件 add/update/delete 成功。
- hash 不一致不写文件。
- replacement 为 0 次或多次时不写文件。
- 中途写失败后恢复原内容。
- 保留 executable bit。
- 拒绝二进制、大文件和路径逃逸。

**CommandPolicy**

- 精确只读命令自动执行。
- 测试命令要求审批。
- `bash -c`、网络工具、`git push`、数据库 CLI 被拒绝。
- 用户编辑 argv 后重新评估。
- workspace 内伪造的 `git` 或 `pytest` 不作为受信程序。

**Trace**

- 父子 Span 正确。
- 大 payload 写 artifact。
- secret 被脱敏。
- LangSmith payload 不含路径、源码、消息和命令。
- exporter 失败不影响 turn。

**Context**

- 达到 token 和消息阈值时压缩。
- 摘要保留最近消息和关键字段。
- 完整 journal 未被删除。
- 无 context window 时启动失败。

### 25.2 Git 集成测试

每个测试创建临时 Git 仓库，覆盖：

- clean、dirty、untracked、ignored、deleted、renamed、symlink、executable 文件。
- snapshot 不改变 HEAD、branch 和真实 index。
- `turn 0` 精确捕获初始脏状态。
- restore 精确恢复 workspace。
- restore 不修改 workspace 外仓库路径。
- restore 不删除 ignored 文件。
- restore 失败后回滚 safety snapshot。
- fork 后旧 timeline ref 和历史仍可读取。
- unborn branch。

### 25.3 Checkpoint 集成测试

使用 fake model 和真实 `SqliteSaver`：

- 每轮最终 checkpoint 可查询。
- 内部 checkpoint 不出现在用户 history。
- 从 turn N fork 后，新 thread 状态等于来源状态。
- fork 后新消息只进入新 timeline。
- context summary 随 checkpoint 恢复。
- checkpoint 缺失时拒绝文件恢复。

### 25.4 Turn 故障注入测试

在每个状态转换后注入崩溃：

- `PREPARING` 后。
- Agent 改文件但 graph 调用失败。
- checkpoint 已写但 post snapshot 未写。
- post snapshot 已写但数据库未提交。
- restore 文件写到一半。
- LangSmith 上传失败。

重启后必须得到文档第 17.5 节规定的结果，且不得静默丢失用户文件。

### 25.5 E2E 测试

使用 scripted fake model 完成：

1. 修改已有函数并运行测试。
2. 新增文件。
3. patch 冲突后重新读取并成功修改。
4. 命令审批通过、编辑和拒绝。
5. 连续多轮触发上下文压缩。
6. 从历史轮恢复并产生不同文件结果。
7. `Ctrl-C` 中断长命令并恢复。

## 26. 验收标准

V1 只有同时满足以下条件才算完成：

1. 可以从任意 Git 仓库或其子目录启动。
2. 启动不改变 HEAD、当前分支、真实 index 和已有工作树内容。
3. Agent 能搜索、读取、patch 文件并经审批运行测试。
4. Agent 无法通过工具访问 workspace 外文件或 `.git`。
5. 每个正常完成的用户轮都有唯一 final checkpoint 和 post snapshot。
6. `/restore N` 创建新 timeline，恢复 Agent 上下文和 workspace 内容。
7. restore 不删除 ignored 文件，不修改 workspace 外路径。
8. 任一联合提交阶段崩溃后可检测并恢复，不静默覆盖用户文件。
9. 上下文达到阈值时自动压缩，完整 journal 仍可查询。
10. 本地 Trace 可重建模型、工具、审批、命令、snapshot 和 checkpoint 链路。
11. LangSmith 中不出现源码、用户消息、文件路径、命令正文或命令输出。
12. 所有安全、checkpoint、Git 恢复和故障注入测试通过。
13. lint、类型检查和单元测试通过。
14. README 包含安装、配置、启动、审批、恢复和安全限制说明。

## 27. 实现顺序

实现必须按以下阶段推进，每个阶段独立通过测试后再进入下一阶段：

1. **项目骨架与配置**：创建 package、CLI、错误模型、ID 和配置。
2. **Workspace 基础**：PathGuard、仓库锁、Git snapshot、diff 和 restore。
3. **本地持久化**：数据库迁移、repository、artifact store。
4. **文件工具**：list/read/glob/search/patch/status/diff。
5. **命令系统**：ExecutionBackend、Host 实现、策略和审批。
6. **Agent 装配**：模型、prompt、工具和 middleware。
7. **Turn Coordinator**：联合 checkpoint 状态机和崩溃恢复。
8. **上下文管理**：journal、预算和 summarization。
9. **Trace**：本地 callback、应用 span 和 LangSmith 指标导出。
10. **恢复与 fork**：CLI 命令、状态复制、文件恢复和回滚。
11. **完整验证**：故障注入、E2E、文档和安全复核。

每阶段不得顺带修改 LangChain 公共 API。若发现上游能力缺失，应先在应用内通过适配层解决；只有确有通用价值时再单独设计上游变更。

## 28. 分支与提交

开始实现前创建符合仓库规范的分支：

```text
<github-username>/langchain/coding-agent
```

建议提交按实现阶段拆分，提交标题使用 Conventional Commits，例如：

```text
feat(langchain): add coding agent workspace snapshots
feat(langchain): add coding agent tools and command policy
feat(langchain): add coding agent checkpoints and tracing
```

创建分支前需要确定实际 GitHub 用户名；在此之前不得猜测用户名。
