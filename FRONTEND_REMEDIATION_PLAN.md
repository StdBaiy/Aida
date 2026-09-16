# 前端问题与修复计划

## 目标

让 Web 工作台在多 session 并发、切换、取消、审批、恢复和断线重连场景下保持状态隔离，
并让界面展示与服务端持久化状态一致。

## 修复原则

- 服务端 Session、Turn、Operation 和持久化事件是唯一事实来源。
- 所有运行时 UI 状态必须按 `session_id` 隔离。
- 所有异步响应必须校验所属 session 或请求代次，不能覆盖更新后的选择。
- SSE 必须支持游标恢复、事件去重和明确终态。
- 高风险状态转换必须有后端单元测试和 Playwright 竞态测试。

## P0：数据正确性

### 1. Restore 会话绑定

- [x] 后台 restore 捕获固定的 `session_id` 和 runner/coordinator。
- [x] restore 执行过程不再读取可变的全局 selected session。
- [x] restore 运行期间切换或新建 session 不会改变恢复目标。
- [x] 增加 restore/switch 竞态回归测试。

### 2. 审批归属

- [x] pending approval 响应包含 `session_id`。
- [x] 前端只展示当前 session 的审批。
- [x] session 切换后清除旧 session 审批并恢复目标 session 审批。
- [x] 增加两个 session 同时运行时的审批隔离测试。

## P1：多 Session 状态隔离

- [x] 将 operation、running、cancelling、stream text、events、tool runs 和 error 按 session 保存。
- [x] 每个 session 保存独立输入草稿。
- [x] session 切换不丢失本地实时状态。
- [x] 快速连续切换使用串行选择队列，最后一次选择获胜。
- [x] session 列表展示 queued、running、waiting approval、cancelling、committing 等状态。
- [x] 后台 session 完成后刷新 session 摘要、Diff 和 workspace 状态。

## P1：SSE 与恢复

- [x] 所有 operation 成功结束时都有明确终止事件。
- [x] 客户端保存每个 operation 的最后事件游标。
- [x] 重连或重新订阅时从游标继续，不从头重复拼接。
- [x] 按 `operation_id + sequence` 去重并拒绝乱序事件。
- [x] 网络断开显示重连状态，Host 重启产生可回放的恢复失败终态。
- [x] JSON 解析失败不会破坏整个订阅状态机。

## P2：现有交互正确性

- [x] workspace 忙时禁用 restore，并说明原因。
- [x] Diff、文件和工具输出明确显示截断状态。
- [x] 流式 token 合并刷新，避免逐 token 解析 Markdown。
- [x] 用户主动离开底部后停止强制自动滚动。
- [x] 查询失败显示局部错误和重试入口。
- [x] 长对话、事件和大文件视图采用分页或上限策略。

## P3：未实现能力

- [ ] Session 搜索、重命名和删除。
- [ ] 附件输入。
- [ ] Agent 模式与自动模式。
- [ ] 安全预览。
- [ ] Problems/诊断聚合。
- [ ] Git commit 与统一历史恢复入口。
- [ ] Artifact 查看和完整输出下载。
- [ ] 通用子 Agent 创建、反馈、授权和验收操作。

## 测试矩阵

- [x] A/B session 同时运行并反复切换。
- [x] 后台 session 等待审批，当前 session 不串审批。
- [x] restore 期间切换和新建 session。
- [x] 快速连续点击多个 session。
- [x] SSE 断线、重放、重复、坏消息和乱序事件。
- [ ] 多浏览器标签页选择竞争。
- [x] 后台任务完成后的摘要、Diff 和工作区刷新。
- [x] 长输出、截断、离线和恢复状态。

## 当前基线

- `npm run build`：通过。
- `npm test -- --reporter=line`：16 条 Playwright 用例通过。
- `uv run pytest -q`：133 条通过，1 条跳过；测试完成后的外部 `.bytesec` hook
  因沙箱权限返回非零退出码，不影响 pytest 结果。
- 多 session 切换、审批隔离、restore 竞态、SSE 游标恢复与截断展示已有回归覆盖。
- P3 条目是新增产品能力，保留为后续迭代，不属于本轮正确性修复。
