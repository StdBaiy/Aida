# Skill 与沙箱兼容方案

## 目标

在不削弱通用命令沙箱的前提下，让用户明确批准的可信 Skill：

- 读取自身引用的子指南；
- 使用宿主机已有 CLI、登录态和网络；
- 保留超时、取消、输出限制和审计信息。

不可信代码、普通 `run_command`、Skill 临时生成的脚本仍必须进入
`SandboxExecutionService`，且沙箱不可用时继续 fail closed。

## 根因

问题不是单一的 Seatbelt 配置错误，而是四条边界混在了一起。

### 1. Skill 资源只能读取顶层文件

`SkillRegistry.load()` 只能读取预注册 Skill 的 `SKILL.md`。
`read_file` 又被 `PathGuard` 限制在 workspace 内。

`bytedcli` 的具体领域说明位于
`references/subskills/<name>/GUIDE.md`，因此顶层 Skill 虽然能被加载，
Agent 却无法继续读取它引用的子指南。

### 2. 可信 Skill 命令被错误地送入通用沙箱

`SkillExecutionManager.run_command()` 和 `start_command()` 都调用
`SandboxExecutionService.run()`。Seatbelt 则有意：

- 设置临时 `HOME`；
- 禁止读取真实用户目录；
- 执行 `(deny network*)`。

这正适合模型生成的构建、测试和代码命令，却与 `bytedcli`、`lark-cli`
等依赖宿主登录态和网络的可信 CLI 冲突。

### 3. 大部分已安装 Skill 没有可执行声明

当前只有带 `skill.json` 的 Skill 才能激活和调用
`run_skill_command`。现有 `bytedcli` Skill 只有说明和引用文件，
没有 manifest，因此 Agent 最终只能尝试普通 `run_command`，
再次进入 Seatbelt。

### 4. 重名 Skill 只能覆盖，不能精确访问

当前 `discover_skills()` 按 `workspace > user > codex` 保留第一个同名 Skill，
后续版本会被静默跳过。`SkillRegistry` 也只按裸名称索引，因此无法访问被遮蔽版本。

这还会影响宿主权限：如果可信命令只绑定裸名称 `bytedcli`，workspace 中的同名
Skill 可能错误继承本应授予用户级 Skill 的权限。

补充事实：

- 当前环境中的 `bytedcli` 已安装到受支持的
  `~/.agents/skills/bytedcli`，所以它能够被发现；
- `~/.trae-cn/skills` 本身不在默认发现路径中。MVP 不应扫描 TRAE
  内部目录，应继续使用 `.agents/skills` 作为标准安装入口；
- 子 Agent 的 `_CHILD_TOOLS` 目前也不包含 Skill 工具，但这属于能力委派，
  不应与本次兼容修复绑在一起。

## 方案

保留两条完全独立的执行通道。

```text
普通 run_command
  -> SandboxExecutionService
  -> Seatbelt / Docker

已批准的 Skill 声明命令
  -> TrustedSkillExecutionService
  -> HostExecutionBackend
```

禁止按错误类型自动切换通道。Seatbelt 失败不能触发宿主机回退。

### A. 增加稳定的 Skill 命名空间

定义规范化标识 `SkillId = <namespace>:<name>`，默认 namespace 与现有来源一致：

```text
workspace:bytedcli
user:bytedcli
codex:bytedcli
```

注册器保留所有重名 Skill，分别维护：

- `_by_id`：按完整 `SkillId` 精确查询；
- `_aliases`：裸名称按 `workspace > user > codex` 指向一个默认版本。

所有 Skill 工具的 `skill_name` 参数同时接受两种形式：

```text
load_skill("bytedcli")       # 兼容行为，解析为当前最高优先级版本
load_skill("user:bytedcli")  # 精确访问用户级版本
```

解析完成后，后续资源读取、激活、审批和执行一律使用规范化 `SkillId`。不得在执行阶段
重新按裸名称解析，避免同名 Skill 变化导致权限漂移。

目录来源必须具有唯一 namespace；未来若支持自定义根目录，配置时必须显式指定唯一
namespace，重复值在启动时直接报错。

Catalog 对唯一名称保持当前简洁展示；仅在重名时列出所有完整 ID，并标记裸名称默认
指向哪个版本。这样保留兼容性，同时避免为所有 Skill 增加提示词噪声。

### B. 支持包内资源读取

增加工具：

```text
load_skill_resource(skill_name, relative_path)
```

约束：

1. `skill_name` 必须解析为启动时生成的规范化 `SkillId`；
2. `relative_path` 必须是相对路径；
3. resolve 后必须位于该 Skill 目录内，拒绝 `..` 和逃逸 symlink；
4. 只读普通 UTF-8 文件，并沿用 `max_read_bytes`；
5. 返回逻辑 Skill 名和相对路径，不向模型暴露可用于任意读取的宿主路径。

不扩展 workspace 的 `read_file`，避免混淆两种信任边界。

### C. 显式配置可信宿主命令

不根据 Skill 文本猜测可执行权限，也不要求修改由外部工具管理的 Skill 包。
在 Agent 配置中增加小型 capability overlay：

```json
{
  "trusted_skill_commands": {
    "user:bytedcli": {
      "cli": {
        "argv": ["bytedcli", "--no-auto-upgrade"],
        "description": "Run the installed bytedcli with host credentials and network.",
        "allow_args": true,
        "timeout_seconds": 120
      }
    }
  }
}
```

overlay 的 key 必须是完整 `SkillId`，禁止使用裸名称。它只在对应 Skill 已被发现时
生效，并与 `skill.json` 命令合并。
同名冲突直接报配置错误，不做隐式覆盖。

启动时将 `argv[0]` 通过 `shutil.which()` 解析为绝对路径；运行期间始终使用该路径，
避免 workspace 或后续 `PATH` 变化劫持命令。同时记录目标文件 identity/digest，
每次执行前校验；目标被替换或消失时 fail closed。命令继续使用 argv 数组，不经过
shell。

### D. 独立的可信执行服务

新增 `TrustedSkillExecutionService`，内部复用现有
`HostExecutionBackend` 的进程组取消、超时、流式输出和截断能力。

它只接受已经解析完成的 capability，不接受任意 executable：

```text
run(capability, extra_args, environment)
```

执行环境只继承既有最小集合 `PATH`、`HOME`、`TMPDIR`、locale，再叠加 manifest
显式声明的环境变量。`cwd` 仍由 `PathGuard` 限制在 workspace。

`SkillExecutionManager` 根据合并后的 capability 选择固定通道：

- Skill 自带 `skill.json` 命令：现有 `SandboxExecutionService`；
- 用户配置 overlay 命令：`TrustedSkillExecutionService`。

不向 `skill.json` 开放 `trusted_host` 字段，避免 workspace Skill 自行请求宿主权限。
工具调度记录中，sandbox 命令保持 `workspace_write`，可信宿主命令标记为 `external`。

### E. 审批与审计

沿用当前“每个 thread、每个规范化 Skill ID 首次激活一次”的审批模型。同名 Skill
之间不共享批准状态。审批摘要必须额外展示：

- `skill_id: user:bytedcli`；
- `execution: trusted_host`；
- 解析后的 executable 绝对路径；
- `network: host`；
- `home_access: true`；
- 可能产生不可回滚外部副作用。

每次结果增加：

```json
{
  "provider": "trusted_host",
  "isolation_level": "none",
  "skill_id": "user:bytedcli",
  "command": "cli",
  "executable": "/resolved/path/to/bytedcli",
  "approval_scope": "thread"
}
```

不得记录 token、Cookie 或完整环境变量。外部写操作不能被 Git snapshot 回滚，
继续依赖 Skill 自身的 dry-run/二次确认规则。

## 安全不变量

1. 普通 `run_command` 永远不能进入宿主执行器。
2. 只有“完整 Skill ID 已发现 + 配置声明命令 + 当前 thread 已批准”才能宿主执行。
3. 模型只能追加参数，不能替换固定 executable 前缀。
4. 不使用 shell，不支持 inline Python/Node 或任意脚本路径。
5. workspace Skill 不得声明 `trusted_host`；宿主权限只能来自用户配置 overlay。
6. 沙箱不可用、网络被拒或凭据不可见时，禁止自动重试到宿主通道。
7. MCP 保持现状：仅在 Skill 激活后连接，不经过命令沙箱。
8. 裸名称只用于首次解析；权限、资源和审计都绑定规范化 Skill ID。

## 最小改动范围

1. `config.py`：增加 `trusted_skill_commands` 配置模型。
2. `skills.py`：保留重名 Skill、解析 namespace、合并 capability overlay，并增加
   受约束的资源读取工具。
3. `skill_execution.py`：按 capability 选择 sandbox 或 trusted host。
4. `runtime.py`：构造并注入两个执行服务。
5. Prompt：说明子指南使用 `load_skill_resource`，可信命令只能用
   `run_skill_command`。
6. README 和测试：补充配置、审批、路径逃逸和执行通道测试。

不修改 `SeatbeltProvider`，不增加域名白名单，不扫描
`~/.trae-cn` 内部目录，也不在本次开放子 Agent 的 Skill 委派。

## 验收

1. `bytedcli` 顶层 Skill 及任一 `references/subskills/.../GUIDE.md` 可读取。
2. 同名 Skill 可分别通过 `workspace:<name>`、`user:<name>` 和 `codex:<name>` 加载。
3. 裸名称仍解析到原有最高优先级版本。
4. 批准 `user:bytedcli` 不会授权 `workspace:bytedcli`。
5. 未配置 overlay 时，`bytedcli` 不能宿主执行。
6. 配置后未批准时返回 `SKILL_APPROVAL_REQUIRED`。
7. 批准后 `bytedcli --json auth status` 使用真实登录态并成功联网。
8. 同一命令通过普通 `run_command` 仍受 Seatbelt 禁网和临时 `HOME` 限制。
9. `../`、绝对路径和逃逸 symlink 的资源读取全部拒绝。
10. executable 在启动后被替换或消失时 fail closed，不重新按 `PATH` 查找。
11. 超时、取消、输出截断及 `trusted_host` provenance 均有自动化测试。

## 后续但不纳入本次

子 Agent 若需要 Skill，应由父 Agent 显式授予 `skill:<name>`，并继承独立审批记录。
不要简单把所有 Skill 工具加入 `_CHILD_TOOLS`。这可以在主 Agent 链路稳定后单独实现。
