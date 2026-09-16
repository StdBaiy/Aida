# Coding Agent 沙箱架构调查与设计

状态：设计提案

目标版本：Sandbox V1-V3

适用范围：`apps/coding-agent`

更新日期：2026-09-15

## 1. 执行摘要

当前系统已经具备 Git worktree、路径校验、命令策略、工具授权、协作式取消、任务租约、
结构化结果和 Trace。这些能力解决了并发修改、误操作约束和审计问题，但不构成执行不可信
代码所需的安全边界：

- `HostExecutionBackend` 仍直接在宿主机执行仓库命令；
- 子 Agent 的 worktree 与宿主机共享内核、进程空间、用户身份和环境；
- 命令黑名单无法阻止仓库测试、构建脚本或编译器插件执行任意代码；
- `PathGuard` 只约束受控文件工具，不能约束命令启动后的系统调用；
- MCP、Skill 和外部 API 的副作用不能通过进程沙箱回滚。

建议建设“策略驱动、多后端、能力经纪化”的沙箱系统：

1. **可信控制面留在沙箱外**：模型编排、策略判断、凭证、Git 集成、事件库和审计由
   Coding Agent Host 管理。
2. **不可信执行面进入沙箱**：仓库命令、测试、构建、语言服务、预览服务及任何会加载
   仓库代码的工具必须在沙箱内运行。
3. **默认拒绝网络和宿主资源**：网络、凭证、外部工具、额外目录均通过独立 Capability
   Broker 按任务授予，不能继承宿主环境。
4. **按风险选择隔离后端**：普通可信本地任务使用加固 OCI 容器；未知仓库、外部 PR、
   自动执行或多租户场景使用 gVisor；主动恶意代码和最高风险场景使用 Kata/Firecracker
   microVM。
5. **每个 Attempt 独立沙箱**：Attempt 是安全、状态、预算和审计的最小所有权单元。
   返工默认创建新沙箱，只通过可验证 patch 和 Artifact 传递结果。
6. **Git 仍是交付协议**：沙箱不直接操作用户分支，不持有 Git 凭证；控制面从沙箱导出
   patch，经范围检查和测试后在可信侧生成 commit、验收和集成。

推荐落地顺序：

- **V1，本地 MVP**：每个命令 ToolRun 使用一个加固 OCI 执行容器，网络默认关闭，
  workspace 挂载，宿主侧策略、资源限制和审计。
- **V2，完整执行隔离**：沙箱私有 workspace、文件 RPC、受控网络代理、凭证 Broker、
  gVisor 后端和持久化 SandboxLease。
- **V3，强隔离生产版**：远程 Kata/Firecracker 或 Kubernetes Agent Sandbox，warm pool、
  快照恢复、多租户配额和自动风险路由。

在当前 macOS 开发环境中，V1 可通过 Docker Desktop/Colima 提供 Linux VM 内的容器隔离；
高风险等级不应宣称由本机普通容器满足，应路由到远程 Linux gVisor 或 microVM Provider。

## 2. 调查结论

### 2.1 成熟方案的共同模式

| 方案 | 主要边界 | 适用场景 | 对本系统的启示 |
|---|---|---|---|
| Claude Code sandbox | macOS Seatbelt / Linux bubblewrap + 网络代理 | 本地可信开发、减少审批 | 文件系统与网络必须同时隔离；只隔离 Bash 不足以覆盖 MCP、hooks 和文件工具 |
| OpenAI Codex cloud | 隔离容器，网络默认关闭 | 托管 Coding Agent | 每个任务使用独立环境，默认无网络，能力按需开放 |
| Docker/rootless OCI | namespace、cgroup、seccomp、MAC | 可信或半可信代码 | 成本低、兼容好，但与宿主共享内核，不能作为最高风险边界 |
| gVisor | 用户态应用内核拦截系统调用 | 未知代码、多租户容器 | 在 OCI 兼容性和更小宿主内核攻击面之间取得平衡 |
| Kata Containers | 每个 Sandbox 独立轻量 VM/内核 | 高风险、多租户 Kubernetes | 可通过 RuntimeClass 接入现有容器编排，安全边界强于共享内核容器 |
| Firecracker | KVM microVM + jailer | 不可信代码、Serverless | 强硬件边界、低启动开销，但需要 Linux/KVM 和专门控制面 |
| E2B | 托管 microVM、模板、暂停/恢复、SDK | 快速采购沙箱能力 | Provider API 应覆盖生命周期、命令、文件、快照和网络，而不只是 `exec` |
| Kubernetes Agent Sandbox | Sandbox/Claim/Template/WarmPool CRD | 长生命周期 Agent 平台 | Sandbox 应是一级资源，具有身份、租约、TTL、持久卷和运行时选择 |

### 2.2 关键行业经验

1. **容器不是 VM**。普通 OCI 容器仍共享宿主内核；seccomp、capability 和 user namespace
   是纵深防御，不应被描述为可抵抗所有主动逃逸。
2. **文件系统隔离与网络隔离缺一不可**。只有文件隔离时，已读取数据可以外泄；只有网络
   隔离时，恶意代码仍可篡改凭证、socket 或宿主资源，为后续外泄创造条件。
3. **代理变量不是网络边界**。仅注入 `HTTP_PROXY` 无法阻止原始 socket、非 HTTP 协议、
   DNS rebinding、IPv6、Unix socket 或云 metadata 地址。网络命名空间和宿主防火墙必须
   先默认拒绝，代理只负责精细放行与审计。
4. **只隔离 shell 不足够**。文件工具、语言服务器、浏览器、MCP server、hooks 和插件都
   可能解析或执行不可信输入。应按“是否会加载仓库控制的数据”判断是否进入沙箱。
5. **审批不是隔离**。用户批准一条命令不能使其逃离沙箱；批准的含义只能是授予一个受限
   能力，例如允许访问一个域名或增加 CPU 配额。
6. **生命周期是平台能力**。TTL、强制销毁、暂停恢复、warm pool、租约和孤儿回收与底层
   隔离技术同等重要。
7. **快照可能保存入侵状态**。执行过不可信代码后的内存/磁盘快照不得提升为共享模板；
   只能在同一 Attempt 的同一安全域内恢复。

### 2.3 参考资料

- [NIST Sandbox 定义](https://csrc.nist.gov/glossary/term/sandbox)
- [NIST SP 800-190 Application Container Security Guide](https://csrc.nist.gov/pubs/sp/800/190/final)
- [Claude Code Sandboxing](https://code.claude.com/docs/en/sandboxing)
- [Claude Code Sandbox Environments](https://code.claude.com/docs/en/sandbox-environments)
- [OpenAI Codex System Card: Agent Sandbox](https://deploymentsafety.openai.com/gpt-5-3-codex/agent-sandbox)
- [gVisor Architecture and Security](https://gvisor.dev/docs/architecture_guide/intro/)
- [Firecracker](https://firecracker-microvm.github.io/)
- [Kata Containers](https://katacontainers.io/)
- [E2B Sandbox Persistence](https://e2b.dev/docs/sandbox/persistence)
- [Kubernetes SIG Agent Sandbox](https://agent-sandbox.sigs.k8s.io/docs/)
- [Docker Rootless Mode](https://docs.docker.com/engine/security/rootless/)
- [Docker Seccomp Profiles](https://docs.docker.com/engine/security/seccomp/)

## 3. 目标与非目标

### 3.1 目标

- 抵御 Agent 误操作宿主文件、进程、网络和凭证。
- 在强隔离等级下运行未知或主动恶意的仓库代码。
- 保持主 Agent、子 Agent、并行 ToolRun、取消、恢复和 ResultEnvelope 的现有语义。
- 支持本地开发、单机 Linux、Kubernetes 和托管沙箱 Provider。
- 策略默认拒绝，所有授权可解释、可审计、可撤销。
- 不信任沙箱内产生的状态声明，关键证据由控制面采集或验证。
- 允许以较低延迟执行日常任务，并将强隔离成本集中到高风险任务。

### 3.2 非目标

- 保证底层内核、hypervisor 或 CPU 不存在漏洞。
- 在 V1 自动分析任意命令的真实意图。
- 回滚数据库、云平台、MCP 等外部系统的任意副作用。
- 在同一沙箱中安全混合不同用户或不同信任域的任务。
- 允许 Agent 自行关闭安全策略或选择更低隔离等级。
- 把杀进程等同于撤销已经完成的外部副作用。

## 4. 威胁模型

### 4.1 受保护资产

| 资产 | 典型内容 |
|---|---|
| 宿主系统 | 用户目录、系统配置、其他进程、设备、Docker/Kubernetes socket |
| 用户数据 | 其他仓库、未提交代码、文档、浏览器数据 |
| 凭证 | 模型 API Key、SSH Key、Git token、云凭证、MCP token |
| 控制面 | SQLite checkpoint、事件库、策略配置、Trace、Artifact Store |
| 其他任务 | 其他 session、Agent Attempt、worktree 和缓存 |
| 网络环境 | 内网服务、metadata endpoint、数据库、开发集群 |
| 供应链 | 基础镜像、依赖缓存、编译产物、Sandbox Runner |

### 4.2 攻击者与输入

- 无意生成危险命令的 Agent。
- 包含恶意测试、构建脚本、编译器插件或安装脚本的仓库。
- 来自 issue、网页、代码注释、MCP 输出的 prompt injection。
- 恶意依赖包、预编译二进制、压缩包和媒体文件。
- 被攻陷或行为异常的 MCP/Skill 服务。
- 试图耗尽 CPU、内存、PID、磁盘、inode、网络或日志配额的代码。
- 能利用容器运行时、宿主内核或 hypervisor 漏洞的主动攻击代码。

### 4.3 必须覆盖的攻击路径

- 路径穿越、符号链接、hardlink、mount 和 `/proc`/`/sys` 逃逸。
- 读取 `.env`、SSH、云凭证、浏览器 cookie、其他 worktree。
- 通过环境变量、命令行、日志或错误信息窃取 Secret。
- 使用 DNS、HTTP、任意 TCP/UDP、Unix socket 或预览端口外泄数据。
- 访问 `169.254.169.254`、Kubernetes service account 或内网控制面。
- 容器逃逸、内核攻击面利用和跨租户侧向移动。
- fork bomb、超大输出、磁盘填充、后台守护进程和超时后残留。
- 伪造测试结果、patch、provenance、退出码或审计事件。
- 取消/超时后旧 Worker 继续写结果。
- 通过共享可写缓存污染后续任务。

### 4.4 明确的剩余风险

- microVM 仍可能受 hypervisor、KVM、硬件侧信道和供应链漏洞影响。
- Agent 可将它被明确授权读取的数据发送到被明确授权访问的目标。
- 模型请求本身发生在控制面；发送给模型的数据不受执行沙箱保护。
- 外部 API 成功产生的副作用不能靠销毁沙箱撤销。
- 本地 macOS 容器后端实际依赖 Docker Desktop/Colima 的 Linux VM 安全性和配置。

## 5. 信任边界

```text
┌──────────────────────────── Trusted Control Plane ────────────────────────────┐
│ Agent Runtime / Scheduler / Policy Engine / Approval Broker                  │
│ Task & Attempt Repository / Sandbox Registry / Git Integrator                │
│ Credential Broker / MCP Capability Broker / Artifact & Trace Store           │
└───────────────────────┬───────────────────────────────┬───────────────────────┘
                        │ versioned Sandbox RPC          │ brokered egress
                        │ mTLS/UDS + lease epoch          │ identity + policy
┌───────────────────────▼───────────────────────────────▼───────────────────────┐
│ Untrusted Sandbox                                                                  │
│ Sandbox Agent -> process supervisor -> command/test/build/LSP/preview              │
│ private rootfs + private workspace + tmpfs + cgroup limits + isolated network      │
└──────────────────────────────┬─────────────────────────────────────────────────┘
                               │ verified export only
                               ▼
                    Patch / ResultEnvelope / Artifacts
```

可信控制面不得向沙箱暴露：

- Docker/containerd/Kubernetes API socket；
- Coding Agent SQLite、checkpoint、Trace 原始目录；
- 模型或 LangSmith API Key；
- 用户 HOME、SSH、云凭证和宿主 `/tmp`；
- 主仓库 `.git` 目录或可写 Git 凭证；
- 其他沙箱的卷、网络命名空间或控制 socket。

## 6. 隔离等级与风险路由

### 6.1 隔离等级

| 等级 | 后端 | 适用范围 | 安全承诺 |
|---|---|---|---|
| `S0_HOST` | 当前 Host backend | 仅开发调试和受信命令 | 无安全隔离，不得默认使用 |
| `S1_NATIVE` | Seatbelt/bubblewrap | 本机可信仓库、低风险命令 | 限制文件和网络，不抵御宿主内核漏洞 |
| `S2_CONTAINER` | rootless OCI + seccomp/MAC | 日常仓库、半可信依赖 | 加固共享内核边界 |
| `S3_GVISOR` | OCI + runsc | 未知仓库、外部 PR、自动执行 | 用户态内核显著缩小宿主攻击面 |
| `S4_MICROVM` | Kata/Firecracker | 主动恶意、多租户、敏感资产邻近 | 独立 guest kernel 和硬件虚拟化边界 |

等级只表示底层执行隔离，文件、网络、凭证和资源策略仍需独立配置。例如，
`S4_MICROVM + unrestricted_network` 仍可能外泄数据。

### 6.2 风险分类输入

风险判断必须由确定性 Policy Engine 完成，模型只能请求升级，不能自行降级。

```text
RiskInputs
  source_trust:
    local_trusted | local_unknown | external_pr | downloaded | adversarial
  execution:
    no_exec | known_binary | repo_script | package_install | native_binary | privileged_request
  data_sensitivity:
    public | internal | confidential | credential_adjacent
  network:
    none | package_registry | allowlisted_api | arbitrary | internal
  autonomy:
    attended | background | unattended
  tenancy:
    single_user | shared_host | multi_tenant
  capabilities:
    file_read/write, mcp, browser, preview, device, docker
```

### 6.3 默认路由规则

| 条件 | 最低等级 |
|---|---|
| 只做受控文本读取与结构化 patch，不执行仓库代码 | `S1_NATIVE` 或无执行沙箱 |
| 可信本地仓库运行测试/构建 | `S2_CONTAINER` |
| 安装依赖、执行仓库脚本、解析未知二进制 | `S2_CONTAINER` |
| 外部 PR、下载代码、无人值守执行 | `S3_GVISOR` |
| 明确恶意样本、多租户不可信代码、敏感环境邻近 | `S4_MICROVM` |
| 请求 privileged、host network、Docker socket、宿主设备 | 默认拒绝，不通过升级自动放行 |

Provider 不可用时必须 fail closed。高风险任务不能静默回落到 Host 或普通容器。

## 7. 总体架构

```text
User / Main Agent
        │
        ▼
TaskContract + requested capabilities
        │
        ▼
Risk Classifier ──> Policy Decision ──> Approval Broker
        │                  │
        │                  └── immutable SandboxSpec + policy hash
        ▼
Sandbox Manager ──> Provider Router ──> Local OCI / gVisor / MicroVM / Remote
        │                                      │
        │                                  Sandbox Agent
        │                                      │
        ├── Lease / heartbeat / cancel          ├── exec
        ├── Workspace import/export             ├── file RPC
        ├── Egress & Credential Broker          ├── process events
        └── Artifact / audit ingestion          └── result manifest
        │
        ▼
Verifier ──> Git commit ──> Main Agent review ──> integration
```

### 7.1 新增组件

| 组件 | 职责 |
|---|---|
| `SandboxPolicyEngine` | 风险分类、策略合并、授权判断、降级阻止 |
| `SandboxManager` | 生命周期、租约、幂等创建、孤儿回收、Provider 路由 |
| `SandboxProvider` | 屏蔽 OCI、gVisor、Kata、Firecracker、E2B 等实现差异 |
| `WorkspaceBroker` | 基线导入、文件 RPC、patch 导出、范围和完整性验证 |
| `ExecutionBroker` | 启动、流式输出、超时、取消、资源统计 |
| `EgressGateway` | 默认拒绝网络、域名/API 放行、DNS 防护、网络审计 |
| `CredentialBroker` | 短期、限域、最小权限凭证签发与撤销 |
| `ArtifactVerifier` | 校验输出哈希、patch、测试证据和来源 |
| `SandboxReconciler` | 重启恢复、租约过期、泄漏资源和终态收口 |

## 8. 核心领域模型

### 8.1 SandboxSpec

`SandboxSpec` 是不可变执行契约。创建后任何扩权都生成新 revision，并记录原策略与批准。

```json
{
  "schema_version": 1,
  "sandbox_id": "sbx_...",
  "task_id": "task_...",
  "attempt_id": "attempt_...",
  "provider_class": "gvisor",
  "image": {
    "ref": "coding-agent/python@sha256:...",
    "platform": "linux/arm64",
    "signature_policy": "required"
  },
  "workspace": {
    "base_commit": "...",
    "mode": "private_copy",
    "read_paths": ["**"],
    "write_paths": ["coding_agent/**", "tests/**"],
    "deny_paths": [".git/**", ".env*", "**/*.pem", "**/*credentials*"],
    "max_bytes": 2147483648
  },
  "network": {
    "mode": "deny_all",
    "allow_domains": [],
    "deny_private_ranges": true,
    "allow_listen_ports": []
  },
  "resources": {
    "cpu": {
      "quota_millis": 2000,
      "burst_millis": 4000,
      "cumulative_seconds": 1800
    },
    "memory": {
      "hard_bytes": 2147483648,
      "swap_bytes": 0
    },
    "process": {
      "max_pids": 256,
      "max_open_files": 4096
    },
    "disk": {
      "workspace_bytes": 5368709120,
      "tmpfs_bytes": 536870912,
      "cache_bytes": 2147483648,
      "max_inodes": 200000
    },
    "io": {
      "read_bps": 104857600,
      "write_bps": 52428800,
      "read_iops": 5000,
      "write_iops": 2000
    },
    "time": {
      "execution_wall_seconds": 900,
      "attempt_wall_seconds": 3600,
      "idle_seconds": 300,
      "termination_grace_seconds": 3
    },
    "concurrency": {
      "max_executions": 4
    },
    "output": {
      "stdout_bytes": 10485760,
      "stderr_bytes": 10485760,
      "artifact_bytes": 104857600
    },
    "network_bytes": 524288000
  },
  "credentials": [],
  "tools": ["exec", "read_file", "apply_patch"],
  "policy_revision": "policy_...",
  "spec_hash": "sha256:..."
}
```

策略合并顺序：

```text
organization ceiling
  ∩ workspace policy
  ∩ task contract
  ∩ user approval
  ∩ provider capability
```

任何一层拒绝都不可被下层覆盖。deny 优先于 allow。

### 8.2 SandboxInstance

```text
sandbox_id
attempt_id
provider
provider_instance_id
spec_hash
status
lease_id
lease_epoch
image_digest
workspace_digest
network_policy_digest
created_at
ready_at
last_heartbeat_at
expires_at
terminated_at
failure_code
```

状态机：

```text
REQUESTED -> PROVISIONING -> READY -> RUNNING
                              │         │
                              │         ├-> PAUSING -> PAUSED -> RESUMING -> READY
                              │         └-> TERMINATING
                              └---------------------------> TERMINATING

PROVISIONING/READY/RUNNING/PAUSED -> FAILED
TERMINATING -> TERMINATED
lease expired -> ORPHANED -> TERMINATING -> TERMINATED
```

`TERMINATED` 是终态。删除 Provider 资源失败时仍保持 `TERMINATING`，由 Reconciler 重试，
不能仅在数据库中标记成功。

### 8.3 SandboxLease

沿用子 Agent 已有 lease/fencing 思路：

```text
lease_id
sandbox_id
attempt_id
owner_worker_id
lease_epoch
issued_at
expires_at
heartbeat_at
revoked_at
```

所有 exec、文件写入、结果提交和状态更新必须携带 `lease_epoch`。控制面拒绝旧 epoch 的
迟到结果，解决取消、重试、Worker 重启后的 zombie write。

### 8.4 SandboxExecution

一个 Attempt 可在同一 Sandbox 中运行多个 ToolRun：

```text
execution_id
sandbox_id
tool_run_id
argv
cwd
environment_keys
stdin_artifact_ref
status
pid_handle
started_at
ended_at
exit_code
termination_reason
stdout_artifact_ref
stderr_artifact_ref
resource_usage_json
policy_decision_id
```

`argv` 必须是结构化数组，不接受隐式 shell。需要 shell 语义时，shell 本身仍可在沙箱内
运行，但必须在策略和审计中标记为 `dynamic_execution`。

### 8.5 ResourceBudget 与 ResourceUsage

资源预算必须是可持久化、可比较的领域对象，不能只存在于 Docker/Kubernetes 启动参数中：

```text
ResourceBudget
  scope_type: host | organization | workspace | session | attempt | execution
  scope_id
  cpu_quota_millis
  cpu_burst_millis
  cpu_cumulative_seconds
  memory_hard_bytes
  swap_bytes
  max_pids
  max_open_files
  workspace_bytes
  tmpfs_bytes
  cache_bytes
  max_inodes
  read_bps / write_bps
  read_iops / write_iops
  execution_wall_seconds
  attempt_wall_seconds
  idle_seconds
  max_parallel_executions
  stdout_bytes / stderr_bytes / artifact_bytes
  network_bytes
  policy_revision
```

Provider 或可信节点采集器周期性生成：

```text
ResourceUsage
  sandbox_id
  execution_id
  sampled_at
  cpu_usage_usec
  cpu_throttled_usec
  memory_current_bytes
  memory_peak_bytes
  oom_events
  pids_current
  disk_used_bytes
  inode_used
  io_read_bytes / io_write_bytes
  network_rx_bytes / network_tx_bytes
  stdout_bytes / stderr_bytes / artifact_bytes
```

最终 usage summary、峰值、累计值和终止原因必须由控制面写入执行证据。Agent 自报的资源
使用量只能作为普通文本，不能作为计费或安全判断依据。

## 9. Provider 接口

```python
class SandboxProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...
    def create(self, spec: SandboxSpec, idempotency_key: str) -> SandboxHandle: ...
    def wait_ready(self, handle: SandboxHandle, deadline: datetime) -> SandboxStatus: ...
    def exec(
        self,
        handle: SandboxHandle,
        request: ExecRequest,
        lease_epoch: int,
    ) -> ExecHandle: ...
    def stream(self, execution: ExecHandle, after_cursor: int) -> list[ExecEvent]: ...
    def cancel(self, execution: ExecHandle, grace_seconds: int) -> None: ...
    def export(self, handle: SandboxHandle, request: ExportRequest) -> ArtifactRef: ...
    def pause(self, handle: SandboxHandle) -> None: ...
    def resume(self, handle: SandboxHandle) -> SandboxHandle: ...
    def destroy(self, handle: SandboxHandle) -> None: ...
    def inspect(self, handle: SandboxHandle) -> ProviderObservation: ...
```

要求：

- 所有 mutating API 有幂等键；
- `create` 返回不代表 Ready；
- `cancel` 与 `destroy` 必须可重复调用；
- Provider 必须声明是否支持网络策略、暂停、快照、私有 workspace、资源统计和强制销毁；
- Provider 不满足 Spec 时创建失败，不得忽略字段；
- Provider 原始错误映射为稳定的领域错误码，同时保留受限诊断 Artifact。

## 10. Workspace 与 Git 设计

### 10.1 最终推荐：私有 workspace

强隔离模式不 bind mount 宿主 worktree：

1. 控制面固定 `base_commit`。
2. `WorkspaceBroker` 使用 `git archive` 或内容寻址 bundle 生成只读输入 Artifact。
3. Provider 将输入解压到沙箱私有卷 `/workspace`。
4. 沙箱内不包含宿主 `.git`，只保存只读的基线 manifest。
5. Agent 修改私有 workspace。
6. 结束时导出文件 manifest、二进制安全 patch 和新增 Artifact。
7. 控制面验证路径、大小、类型、symlink、hash、scope 和配额。
8. 在可信临时 worktree 中应用 patch、运行独立验收，再创建 `result_commit`。
9. Main Agent 只审阅可信侧 commit 和结构化证据。

这样可以避免：

- 沙箱通过 `.git` 指针访问主仓库对象或 hooks；
- bind mount 配置错误暴露宿主路径；
- 远程 Provider 与本地 worktree 的路径耦合；
- 沙箱伪造 Git ref 或直接修改用户分支。

### 10.2 V1 过渡：隔离 worktree bind mount

V1 可将 Attempt worktree 挂载为 `/workspace`，但必须：

- 只挂载该 Attempt worktree，不挂载 repo root；
- 屏蔽 `.git` 文件/目录，Git commit 在宿主可信侧完成；
- 根文件系统只读，仅 `/workspace`、`/tmp` 和显式 cache 可写；
- 容器结束后通过宿主 Git diff 检测所有变化；
- 超 scope 变化导致 Attempt 验收失败；
- 不把 ignored 文件或宿主未跟踪 Secret 复制进 worktree；
- 明确标记 `workspace.mode=host_bind`，不能用于 `S4_MICROVM` 安全承诺。

### 10.3 缓存

- 基础镜像与依赖缓存按 digest 固定。
- 沙箱只读挂载共享 CAS；写缓存必须按租户和信任域隔离。
- 不复用执行过仓库脚本的可写 `node_modules`、venv、compiler cache 作为公共缓存。
- cache 命中必须进入 provenance，包含来源、digest 和扫描状态。

## 11. 文件系统、进程与资源治理

### 11.1 隔离基线

所有 OCI 类后端至少满足：

- 非 root 用户；rootless daemon 或受控 runtime；
- `cap-drop=ALL`，不允许 `CAP_SYS_ADMIN`；
- `no-new-privileges`；
- 默认 seccomp，生产环境维护更窄 profile；
- AppArmor/SELinux/Landlock 等 MAC；
- read-only rootfs；
- 独立 PID、mount、IPC、UTS、user、network namespace；
- 禁止 host PID/network/IPC、privileged、device passthrough；
- `/proc`、`/sys` 使用受限挂载；
- `/tmp` 使用带大小限制的 tmpfs，`nosuid,nodev`，可行时 `noexec`；
- 不挂载 Docker socket、SSH agent socket、GPG agent socket；
- 限制 PID、file descriptor、core dump、共享内存和 inode；
- 子进程由 Sandbox Agent 作为 subreaper 统一回收。

单纯命令黑名单降级为用户体验和策略提示，不再承担安全边界职责。

### 11.2 配额层级与准入

资源限制按以下层级同时生效：

```text
host/provider capacity
  -> organization quota
  -> workspace quota
  -> session quota
  -> attempt sandbox budget
  -> execution/ToolRun budget
```

子级预算只能等于或小于父级剩余预算。SandboxManager 在创建 Sandbox 和启动 ToolRun 前
执行原子资源预留；容量不足时进入 `WAITING_RESOURCES`，不得先启动再依赖 OOM 或抢占收口。

准入检查至少包含：

- 请求的 CPU、内存、磁盘与 Provider 可用容量；
- 当前 Session 和 Workspace 的并行 Sandbox 数；
- Attempt 已使用的累计 CPU 时间、网络流量和 Artifact 容量；
- 并行 ToolRun 的总预算，不能给每个 ToolRun 重复授予完整 Sandbox 上限；
- deadline 是否足以覆盖 provisioning 和最短执行时间；
- 高优先级任务是否允许抢占低优先级 Sandbox。

资源预留必须带 TTL。Worker 在创建失败或失联后，由 Reconciler 释放预留，避免“账面占用”
长期阻塞后续任务。

### 11.3 强制机制

| 资源 | 推荐强制机制 | 超限行为 |
|---|---|---|
| CPU 瞬时使用 | cgroup v2 `cpu.max` / Kubernetes CPU limit / VMM rate limiter | 限速，不立即终止 |
| CPU 累计预算 | 可信侧读取 `cpu.stat` 或 Provider usage | 取消 Attempt |
| 内存 | cgroup v2 `memory.max`，swap 默认 0 | OOM kill，标记 `SANDBOX_MEMORY_LIMIT` |
| PID/线程 | cgroup v2 `pids.max` | 新进程创建失败；持续异常则终止 |
| 文件描述符 | `RLIMIT_NOFILE` | 系统调用失败并记录 |
| workspace 磁盘 | 独立 volume/quota/overlay 上层大小 | 停止写入并终止导出 |
| tmpfs/共享内存 | 独立 size limit | 写入失败 |
| inode | 文件系统 project quota 或独立 volume | 停止创建文件 |
| 块 I/O | cgroup v2 `io.max` / Provider rate limiter | 限速；累计超限则取消 |
| 网络流量 | Egress Gateway 计量和限速 | 断开连接、撤销网络能力 |
| stdout/stderr | 流式计数、截断和 Artifact 配额 | 停止采集；持续输出则终止 |
| wall time/idle time | 控制面 deadline timer | TERM 后 KILL |
| 并行执行数 | Sandbox Agent semaphore + 控制面校验 | 排队或拒绝 |

关键限制必须由宿主 cgroup、hypervisor、存储层或网络 Gateway 强制，不能只由沙箱内
Sandbox Agent 执行。Sandbox Agent 可以提供更友好的预警，但其被杀死或攻陷不能解除硬限制。

CPU 需要同时设置：

- **瞬时配额**：防止单个任务长期占满所有核心；
- **burst**：允许编译等短时高并发，但受 host/provider 上限控制；
- **累计 CPU 时间**：防止低速后台进程绕过 wall-clock 预算。

内存限制必须覆盖 page cache 和所有后代进程。禁止无限 swap；默认 `swap_bytes=0`，确有
兼容需求时也必须设硬上限。磁盘限制必须分别覆盖 workspace、tmpfs、共享内存、可写 cache、
日志和导出 Artifact，不能只检查最终 worktree 大小。

### 11.4 后台进程归属

所有由 Agent、测试框架或构建脚本派生的后台进程必须满足：

1. 始终留在 Attempt Sandbox 的 cgroup 或 microVM 中。
2. 每个 ToolRun 使用子 cgroup 或 Provider execution group，便于单独计量和取消。
3. ToolRun 返回时检查其 execution group 是否仍有活跃进程。
4. 除明确声明为 `service` 的预览/开发服务器外，残留进程视为执行未完成。
5. `service` 进程也受 Attempt 总预算、idle timeout、端口和生命周期约束。
6. Attempt 完成、取消、租约过期或控制面重启回收时，销毁整个 cgroup/VM，而不是只 kill
   已知 PID。

这样可阻止 `nohup`、double-fork、setsid、子进程改名或守护进程化绕过 ToolRun 取消和资源
计费。

### 11.5 超限策略与资源扩容

资源接近软阈值时，控制面发送一次 `resource.warning`，包含当前值、硬上限和剩余预算。
达到硬上限时不询问模型，直接由 Provider 限速或终止，并写入 `resource.exhausted`。

Agent 可以请求增加预算，但必须满足：

- 请求发生在超限前，包含资源类型、增量、期限和理由；
- Policy Engine 检查组织上限、Provider 容量和任务风险；
- 用户审批只增加该 Attempt 的指定资源，不能修改组织上限；
- 扩容生成新的 `SandboxSpec` revision 和 policy decision；
- Provider 不支持原地调整时，暂停并迁移到新 Sandbox，不能回落到 Host；
- 内存 OOM、磁盘写满后默认不自动重试，避免重复消耗。

资源超限后的默认处置：

| 场景 | 处置 |
|---|---|
| CPU 瞬时超限 | 持续限速，保留执行 |
| CPU 累计/Attempt wall time 超限 | 取消整个 Attempt |
| 单 ToolRun wall time 超限 | 终止该 execution；由策略决定是否允许后续 ToolRun |
| 内存 OOM | 终止 execution；沙箱状态不可信时销毁整个 Sandbox |
| 磁盘/inode 超限 | 冻结写入、采集有限诊断、销毁 Sandbox |
| 输出超限 | 截断一次并通知；持续高速输出则终止 execution |
| 网络字节超限 | 撤销 egress；有外部副作用的请求标记结果不确定 |
| PID 超限 | 拒绝新进程；若无法收敛则终止 execution |

### 11.6 计量与公平调度

- CPU 使用按实际 CPU time 计量，wall time 单独记录。
- 内存记录 current、peak、OOM 事件和 throttling/pressure 信息。
- 磁盘使用包含普通文件、删除但仍被打开的文件、overlay upper layer 和 Artifact staging。
- 资源采样由 Provider 或节点侧可信组件完成，采样间隔建议 1-5 秒。
- 终态前执行一次强制采样，避免最后一个周期的使用量丢失。
- Scheduler 使用加权公平队列，防止一个 Session 的多个子 Agent 占满全局容量。
- 并行 ToolRun 的资源总和不能超过 Attempt hard limit；超额请求排队。
- `PAUSED` Sandbox 不占 CPU，但保留的内存/磁盘快照继续计入存储配额。

UI 至少展示 CPU、内存峰值、磁盘使用、运行时长、当前限速/排队状态和超限原因。资源指标
用于用户理解和运维告警，不应把高 CPU 本身解释为恶意行为。

## 12. 网络设计

### 12.1 默认策略

```text
default: deny all ingress and egress
DNS: only controlled resolver
private/link-local/metadata ranges: always deny
Unix sockets: deny unless由显式 broker 创建
listening ports: deny unless preview capability grants
```

### 12.2 受控放行

网络路径必须是：

```text
Sandbox netns/tap
  -> host firewall default deny
  -> authenticated Egress Gateway
  -> DNS/IP/SNI/HTTP policy
  -> approved destination
```

Gateway 必须处理：

- 域名 allowlist 与端口；
- DNS 解析固定、TTL、rebinding 检查；
- 阻止 RFC1918、loopback、link-local、multicast、metadata 和集群网段；
- IPv4/IPv6 策略一致；
- HTTP CONNECT/SOCKS 和非 HTTP 流量；
- 请求字节、响应字节、目标、时间和 policy decision 审计；
- 可选下载大小、MIME、包 digest 和恶意内容扫描；
- 每个 Attempt 独立网络身份和速率限制。

常用网络 Profile：

| Profile | 放行 |
|---|---|
| `offline` | 无 |
| `package_python` | 指定 PyPI mirror，只读 |
| `package_node` | 指定 npm mirror，只读 |
| `git_readonly` | 指定代码托管域名，只允许 clone/fetch |
| `api_allowlist` | 精确域名、端口和方法 |
| `preview_local` | 仅通过 Preview Gateway 暴露一个沙箱端口 |

“任意公网”和“内网访问”不应作为普通审批选项。

## 13. 凭证与外部能力

### 13.1 原则

- 沙箱环境变量从空白 allowlist 构建，不继承 Host `os.environ`。
- 模型 API Key 永不进入执行沙箱。
- Git clone/push 优先由可信 Git Broker 执行；沙箱不持有 SSH Key。
- 需要凭证时由 Credential Broker 签发短期、限 audience、限 scope token。
- Secret 使用 tmpfs 文件、一次性 FD 或本地 broker socket 提供，避免出现在 argv、Trace 和
  长生命周期环境变量中。
- 每次签发、使用、刷新和撤销都关联 task/attempt/execution。

### 13.2 MCP 与 Skill

进程沙箱不能限制外部 MCP 已经拥有的权限。因此 MCP 继续位于可信控制面，通过 Capability
Broker 调用：

```text
Subagent request
 -> exact mcp server/tool/schema grant
 -> argument policy
 -> approval/idempotency check
 -> trusted MCP bridge
 -> redacted result + provenance
```

必须禁止把通用 MCP client token 或 server credentials 下发到沙箱。具有外部写副作用的
MCP 工具需单独标记 `external_write`，默认串行、显式审批，并要求幂等键。

## 14. 生命周期、取消与恢复

### 14.1 创建

1. 持久化 Task/Attempt。
2. 计算 RiskDecision 和不可变 SandboxSpec。
3. 在同一数据库事务中创建 `sandbox.requested` outbox。
4. SandboxManager 使用 `attempt_id:spec_hash` 作为幂等键创建资源。
5. Provider Ready 后写入 `sandbox.ready`。
6. Attempt 获得当前 lease epoch 后才可执行。

### 14.2 取消

取消顺序：

1. CAS 写入 Attempt `CANCEL_REQUESTED` 并递增/撤销 lease epoch。
2. 停止接受新 ToolRun。
3. 对活跃 execution 发送 TERM，宽限期后 KILL。
4. 撤销网络和短期凭证。
5. Provider 销毁或冻结 Sandbox。
6. 确认资源消失后写 `sandbox.terminated` 和 Attempt `CANCELLED`。
7. 拒绝所有旧 epoch 的迟到事件和 ResultEnvelope。

控制面重启后 Reconciler 从数据库和 Provider 双向对账，不能依赖内存 Future。

### 14.3 超时和资源耗尽

稳定错误码：

```text
SANDBOX_PROVISION_TIMEOUT
SANDBOX_POLICY_DENIED
SANDBOX_PROVIDER_UNAVAILABLE
SANDBOX_EXEC_TIMEOUT
SANDBOX_CPU_LIMIT
SANDBOX_CPU_BUDGET_EXHAUSTED
SANDBOX_MEMORY_LIMIT
SANDBOX_PID_LIMIT
SANDBOX_DISK_LIMIT
SANDBOX_INODE_LIMIT
SANDBOX_IO_LIMIT
SANDBOX_OUTPUT_LIMIT
SANDBOX_NETWORK_BUDGET_EXHAUSTED
SANDBOX_RESOURCE_CAPACITY
SANDBOX_NETWORK_DENIED
SANDBOX_CREDENTIAL_DENIED
SANDBOX_CANCELLED
SANDBOX_LOST
SANDBOX_EXPORT_INVALID
SANDBOX_DESTROY_INCOMPLETE
```

资源耗尽是正常领域结果，不应统一映射为 `RUNTIME_ERROR`。

限速与硬失败必须区分：CPU throttling、I/O throttling 属于运行状态；只有累计预算耗尽、
OOM、硬磁盘上限或 deadline 才进入失败/取消状态。错误结果应包含 `resource_type`、
`limit`、`observed_peak`、`usage_artifact_ref` 和 `retryable`，供主 Agent 判断缩小任务、
申请扩容或更换 Provider。

### 14.4 暂停和恢复

- V1 不要求内存快照。
- V2 可暂停同一 Attempt 的 Sandbox，释放计算资源。
- 恢复前必须验证 Provider identity、spec hash、image digest 和 lease epoch。
- 执行过不可信代码的快照不能用于其他 Attempt、用户或模板。
- 返工默认从可信 `base_commit + accepted intermediate patch` 创建新 Sandbox，不继承进程内存。

## 15. 审计、证据与 ResultEnvelope

现有 `ResultEnvelope` 应增加：

```json
{
  "sandbox": {
    "sandbox_id": "sbx_...",
    "provider": "gvisor",
    "isolation_level": "S3_GVISOR",
    "spec_hash": "sha256:...",
    "image_digest": "sha256:...",
    "workspace_input_digest": "sha256:...",
    "workspace_output_digest": "sha256:...",
    "lease_epoch": 3
  },
  "executions": [
    {
      "execution_id": "exec_...",
      "argv_digest": "sha256:...",
      "exit_code": 0,
      "resource_usage": {
        "cpu_seconds": 18.4,
        "cpu_throttled_seconds": 2.1,
        "memory_peak_bytes": 734003200,
        "disk_peak_bytes": 120586240,
        "pids_peak": 37
      },
      "resource_usage_ref": "artifact://...",
      "stdout_ref": "artifact://...",
      "stderr_ref": "artifact://..."
    }
  ],
  "policy_decisions": ["policy-decision://..."],
  "network_evidence": ["artifact://..."],
  "termination_reason": "completed"
}
```

证据可信度分级：

| 证据 | 采集方 | 信任级别 |
|---|---|---|
| 模型总结 | Agent | 声明 |
| 沙箱内测试日志 | Sandbox Agent | 未信任执行证据 |
| Provider exit/resource 状态 | Provider/control plane | 平台证据 |
| 控制面重新应用 patch | WorkspaceBroker | 验证证据 |
| 独立验收沙箱测试 | Verifier Sandbox | 强验证证据 |
| Git result commit | Trusted Git Integrator | 交付证据 |

审计事件至少包括 Sandbox 生命周期、策略决策、capability grant、exec、网络、Secret 使用、
Artifact 导入导出、取消和销毁。高频 stdout 进入 Artifact Store，事件仅保存 cursor 和 hash。

## 16. 与当前系统的集成

### 16.1 复用点

| 当前能力 | 沙箱中的位置 |
|---|---|
| `ExecutionBackend` Protocol | 扩展为 Sandbox execution facade |
| `ToolRunManager` | 保留并行、cursor、取消和 UI 事件 |
| `effect` 分类 | 扩展为 risk/capability 输入 |
| `WorkspaceMutationGate` | 继续保护主工作区集成，不用于沙箱内部串行化 |
| 子 Agent Attempt | 作为 Sandbox 所有权和租约边界 |
| worktree/base/result commit | 继续作为代码交付协议 |
| `allowed_tools` / MCP grant | 输入 Capability Broker |
| CancellationToken | 连接到 Provider cancel/destroy |
| Trace/Artifact | 保存平台采集证据 |
| ResultEnvelope | 增加 sandbox provenance |

### 16.2 必须调整的边界

1. `build_tools()` 不应直接构造 `HostExecutionBackend`，改为注入 `ExecutionService`。
2. `HostExecutionBackend` 仅保留为显式 `S0_HOST` 开发后端。
3. `run_command` 的 `effect` 不能固定为 `workspace_write`，应拆成文件、副作用、网络和执行
   风险维度。
4. 会加载仓库代码的 Skill command 必须共享 SandboxLease，不得另起宿主进程。
5. 子 Agent `workspace_mode` 与 `sandbox_mode` 分离：是否需要写 workspace 不决定隔离等级。
6. `SubagentWorkspaceManager.commit()` 移到可信导出验证之后。
7. MCP 工具保留在控制面，其授权记录进入同一 policy/provenance 链。

### 16.3 建议模块

```text
coding_agent/sandbox/
  models.py
  policy.py
  risk.py
  manager.py
  repository.py
  reconciler.py
  provider.py
  workspace.py
  execution.py
  network.py
  credentials.py
  artifacts.py
  providers/
    host.py
    oci.py
    gvisor.py
    kubernetes.py
    microvm.py
    e2b.py
```

领域层只依赖 `SandboxProvider` 和结构化模型，不应包含 Docker/Kubernetes 命令拼接。

## 17. 部署建议

### 17.1 本地 macOS

- V1：Docker Desktop 或 Colima 内运行加固 OCI Sandbox。
- Host 只通过受限 Docker API adapter 创建预定义容器；Agent 永远看不到 socket。
- 网络默认 `--network none`。
- 高风险任务显示“需要远程强隔离”，路由至 gVisor/microVM Provider。
- macOS Seatbelt 可作为轻量优化，但不作为跨平台核心实现。

### 17.2 单机 Linux

- rootless containerd/Docker + cgroup v2。
- `S2_CONTAINER` 使用 runc 加固 profile。
- `S3_GVISOR` 使用 runsc。
- Runner daemon 独立低权限用户，通过 Unix socket 与 Host 通信。
- 宿主 nftables 提供默认拒绝网络与 egress gateway。

### 17.3 Kubernetes

- 控制面与 Sandbox workload 分 namespace、service account 和 node pool。
- 使用 Kubernetes SIG Agent Sandbox 的 Template/Claim/WarmPool 语义。
- `runtimeClassName=runsc` 对应 S3；Kata 对应 S4。
- 默认 deny NetworkPolicy，同时在节点/egress gateway 再做一层强制。
- ResourceQuota、LimitRange、Pod Security Admission 和独立 Artifact 存储。
- 不把 Kubernetes service account token 自动挂载到 Sandbox。

### 17.4 自建 Firecracker

- 控制面不直接持有 `/dev/kvm`，通过最小化 Runner daemon 管理。
- 每个 Sandbox 一个 microVM、独立 tap、rootfs overlay、cgroup 和 jailer。
- Host/guest 使用 vsock 的版本化协议。
- 使用只读基础 rootfs + 每实例 COW 层。
- warm pool 只能来自执行前的干净模板快照。
- 该方案安全和性能潜力最高，但运维复杂度显著高于 Kata/Kubernetes。

因此，除非 Sandbox 是产品核心竞争力，V3 优先采用 Kubernetes Agent Sandbox + Kata 或成熟
托管 Provider，不建议一开始自建 Firecracker 控制面。

## 18. OCI MVP 功能范围

### 18.1 MVP 目标

OCI MVP 只验证一件事：在不破坏现有主 Agent、子 Agent、ToolRun、取消、worktree 和
ResultEnvelope 语义的前提下，所有可能执行仓库代码的本地命令都不再直接运行在 Host，
而是在受限、可终止、可审计的 Docker 容器中运行。

支持范围：

| 项目 | MVP 结论 |
|---|---|
| macOS | 支持 Docker Desktop 和 Colima 提供的 Docker context |
| Linux | 支持 Docker Engine；推荐 rootless Docker |
| Provider | 只实现 Docker CLI Provider |
| 接口 | 保留最小 `SandboxProvider` 抽象，避免工具层依赖 Docker |
| 隔离等级 | `S2_CONTAINER` |
| 网络 | 完全关闭，不支持动态放行 |
| workspace | Host worktree bind mount，`.git` 使用可信空挂载遮蔽 |
| 生命周期 | 每个命令 ToolRun 一个短生命周期执行容器 |
| 镜像 | 只允许管理员配置、digest 固定的可信镜像 |
| 资源 | CPU、内存、PID、时间和输出硬限制；磁盘采用 tmpfs 硬限制与 workspace 增量监控 |
| 审批 | 保留当前命令审批；批准后仍必须在容器内执行 |

MVP 成功不代表能够安全运行明确恶意的内核逃逸样本。其安全承诺是：

- 防止普通命令和仓库脚本直接访问宿主 HOME、凭证、进程、网络和 workspace 外文件；
- 限制 Agent 在后台消耗的 CPU、内存、PID、时间、临时磁盘和输出；
- 命令取消、超时或 Host 重启后可以发现并回收残留容器；
- 所有命令结果都带有可核验的容器、镜像、资源和终止来源。

### 18.2 为什么 MVP 选择每 ToolRun 一个容器

目标架构仍以 Attempt 作为安全域，但 Docker CLI 的 `docker exec` 缺少可靠的单 execution
资源隔离和跨平台进程组取消。MVP 使用“每个 `run_command` ToolRun 一个容器”：

```text
Agent Turn / Subagent Attempt
  -> ToolRunManager
  -> OciExecutionService
  -> DockerCliProvider.create_execution_container()
  -> docker start + attach
  -> result / cancel / timeout
  -> docker rm --force
```

收益：

- 取消时销毁容器即可终止所有后代进程，不需要相信容器内 PID 跟踪；
- 每个命令可独立设置 CPU、内存、PID 和 wall time；
- 并行 ToolRun 互不影响进程生命周期；
- 命令完成后没有可继续消耗资源的后台守护进程；
- 不需要在 MVP 内开发常驻 Sandbox Agent。

代价：

- 命令之间不保留内存、后台服务和容器内安装的包；
- 每条命令有容器启动开销；
- 多个命令通过同一 bind-mounted workspace 协作，仍需现有 mutation gate 和冲突检查；
- 无法支持需要跨命令常驻服务的预览场景。

这些限制应在 UI 和工具说明中明确。Attempt 级常驻 Sandbox、service ToolRun 和 warm pool
延后到 V2。

### 18.3 P0 必要功能

#### 18.3.1 Docker Provider 预检

启动 Host 或打开 workspace 时执行只读预检：

```text
docker version
docker info
docker context show
docker image inspect <configured-image@sha256:digest>
```

预检必须验证：

- Docker CLI 存在且 client/server 可通信；
- daemon 运行 Linux containers；
- 当前 context 是显式记录的 Docker Desktop、Colima 或 Docker Engine；
- 目标镜像存在或能由可信控制面拉取；
- 实际镜像 digest 与配置一致；
- CPU、memory、PID、read-only rootfs、network none 和 bind mount 能力可用；
- Docker daemon 不是由沙箱容器直接访问；
- Linux rootless 状态被记录，但 MVP 不强制所有环境必须 rootless。

预检结果持久化为 `ProviderHealth`，包括 provider version、context、OS、architecture、
rootless、capability 和最近检查时间。预检失败时 `run_command` 必须 fail closed，不能回落
到 `HostExecutionBackend`。

#### 18.3.2 最小 Provider 接口

MVP 只实现执行容器需要的子集：

```python
class OciExecutionProvider(Protocol):
    def health(self) -> ProviderHealth: ...
    def prepare(self, spec: OciExecutionSpec) -> PreparedExecution: ...
    def start(
        self,
        prepared: PreparedExecution,
        on_output: OutputCallback,
    ) -> ExecutionHandle: ...
    def inspect(self, handle: ExecutionHandle) -> ExecutionObservation: ...
    def cancel(self, handle: ExecutionHandle, grace_seconds: int) -> None: ...
    def remove(self, handle: ExecutionHandle) -> None: ...
    def list_owned(self, host_instance_id: str) -> list[ExecutionHandle]: ...
```

工具层只能依赖 `OciExecutionProvider` 或更上层 `ExecutionService`，不得拼接 Docker CLI。
Docker Provider 使用结构化 argv 调用 `docker`，不得经过 shell。

#### 18.3.3 执行容器安全配置

容器模板至少包含：

```text
docker create
  --label coding-agent.managed=true
  --label coding-agent.host=<host_instance_id>
  --label coding-agent.operation=<operation_id>
  --label coding-agent.tool-run=<tool_run_id>
  --label coding-agent.owner=<attempt_id-or-operation_id>
  --network none
  --read-only
  --cap-drop ALL
  --security-opt no-new-privileges
  --pids-limit <budget>
  --memory <budget>
  --memory-swap <same-as-memory>
  --cpus <budget>
  --ulimit nofile=<budget>:<budget>
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=<budget>
  --tmpfs /home/agent:rw,nosuid,nodev,size=<small-budget>
  --mount type=bind,src=<workspace>,dst=/workspace,rw
  --mount type=bind,src=<trusted-git-mask>,dst=/workspace/.git,readonly
  --workdir /workspace/<relative-cwd>
  --user <sandbox-uid>:<sandbox-gid>
  --env HOME=/home/agent
  --env TMPDIR=/tmp
  --env LANG=C.UTF-8
  --stop-timeout <grace-seconds>
  <trusted-image@sha256:digest>
  <argv...>
```

约束：

- 不使用 `--privileged`、host network/PID/IPC、额外 capability 或设备；
- 不挂载 Docker socket、Host `/tmp`、HOME、SSH agent 或凭证目录；
- 不继承 Host 环境变量，只允许固定 allowlist；
- 不使用用户仓库提供的 Dockerfile 构建执行镜像；
- 保留 Docker 默认 seccomp；不得使用 `seccomp=unconfined`；
- Linux 可额外启用 AppArmor/SELinux，macOS 不宣称由 Host MAC 直接保护容器；
- 镜像 entrypoint 不得是 shell wrapper，命令使用原始 argv；
- 容器名称只使用内部 ID，不拼接用户输入。

`trusted-git-mask` 由 Host 创建：

- 普通仓库的 `.git` 是目录时，挂载一个只读空目录；
- worktree 的 `.git` 是文件时，挂载一个只读空文件；
- workspace 内不存在 `.git` 时不创建目标，也不添加该 mount；
- Host Git 操作始终在容器外执行；
- 若平台无法可靠遮蔽 `.git`，Provider 预检失败。

#### 18.3.4 Workspace 边界

MVP 保留 bind mount，以控制改造范围：

- 主 Agent 命令挂载当前 workspace root；
- 子 Agent 命令只挂载对应 Attempt worktree；
- workspace 外不提供任何 Host bind mount；
- `cwd` 先经过现有 `PathGuard`，再映射为容器内 `/workspace/<relative-path>`；
- 容器只看到 workspace 视图，不看到 repo root 中 workspace 之外的目录；
- 命令前记录 workspace baseline digest/status；
- 命令后记录 changed paths、大小变化和 Git diff；
- 超出 TaskContract scope 的变化由验收层拒绝，而不是由 OCI mount 假装可精确限制；
- 容器异常退出或破坏 worktree 时，使用现有 base commit/snapshot 恢复或丢弃 Attempt。

这是 MVP 最大的安全妥协：workspace 本身是可写 Host bind mount。容器可以删除或任意修改
该 workspace，但不能访问其他 Host 路径。主 Agent 的可恢复快照和子 Agent 的独立 worktree
负责限制破坏范围。

#### 18.3.5 资源限制

MVP 默认预算建议：

| 资源 | 默认值 | 硬上限 | 强制方式 |
|---|---:|---:|---|
| CPU | 2 vCPU | 4 vCPU | Docker `--cpus` |
| 内存 | 2 GiB | 8 GiB | `--memory`，swap 等于 memory |
| PID | 256 | 1024 | `--pids-limit` |
| 文件描述符 | 4096 | 16384 | `--ulimit nofile` |
| 单命令 wall time | 120s | 1800s | Host monotonic timer |
| `/tmp` | 512 MiB | 2 GiB | Docker tmpfs size |
| `/home/agent` | 64 MiB | 256 MiB | Docker tmpfs size |
| stdout | 10 MiB | 50 MiB | Host 流式计数 |
| stderr | 10 MiB | 50 MiB | Host 流式计数 |
| workspace 增量 | 1 GiB | 5 GiB | Host watchdog + 终态检查 |
| 并行容器 | 每 Session 2 | Host 配置上限 | Admission semaphore |

预算来源：

```text
organization/host ceiling
  ∩ AgentConfig defaults
  ∩ TaskContract requested budget
  ∩ user-approved increase
```

MVP 必须实现：

- 容器创建前原子获取全局和 Session execution slot；
- CPU、内存、PID、ulimit 与 tmpfs 作为 Docker 硬限制；
- wall time 到期后 `docker stop --time <grace>`，随后 `docker kill`；
- stdout/stderr 超过上限后停止保存，若进程持续高速输出则取消容器；
- 命令前后计算 workspace 大小，并由 Host watchdog 周期采样；
- 运行期间周期调用 Docker stats API/CLI，采集 CPU、memory current/peak、PID 和 I/O；
- 所有 slot、计时器和监控任务在终态释放；
- `docker inspect` 的 OOM、exit、时间和状态进入 ResourceUsage。

bind mount 的 workspace 增量监控不是原子磁盘配额，恶意进程可能在两个采样周期之间快速
写满 Host 磁盘。因此 OCI MVP 只能宣称“有界检测与快速终止”，不能宣称 workspace 磁盘
硬隔离。V2 私有 workspace 必须使用独立 quota volume、tmpfs 或受限 overlay 才能关闭该
风险。

#### 18.3.6 输出、退出与结果映射

Provider 必须流式读取 stdout/stderr，不能在命令完成后一次性加载：

- 每个 chunk 带 stream、cursor、timestamp 和 execution ID；
- UI 继续复用现有 `tool.output` SSE；
- 完整输出进入 Artifact Store，ToolMessage 只保存有界摘要和引用；
- UTF-8 解码错误使用 replacement，不中断进程管理；
- Docker attach/CLI 错误与容器进程退出码分开记录；
- `137 + OOMKilled=true` 映射为 `SANDBOX_MEMORY_LIMIT`；
- Host deadline 触发映射为 `SANDBOX_EXEC_TIMEOUT`；
- 用户取消映射为 `SANDBOX_CANCELLED`；
- 非零退出码仍是成功完成的 command result，不自动当成 Provider failure。

`CommandResult` 增加：

```text
sandbox_id
provider
container_id
image_digest
isolation_level
termination_reason
resource_usage
policy_decision_id
```

#### 18.3.7 取消与清理

取消必须以容器为边界：

1. ToolRun CAS 进入 `cancelling`。
2. 设置现有 cancellation event。
3. Provider 执行 `docker stop`。
4. 宽限期后仍运行则执行 `docker kill`。
5. 等待容器进入 exited/dead。
6. 采集终态 inspect 和 ResourceUsage。
7. 执行 `docker rm --force`。
8. 释放 admission slot。
9. ToolRun 进入 `cancelled`。

即使调用线程被取消或抛异常，`remove()` 也必须在 `finally` 中执行。若 daemon 暂时不可用，
写入 `cleanup_pending`，由 Reconciler 重试。

#### 18.3.8 重启恢复与孤儿回收

每个容器必须有稳定 labels。Host 启动时：

1. 生成新的 `host_instance_id`。
2. 通过 `docker ps -a --filter label=coding-agent.managed=true` 枚举容器。
3. 与 SQLite 中非终态 SandboxExecution 对账。
4. 旧 Host instance 创建且无有效 lease 的容器一律 stop/kill/remove。
5. 当前记录对应但 ToolRun 已终态的容器一律 remove。
6. 清理失败保留审计记录并周期重试。

MVP 不恢复容器内正在运行的命令。Host 崩溃后的相关 ToolRun 标记为
`SANDBOX_LOST`，修复未匹配 tool call 后由 Agent 决定是否重新执行，避免重复外部副作用。

#### 18.3.9 全路径接入

实现必须提供唯一的 `SandboxExecutionGate`。校验分为两级：

1. Runtime 打开 workspace 时执行 Provider、Linux container mode、资源能力和镜像预检，
   将 `ProviderHealth` 写入 SQLite；
2. 每次模型可控命令执行前再次经过 Gate，依次校验 `CommandPolicy`、审批结果、`cwd`
   路径边界、资源准入和 Provider health，再生成不可复用的 policy decision ID。

第二级校验不能只在 Agent/Attempt 启动时执行一次，因为同一轮中的命令、路径和资源请求
各不相同。`sandbox_enabled=false` 的语义是禁止模型命令执行，不是启用 Host fallback。

以下命令执行入口必须统一进入 `OciExecutionService`：

- 主 Agent `run_command`；
- 子 Agent `run_command`；
- Skill 声明的本地 command；
- 测试、构建、lint 和格式化命令；
- 后续新增的会加载仓库代码的本地工具。

以下可信控制面命令可暂时保留 Host 执行，但必须使用内部 API，不能暴露给模型：

- `git status/diff/archive/apply/update-ref/worktree`；
- Docker Provider 自身的 `docker create/start/inspect/stop/rm`；
- Artifact Store 和 SQLite 操作；
- Provider health check。

代码库需要维护一份 Host subprocess allowlist。验收时搜索所有 `subprocess` 调用，证明没有
模型可控的 argv 绕过 `OciExecutionService`。

#### 18.3.10 配置与用户可见状态

建议新增配置：

```text
sandbox_enabled: true
sandbox_provider: docker
sandbox_image: coding-agent-sandbox@sha256:...
sandbox_cpu_limit: 2.0
sandbox_memory_bytes: 2147483648
sandbox_pid_limit: 256
sandbox_tmpfs_bytes: 536870912
sandbox_workspace_growth_bytes: 1073741824
sandbox_max_parallel_per_session: 2
sandbox_max_parallel_global: 4
sandbox_cleanup_grace_seconds: 3
```

安全配置在活跃 ToolRun 期间不可修改。UI 至少显示：

- `OCI 沙箱` 或 `Provider 不可用`；
- Docker context 和 rootless 状态；
- 每个 ToolRun 的容器状态、CPU/内存峰值和终止原因；
- 超时、OOM、PID、输出或 workspace 增量超限；
- 当前是 `host_bind`，不是私有 workspace。

不得使用容易误导的“完全安全”或“VM 级隔离”文案。

#### 18.3.11 可信基础镜像

MVP 不承诺运行任意语言或任意项目。首个镜像只覆盖当前 Coding Agent 仓库的验证需求：

- 与项目兼容的 Python 3.11+；
- `uv`、pytest、ruff 和项目锁文件对应的 Python 依赖；
- 与 Web 项目兼容的 Node.js/npm；
- `rg`、Git 客户端和必要的基础构建工具；
- 一个非 root `agent` 用户；
- `/home/agent`、`/tmp`、`/workspace` 挂载点；
- 默认无 daemon、SSH server、Docker CLI 和云平台 CLI。

镜像要求：

- Dockerfile 位于 Coding Agent 自身受信目录并经过代码审查，不能从待处理 workspace 读取；
- CI 构建并生成 digest，运行配置只接受 digest；
- 依赖版本来自 `uv.lock`、Web lockfile 或显式版本，不使用浮动 `latest`；
- 镜像内依赖环境位于只读 `/opt`，不能依赖 Host `.venv` 或 `node_modules`；
- 默认设置离线模式，依赖不完整时命令明确失败，不在 MVP 临时开放网络；
- 同时构建 `linux/amd64` 和 `linux/arm64`，避免 Apple Silicon 隐式模拟造成性能和行为差异；
- 镜像 digest、platform 和构建 provenance 写入每个 CommandResult。

仓库依赖变化导致可信镜像过期时，由维护者或受信 CI 重建镜像。让 Agent 在执行阶段修改
镜像、运行 `docker build` 或在线安装系统包不属于 MVP。

### 18.4 P1 可选增强

以下功能有价值，但不阻塞 OCI MVP：

- Linux AppArmor/SELinux 自定义 profile；
- CPU/内存实时曲线和压力指标；
- workspace 增量 watchdog 从轮询升级为文件系统事件；
- 按 workspace 选择受信镜像 profile；
- 只读依赖 cache；
- Cosign 镜像签名验证和 SBOM 展示；
- Docker API SDK 替代 CLI；
- 对命令类型设置不同资源 profile；
- 在 Linux 上使用 project quota 提供 workspace 硬磁盘限制。

### 18.5 明确延后

MVP 不实现：

- gVisor、Kata、Firecracker、E2B 或多 Provider 自动路由；
- containerd、Podman 和 Kubernetes；
- 私有 workspace import/export；
- Agent 自主开通网络、Egress Gateway 和域名 allowlist；
- Credential Broker、Git 凭证和容器内远程 Git；
- pause/resume、snapshot、warm pool；
- Attempt 级常驻容器和跨命令后台服务；
- 容器内 MCP server、浏览器和安全预览；
- 用户自定义镜像或仓库 Dockerfile；
- 自动资源扩容与抢占调度；
- 对明确恶意内核逃逸代码提供 S4 安全承诺。

### 18.6 MVP 状态模型

不新增完整 SandboxInstance 状态机，先将执行容器映射到 ToolRun：

```text
ToolRun QUEUED
  -> WAITING_RESOURCES
  -> PREPARING_CONTAINER
  -> RUNNING
  -> STOPPING
  -> COLLECTING
  -> REMOVING
  -> COMPLETED | FAILED | CANCELLED
```

约束：

- `PREPARING_CONTAINER` 后必须持久化 container ID；
- 只有 `docker rm` 成功或进入 `cleanup_pending` 后才能离开 `REMOVING`；
- ToolRun 终态与 cleanup 状态分离，避免清理故障把用户命令伪装成仍在运行；
- 每次状态更新使用当前 operation/attempt lease epoch；
- 容器事件通过现有 ToolRun event callback 写入 SSE 和 Trace。

### 18.7 稳定错误码

```text
SANDBOX_DISABLED
SANDBOX_DOCKER_CLI_MISSING
SANDBOX_PROVIDER_UNAVAILABLE
SANDBOX_PROVIDER_INCOMPATIBLE
SANDBOX_IMAGE_MISSING
SANDBOX_IMAGE_DIGEST_MISMATCH
SANDBOX_PREPARE_FAILED
SANDBOX_START_FAILED
SANDBOX_EXEC_TIMEOUT
SANDBOX_MEMORY_LIMIT
SANDBOX_PID_LIMIT
SANDBOX_OUTPUT_LIMIT
SANDBOX_WORKSPACE_GROWTH_LIMIT
SANDBOX_RESOURCE_CAPACITY
SANDBOX_CANCELLED
SANDBOX_LOST
SANDBOX_CLEANUP_PENDING
```

错误必须包含 `provider`, `retryable`, `operation_id`, `tool_run_id` 和脱敏后的诊断 Artifact
引用。Docker CLI stderr 不直接完整注入模型上下文。

### 18.8 MVP 验收标准

#### 功能闭环

- 主 Agent、子 Agent 和 Skill command 均在 Docker 容器内执行。
- 正常 stdout/stderr、退出码、超时和取消保持现有 ToolRun 语义。
- `apply_patch -> run_command -> show_diff` 工作流可用。
- 两个只读命令可以并行执行并独立显示。
- Docker 不可用时命令明确失败且不回落 Host。

#### 隔离

- 容器无法读取 Host HOME、SSH、云凭证、模型 Key 和其他 workspace。
- 容器无法访问公网、内网、loopback Host 服务和 metadata 地址。
- 容器不能看到 Docker socket、Host PID、设备和 `.git`。
- `../`、绝对路径和 workspace 内 symlink 不能访问 Host workspace 外文件。
- 命令只能修改当前挂载的 workspace。

#### 资源

- CPU 密集命令被限制在配置核数。
- 内存超过上限时容器被 OOM kill，并返回 `SANDBOX_MEMORY_LIMIT`。
- fork bomb 不能超过 PID limit。
- `/tmp` 和容器 HOME 写满后不能继续增长。
- wall time 到期后所有后代进程消失。
- stdout/stderr 超限不会导致 Host 内存无限增长。
- workspace 快速增长触发 watchdog 和终态失败。
- Session/全局容器数超过上限时进入 `WAITING_RESOURCES`。

#### 恢复与审计

- 用户取消后 P99 5 秒内容器停止。
- Host 进程被 kill 后，重启能发现并删除孤儿容器。
- 每个结果包含 container ID、image digest、资源峰值、终止原因和 Artifact 引用。
- Trace 能关联 operation、ToolRun、container 和 workspace。
- 不存在模型可控的 Host command 执行旁路。

### 18.9 MVP 交付物

```text
coding_agent/sandbox/
  models.py                 # spec、health、observation、usage
  provider.py               # 最小 Protocol
  docker_cli.py             # Docker CLI Provider
  execution.py              # ToolRun 适配、输出、取消
  resources.py              # admission、watchdog、usage
  repository.py             # execution/container/cleanup 状态
  reconciler.py             # 启动对账和孤儿回收

tests/
  unit/test_sandbox_models.py
  unit/test_docker_cli_provider.py
  unit/test_sandbox_resources.py
  integration/test_oci_execution.py
  integration/test_oci_isolation.py
  integration/test_oci_recovery.py
```

文档、配置、UI 状态和 Docker 基础镜像同属 MVP 交付物，不能只提交一个
`DockerExecutionBackend`。

## 19. 分阶段路线

### Phase 0：边界整理

- 定义 `SandboxSpec`、风险输入、策略决策和稳定错误码。
- 把 `HostExecutionBackend` 从工具构造中解耦。
- 为所有工具补全 effect/capability 元数据。
- 在 UI 明确显示“Host 未隔离”或实际隔离等级。

退出条件：所有命令都经过统一 `ExecutionService`，不存在旁路 `subprocess` 执行仓库代码。

### Phase 1：本地 OCI MVP

- 每个命令 ToolRun 一个加固 OCI 执行容器。
- worktree bind mount 过渡方案。
- 空环境变量 allowlist、网络关闭、cgroup CPU/内存/PID 限制、磁盘/tmpfs 配额和强制销毁。
- ToolRun 子执行组、后台进程检测、wall time 和输出上限。
- 持久化 ResourceBudget 和终态 ResourceUsage summary。
- ToolRun 输出/取消映射到容器 exec。
- Sandbox provenance 进入 ResultEnvelope。
- fail-closed Provider health check。
- Docker labels、cleanup state 和 Host 启动孤儿回收。

退出条件：恶意测试无法读取宿主 HOME、访问网络、看到其他 worktree 或在取消后残留进程。

### Phase 2：完整隔离

- 私有 workspace import/export。
- Egress Gateway 和 DNS/metadata 防护。
- Credential/MCP Broker。
- gVisor Provider。
- 持久化 SandboxInstance/Lease/Execution 与 Reconciler。
- 独立验收 Sandbox。

退出条件：外部 PR 可无人值守运行，Host 重启后能准确回收或恢复所有 Sandbox。

### Phase 3：强隔离与规模化

- Kata/Firecracker/托管 microVM Provider。
- warm pool、干净快照、暂停恢复。
- 多租户配额、调度、容量和成本控制。
- 镜像签名、SBOM、漏洞门禁和运行时异常检测。
- 跨 Provider 一致性与逃逸演练。

退出条件：主动恶意代码测试通过，且不存在到宿主、其他租户、控制面和内网的可用路径。

## 20. 验收与安全测试

### 20.1 功能测试

- 创建、Ready、exec、流式输出、导出、销毁全链路。
- 并行 ToolRun 与每 Attempt 隔离。
- 主/子 Agent 取消和超时。
- revision、返工、崩溃恢复和 lease fencing。
- Provider 不可用时不降级。

### 20.2 逃逸与数据测试

- 读取 `~/.ssh`、`~/.aws`、环境变量、其他仓库均失败。
- `../`、symlink、hardlink、`/proc/*/root`、mount namespace 攻击失败。
- Docker/Kubernetes/SSH agent socket 不存在。
- raw TCP/UDP、DNS tunnel、IPv6、metadata 和内网地址失败。
- 超 scope 文件在导出时被拒绝。
- 伪造 ResultEnvelope、exit code 或 artifact hash 被控制面发现。

### 20.3 资源测试

- 多个并行 ToolRun 的合计 CPU/内存不能超过 Attempt hard limit。
- CPU 密集任务被限速，累计 CPU 预算耗尽后得到确定的终止原因。
- fork bomb 被 PID limit 终止。
- 内存与 page cache 超限得到稳定 OOM 领域结果，且 swap 不可无限增长。
- workspace、tmpfs、cache、inode、stdout/stderr、Artifact 和网络流量分别有硬上限。
- I/O 密集任务被限速，不影响同 Host 上其他 Sandbox 的基本可用性。
- `nohup`、double-fork、setsid 和守护进程不能脱离 execution group/cgroup。
- 后台进程在 execution、Attempt 和 Sandbox 终止后全部消失。
- ResourceUsage 峰值、累计值、throttling 和超限事件与 Provider 原始指标一致。
- 容量不足时任务进入 `WAITING_RESOURCES`，不会静默超卖或回落到 Host。
- 取消到进程消失的 P99 满足目标。

### 20.4 安全基线

- 镜像以 digest 固定且签名验证通过。
- 无 privileged、host namespace、额外 capability 和设备。
- rootfs 只读，临时目录配额生效。
- 控制面采集的 spec hash、image digest、policy decision 和网络日志完整。
- 执行后快照不能被其他任务复用。

## 21. SLO 与运营指标

建议初始目标：

| 指标 | V1 目标 | V3 目标 |
|---|---:|---:|
| warm Sandbox Ready P95 | < 2s | < 500ms |
| cold Sandbox Ready P95 | < 10s | < 3s |
| cancel 到进程消失 P99 | < 5s | < 3s |
| destroy 成功率 | > 99.9% | > 99.99% |
| orphan Sandbox 存活时间 | < 5min | < 1min |
| 审计事件丢失 | 0 | 0 |
| 高风险任务错误降级次数 | 0 | 0 |
| CPU/内存超卖 | 0 | 0 |
| 资源终态采样覆盖率 | > 99.9% | > 99.99% |
| 超限原因可判定率 | > 99% | > 99.9% |

运营看板应包含 Provider 容量、创建延迟、失败码、OOM/timeout、网络拒绝、Secret 使用、
孤儿资源、CPU throttling、内存峰值、磁盘与 inode、I/O、后台进程、每任务成本、缓存
命中率和隔离等级分布。

## 22. 关键决策

### ADR-1：worktree 不作为安全边界

接受。worktree 继续负责并发修改和 Git 交付；执行安全由 Sandbox Provider 负责。

### ADR-2：控制面在沙箱外，执行与高风险解析在沙箱内

接受。这样可避免模型/API/MCP 凭证进入不可信环境，同时通过 Broker 收窄能力。

### ADR-3：多后端、风险驱动，不选择单一 Docker 方案

接受。普通容器满足 MVP 和日常性能，gVisor/microVM 满足更高威胁模型。

### ADR-4：网络默认关闭，按目标放行

接受。不能以“用户批准了命令”为理由开放任意网络。

### ADR-5：每 Attempt 一个安全域

接受。并行 ToolRun 可共享同一 Attempt Sandbox；不同 Attempt 默认不共享可写状态。
这是 V2 目标架构；OCI MVP 按 ADR-8 使用更短生命周期的每 ToolRun 容器。

### ADR-6：沙箱只导出 patch 和 Artifact，不直接集成 Git

接受。Git commit、验收和 merge 留在可信控制面。

### ADR-7：V3 优先 Kata/Agent Sandbox，谨慎自建 Firecracker

接受。Firecracker 是底层原语，不是完整 Agent Sandbox 产品；自建需要额外解决镜像、网络、
快照、租约、容量、补丁和应急响应。

### ADR-8：OCI MVP 使用每 ToolRun 一个容器

接受。它牺牲跨命令容器状态，换取可靠的资源限制、后台进程收口、并行执行和取消语义。
Attempt 级常驻 Sandbox 作为 V2 演进，不在 MVP 同时实现。

### ADR-9：本地 macOS 默认使用 Seatbelt Provider

接受。为降低本地依赖和运行成本，macOS MVP 默认通过 `sandbox-exec` 应用动态生成的
Seatbelt profile。策略默认拒绝网络，只允许读取系统运行时、显式工具链目录和当前
workspace，只允许写当前 workspace（排除 `.git`）及每次执行独立的临时 HOME。

Seatbelt Provider 继续经过统一的 `SandboxExecutionGate`、资源准入、输出限制、超时、
协作取消、SQLite 审计和 ResultEnvelope 溯源。启动预检必须实际应用最小 profile；
二进制存在但 profile 无法应用时仍视为不可用，并且不得回退 Host 执行。

该 Provider 不承诺 CPU、内存、PID 硬限制、内核隔离或 Host 崩溃后的可靠孤儿进程回收。
因此隔离等级必须明确记录为 `seatbelt`，不能宣称等价于 OCI。Docker Provider 和后续
gVisor/microVM 路线继续保留，用于更高威胁等级。

## 23. 最终建议

当前系统最合理的演进不是直接新增 `DockerExecutionBackend` 后结束，而是先稳定
`SandboxSpec + SandboxManager + SandboxProvider + SandboxLease` 这组领域边界。V1 的 OCI
实现只是第一个 Provider。

短期采用：

```text
Main/Subagent Runtime
  -> SandboxManager
  -> hardened OCI execution container per ToolRun
  -> network none
  -> isolated worktree mount
  -> trusted diff/commit/review
```

中期演进为：

```text
policy-driven routing
  -> private workspace
  -> gVisor for unknown code
  -> brokered network/credentials/MCP
  -> persistent leases and reconciliation
```

生产高风险场景采用：

```text
Kubernetes Agent Sandbox
  -> gVisor or Kata RuntimeClass
  -> warm pool + TTL
  -> independent verifier sandbox
  -> full provenance and policy evidence
```

这条路线与当前 Task/Attempt、异步 ToolRun、协作取消、worktree 交付和 ResultEnvelope 设计
兼容，并能避免把本地开发便利性误当成对主动攻击者的安全承诺。
