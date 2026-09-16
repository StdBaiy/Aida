import { expect, test, type Page } from "@playwright/test";

async function mockWorkbench(
  page: Page,
  overrides: Record<string, unknown> = {},
) {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const responses: Record<string, unknown> = {
      "/api/v1/status": {
        service: "ready",
        workspace: "/tmp/test",
        workspace_name: "test",
        branch: "main",
        model: "test-model",
        session_id: "active",
        timeline_id: "timeline",
        csrf_token: "csrf",
        active_operation: null,
        pending_approvals: [],
      },
      "/api/v1/workspaces": { active: "/tmp/test", recent: [] },
      "/api/v1/sessions": { items: [], next_offset: null },
      "/api/v1/sessions/active/turns": {
        timeline_id: "timeline",
        turns: [],
        next_before_turn_number: null,
      },
      "/api/v1/workspace/status": {
        branch: "main",
        changed_file_count: 0,
        added_lines: 0,
        deleted_lines: 0,
        clean: true,
      },
      "/api/v1/workspace/diff": { files: [] },
      ...overrides,
    };
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(responses[path] ?? {}),
    });
  });
}

test("desktop renders the complete three-column workbench", async ({ page }) => {
  await mockWorkbench(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");
  await expect(page).toHaveTitle("Coding Agent");
  await expect(page.locator(".sidebar")).toBeVisible();
  await expect(page.locator(".conversation")).toBeVisible();
  await expect(page.locator(".inspector")).toBeVisible();
  await expect(page.getByText("预览", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "提交改动" })).toBeDisabled();

  const dimensions = await page.evaluate(() => ({
    width: document.documentElement.scrollWidth,
    height: document.documentElement.scrollHeight,
    viewportWidth: window.innerWidth,
    viewportHeight: window.innerHeight,
  }));
  expect(dimensions.width).toBe(dimensions.viewportWidth);
  expect(dimensions.height).toBe(dimensions.viewportHeight);
  await page.screenshot({ path: "test-results/desktop-workbench.png", fullPage: true });
});

test("mobile switches between conversation and workspace without overflow", async ({ page }) => {
  await mockWorkbench(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.locator(".conversation")).toBeVisible();
  await expect(page.locator(".inspector")).toBeHidden();

  await page.getByRole("button", { name: "工作区" }).click();
  await expect(page.locator(".conversation")).toBeHidden();
  await expect(page.locator(".inspector")).toBeVisible();

  const dimensions = await page.evaluate(() => ({
    width: document.documentElement.scrollWidth,
    height: document.documentElement.scrollHeight,
    viewportWidth: window.innerWidth,
    viewportHeight: window.innerHeight,
  }));
  expect(dimensions.width).toBe(dimensions.viewportWidth);
  expect(dimensions.height).toBe(dimensions.viewportHeight);
  await page.screenshot({ path: "test-results/mobile-workspace.png", fullPage: true });
});

test("marks truncated diffs and file previews", async ({ page }) => {
  await mockWorkbench(page, {
    "/api/v1/workspace/diff": {
      files: [
        {
          path: "large.txt",
          status: "M",
          additions: 1,
          deletions: 0,
          patch: "+partial",
          truncated: true,
        },
      ],
    },
    "/api/v1/workspace/files": {
      files: [{ path: "large.txt", size: 2_000_000 }],
    },
    "/api/v1/workspace/files/content": {
      path: "large.txt",
      sha256: "abc",
      byte_size: 2_000_000,
      binary: false,
      truncated: true,
      content: "partial content",
    },
  });

  await page.goto("/");
  await expect(page.getByText("仅显示前 1 MiB")).toBeVisible();
  await page.getByRole("button", { name: "文件", exact: true }).click();
  await page.getByText("large.txt", { exact: true }).click();
  await expect(page.getByText("内容已截断")).toBeVisible();
});

test("filters empty tasks, right-aligns users, and renders agent markdown", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const responses: Record<string, unknown> = {
      "/api/v1/status": {
        service: "ready",
        workspace: "/tmp/test",
        workspace_name: "test",
        branch: "main",
        model: "test-model",
        session_id: "active",
        timeline_id: "timeline",
        csrf_token: "csrf",
        active_operation: null,
        pending_approvals: [],
      },
      "/api/v1/sessions": {
        items: [
          {
            session_id: "empty",
            active_timeline_id: "empty-timeline",
            title: "空任务",
            turn_count: 0,
            created_at: "2026-09-10T12:00:00Z",
          },
          {
            session_id: "active",
            active_timeline_id: "timeline",
            title: "实现 Markdown",
            turn_count: 1,
            created_at: "2026-09-10T12:00:00Z",
          },
        ],
        next_offset: null,
      },
      "/api/v1/sessions/active/turns": {
        timeline_id: "timeline",
        turns: [
          {
            turn_id: "turn-1",
            turn_number: 1,
            user_text: "请输出 Markdown",
            assistant_text: "## 结果\n\n- 列表项\n\n```ts\nconst ok = true;\n```",
            created_at: "2026-09-10T12:00:00Z",
            snapshot_oid: "1234567890abcdef",
          },
        ],
      },
      "/api/v1/workspace/status": {
        branch: "main",
        changed_file_count: 0,
        added_lines: 0,
        deleted_lines: 0,
        clean: true,
      },
      "/api/v1/workspace/diff": { files: [] },
    };
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(responses[path] ?? {}),
    });
  });

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");
  await expect(page.getByText("空任务", { exact: true })).toHaveCount(0);
  await expect(page.getByText("实现 Markdown", { exact: true })).toBeVisible();
  await expect(page.locator(".assistant-copy h2")).toHaveText("结果");
  await expect(page.locator(".assistant-copy li")).toHaveText("列表项");
  await expect(page.locator(".assistant-copy pre code")).toContainText("const ok = true");

  const userLayout = await page.locator(".user-message").evaluate((element) => ({
    columns: getComputedStyle(element).gridTemplateColumns,
    avatarIsLast: element.lastElementChild?.classList.contains("message-avatar"),
  }));
  expect(userLayout.avatarIsLast).toBe(true);
  expect(userLayout.columns.split(" ").at(-1)).toBe("28px");
});

test("new session is created on first input while the previous session runs", async ({
  page,
}) => {
  let createSessionPosts = 0;
  let createTurnPosts = 0;
  let createdTurnsGets = 0;
  let operationCommitted = false;
  let createdSessionSelected = false;
  let releaseOperationEvents: () => void = () => {};
  let releaseOldOperation: () => void = () => {};
  const operationEventsResponse = new Promise<void>((resolve) => {
    releaseOperationEvents = resolve;
  });
  const oldOperationResponse = new Promise<void>((resolve) => {
    releaseOldOperation = resolve;
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && path === "/api/v1/sessions") {
      createSessionPosts += 1;
      createdSessionSelected = true;
      releaseOldOperation();
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          session_id: "created",
          active_timeline_id: "created-timeline",
          created_at: "2026-09-10T12:00:00Z",
        }),
      });
      return;
    }
    if (request.method() === "POST" && path === "/api/v1/sessions/created/turns") {
      createTurnPosts += 1;
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({ operation_id: "operation-1" }),
      });
      return;
    }
    if (request.method() === "GET" && path === "/api/v1/sessions/created/turns") {
      createdTurnsGets += 1;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          timeline_id: "created-timeline",
          turns: operationCommitted
            ? [
                {
                  turn_id: "turn-created",
                  turn_number: 1,
                  user_text: "第一条消息",
                  assistant_text: "模型回复",
                  created_at: "2026-09-10T12:00:00Z",
                  snapshot_oid: "abcdef1234567890",
                },
              ]
            : [],
          next_before_turn_number: null,
        }),
      });
      return;
    }
    if (path === "/api/v1/operations/operation-1/events") {
      await operationEventsResponse;
      operationCommitted = true;
      await route.fulfill({
        contentType: "text/event-stream",
        body:
          "id: 1\n" +
          "event: turn.committed\n" +
          'data: {"operation_id":"operation-1","sequence":1}\n\n' +
          "id: 2\n" +
          "event: operation.completed\n" +
          'data: {"operation_id":"operation-1","sequence":2}\n\n',
      });
      return;
    }
    if (path === "/api/v1/operations/old-operation/events") {
      await oldOperationResponse;
      await route.fulfill({
        contentType: "text/event-stream",
        body: "",
      });
      return;
    }
    const responses: Record<string, unknown> = {
      "/api/v1/status": {
        service: "ready",
        workspace: "/tmp/test",
        workspace_name: "test",
        branch: "main",
        model: "test-model",
        session_id: createdSessionSelected ? "created" : "active",
        timeline_id: createdSessionSelected ? "created-timeline" : "timeline",
        csrf_token: "csrf",
        active_operation: createdSessionSelected
          ? null
          : {
              operation_id: "old-operation",
              session_id: "active",
              status: "running",
              kind: "turn",
            },
        active_operations: [
          {
            operation_id: "old-operation",
            session_id: "active",
            status: "running",
            kind: "turn",
          },
        ],
        pending_approvals: [],
      },
      "/api/v1/sessions": {
        items: [
          {
            session_id: "active",
            active_timeline_id: "timeline",
            title: "已有任务",
            turn_count: 1,
            created_at: "2026-09-10T12:00:00Z",
          },
        ],
        next_offset: null,
      },
      "/api/v1/sessions/active/turns": {
        timeline_id: "timeline",
        turns: [
          {
            turn_id: "turn-1",
            turn_number: 1,
            user_text: "已有任务",
            assistant_text: "已完成",
            created_at: "2026-09-10T12:00:00Z",
            snapshot_oid: "1234567890abcdef",
          },
        ],
      },
      "/api/v1/sessions/active/subagent-runs": {
        runs: [
          {
            run_id: "old-run",
            session_id: "active",
            status: "completed",
            base_commit: "base",
            created_at: "2026-09-10T12:00:00Z",
            ended_at: "2026-09-10T12:01:00Z",
            turn_id: null,
            expected_task_count: 0,
            tasks: [],
          },
        ],
      },
      "/api/v1/workspace/status": {
        branch: "main",
        changed_file_count: 0,
        added_lines: 0,
        deleted_lines: 0,
        clean: true,
      },
      "/api/v1/workspace/diff": { files: [] },
    };
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(responses[path] ?? {}),
    });
  });

  await page.goto("/");
  await expect(page.locator(".subagent-demo")).toHaveCount(1);
  await expect(page.getByTitle("停止当前任务")).toBeVisible();
  const newTask = page.getByRole("button", { name: "新建任务" });
  await newTask.click();
  expect(createSessionPosts).toBe(0);
  await expect(page.locator(".session-row")).toHaveCount(1);
  await expect(page.locator(".session-row").filter({ hasText: "已有任务" }).locator(".spin"))
    .toBeVisible();
  await expect(page.locator(".subagent-demo")).toHaveCount(0);
  await expect(page.getByText("从一个具体任务开始")).toBeVisible();

  await page.getByPlaceholder("描述你希望 Agent 完成的任务").fill("第一条消息");
  await page.getByTitle("发送任务").click();
  await expect.poll(() => createSessionPosts).toBe(1);
  await expect(page.locator(".session-row").filter({ hasText: "第一条消息" })).toBeVisible();
  await expect(page.locator(".user-message")).toContainText("第一条消息");
  await expect.poll(() => createTurnPosts).toBe(1);
  await expect.poll(() => createdTurnsGets).toBeGreaterThan(0);
  await expect(page.locator(".user-message")).toContainText("第一条消息");
  await expect(page.getByText("正在处理")).toBeVisible();

  releaseOperationEvents();
  await expect(page.getByText("模型回复")).toBeVisible();
  await expect(page.locator(".user-message")).toHaveCount(1);
});

test("settings apply non-secret config immediately", async ({ page }) => {
  let applied: Record<string, unknown> | null = null;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/settings" && request.method() === "POST") {
      applied = request.postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          ...applied,
          model_api_key_configured: true,
          langsmith_api_key_configured: true,
        }),
      });
      return;
    }
    const responses: Record<string, unknown> = {
      "/api/v1/status": {
        service: "ready",
        workspace: "/tmp/test",
        workspace_name: "test",
        branch: "main",
        model: "old-model",
        session_id: "active",
        timeline_id: "timeline",
        csrf_token: "csrf",
        active_operation: null,
        pending_approvals: [],
      },
      "/api/v1/sessions": { items: [], next_offset: null },
      "/api/v1/sessions/active/turns": { timeline_id: "timeline", turns: [] },
      "/api/v1/workspace/status": {
        branch: "main",
        changed_file_count: 0,
        added_lines: 0,
        deleted_lines: 0,
        clean: true,
      },
      "/api/v1/workspace/diff": { files: [] },
      "/api/v1/settings": {
        model: "old-model",
        base_url: "https://old.example.com/v1",
        langsmith_enabled: false,
        langsmith_project: "old-project",
        model_timeout_seconds: 180,
        command_timeout_seconds: 120,
        max_parallel_sessions: 2,
        model_api_key_configured: true,
        langsmith_api_key_configured: true,
      },
    };
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(responses[path] ?? {}),
    });
  });

  await page.goto("/");
  await page.getByTitle("设置").click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByLabel("模型名称").fill("new-model");
  await page.getByLabel("Base URL").fill("https://new.example.com/v1");
  await page.getByLabel("模型调用超时").fill("90");
  await page.getByLabel("命令超时").fill("240");
  await page.getByLabel("并行 Session").fill("6");
  await page.locator(".settings-switch-row input").check();
  await page.getByLabel("LangSmith 项目").fill("new-project");
  await page.getByRole("button", { name: "应用", exact: true }).click();

  await expect.poll(() => applied?.model).toBe("new-model");
  expect(applied).toEqual({
    model: "new-model",
    base_url: "https://new.example.com/v1",
    langsmith_enabled: true,
    langsmith_project: "new-project",
    model_timeout_seconds: 90,
    command_timeout_seconds: 240,
    max_parallel_sessions: 6,
  });
  await expect(page.getByRole("dialog")).toBeHidden();
  await expect(page.locator(".statusbar")).toContainText("new-model");
});

test("desktop inspector collapses persistently while mobile tabs remain independent", async ({
  page,
}) => {
  await mockWorkbench(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");
  await page.evaluate(() => localStorage.removeItem("coding-agent.inspector-open"));
  await page.reload();
  const conversation = page.locator(".conversation");
  const expandedWidth = await conversation.evaluate((element) => element.clientWidth);

  await page.getByTitle("收起工作区").click();
  await expect(page.locator(".inspector")).toBeHidden();
  await expect
    .poll(() => conversation.evaluate((element) => element.clientWidth))
    .toBeGreaterThan(expandedWidth);
  await expect.poll(() => page.evaluate(() => localStorage.getItem(
    "coding-agent.inspector-open",
  ))).toBe("false");

  await page.reload();
  await expect(page.locator(".inspector")).toBeHidden();
  await expect(page.getByTitle("展开工作区")).toBeVisible();

  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await expect(page.locator(".inspector-toggle")).toBeHidden();
  await page.getByRole("button", { name: "工作区" }).click();
  await expect(page.locator(".inspector")).toBeVisible();
});

test("approval is an inline panel and does not take focus", async ({ page }) => {
  let decision = "";
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/approvals/approval-1/decision") {
      decision = (request.postDataJSON() as { decision: string }).decision;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ decision, replayed: false }),
      });
      return;
    }
    const responses: Record<string, unknown> = {
      "/api/v1/status": {
        service: "ready",
        workspace: "/tmp/test",
        workspace_name: "test",
        branch: "main",
        model: "test-model",
        session_id: "active",
        timeline_id: "timeline",
        csrf_token: "csrf",
        active_operation: null,
        pending_approvals: [
          {
            approval_id: "approval-1",
            operation_id: "operation-1",
            session_id: "active",
            request_hash: "hash-1",
            request: {
              name: "run_command",
              args: {
                argv: ["python", "-m", "pytest"],
                cwd: ".",
                timeout_seconds: 120,
              },
            },
          },
        ],
      },
      "/api/v1/sessions": { items: [], next_offset: null },
      "/api/v1/sessions/active/turns": { timeline_id: "timeline", turns: [] },
      "/api/v1/workspace/status": {
        branch: "main",
        changed_file_count: 0,
        added_lines: 0,
        deleted_lines: 0,
        clean: true,
      },
      "/api/v1/workspace/diff": { files: [] },
    };
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(responses[path] ?? {}),
    });
  });

  await page.goto("/");
  const composer = page.getByPlaceholder("描述你希望 Agent 完成的任务");
  await composer.focus();
  await expect(composer).toBeFocused();
  await expect(page.locator(".inline-approval")).toBeVisible();
  await expect(page.locator(".modal-backdrop")).toHaveCount(0);
  await expect(page.locator(".inline-approval")).toContainText("python -m pytest");
  await page.getByRole("button", { name: "拒绝" }).click();
  await expect.poll(() => decision).toBe("reject");
});

test("keeps runtime state and approvals isolated across sessions", async ({
  page,
}) => {
  let selectedSession = "session-a";
  let operationEventRequests = 0;
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const selectedMatch = path.match(/^\/api\/v1\/sessions\/([^/]+)\/select$/);
    if (request.method() === "POST" && selectedMatch) {
      selectedSession = selectedMatch[1];
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          session_id: selectedSession,
          active_timeline_id: `${selectedSession}-timeline`,
          title: selectedSession,
          turn_count: 0,
          created_at: "2026-09-16T00:00:00Z",
        }),
      });
      return;
    }
    if (path === "/api/v1/operations/operation-a/events") {
      operationEventRequests += 1;
      await route.fulfill({
        contentType: "text/event-stream",
        body:
          operationEventRequests === 1
            ? 'id: 1\nevent: assistant.delta\ndata: {"text":"A 后台输出"}\n\n'
            : "",
      });
      return;
    }
    const sessions = ["session-a", "session-b"].map((id) => ({
      session_id: id,
      active_timeline_id: `${id}-timeline`,
      title: id === "session-a" ? "任务 A" : "任务 B",
      turn_count: 0,
      created_at: "2026-09-16T00:00:00Z",
    }));
    const responses: Record<string, unknown> = {
      "/api/v1/status": {
        service: "ready",
        workspace: "/tmp/test",
        workspace_name: "test",
        branch: "main",
        model: "test-model",
        session_id: selectedSession,
        timeline_id: `${selectedSession}-timeline`,
        csrf_token: "csrf",
        active_operation:
          selectedSession === "session-a"
            ? {
                operation_id: "operation-a",
                session_id: "session-a",
                status: "running",
                kind: "turn",
              }
            : null,
        active_operations: [
          {
            operation_id: "operation-a",
            session_id: "session-a",
            status: "running",
            kind: "turn",
          },
        ],
        pending_approvals: [
          {
            approval_id: "approval-a",
            operation_id: "operation-a",
            session_id: "session-a",
            request_hash: "hash-a",
            request: {
              name: "run_command",
              args: { argv: ["npm", "test"] },
            },
          },
        ],
      },
      "/api/v1/workspaces": { active: "/tmp/test", recent: [] },
      "/api/v1/sessions": { items: sessions, next_offset: null },
      "/api/v1/sessions/session-a/turns": {
        timeline_id: "session-a-timeline",
        turns: [],
        next_before_turn_number: null,
      },
      "/api/v1/sessions/session-b/turns": {
        timeline_id: "session-b-timeline",
        turns: [],
        next_before_turn_number: null,
      },
      "/api/v1/workspace/status": {
        branch: "main",
        changed_file_count: 0,
        added_lines: 0,
        deleted_lines: 0,
        clean: true,
      },
      "/api/v1/workspace/diff": { files: [] },
    };
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(responses[path] ?? {}),
    });
  });

  await page.goto("/");
  await expect(page.getByText("A 后台输出")).toBeVisible();
  await expect(page.locator(".inline-approval")).toContainText("npm test");
  await expect(
    page.locator(".session-row").filter({ hasText: "任务 A" }),
  ).toContainText("等待审批");

  await page.getByText("任务 B", { exact: true }).click();
  const composer = page.getByPlaceholder("描述你希望 Agent 完成的任务");
  await expect(page.locator(".inline-approval")).toHaveCount(0);
  await composer.fill("B 的草稿");

  await page.getByText("任务 A", { exact: true }).click();
  await expect(page.getByText("A 后台输出")).toBeVisible();
  await expect(page.locator(".inline-approval")).toContainText("npm test");

  await page.getByText("任务 B", { exact: true }).click();
  await expect(composer).toHaveValue("B 的草稿");
  await expect(page.locator(".inline-approval")).toHaveCount(0);
});

test("rapid session switches commit the last selection", async ({ page }) => {
  let selectedSession = "session-a";
  const selectionOrder: string[] = [];
  let releaseFirstSelection: () => void = () => {};
  const firstSelection = new Promise<void>((resolve) => {
    releaseFirstSelection = resolve;
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const selectedMatch = path.match(/^\/api\/v1\/sessions\/([^/]+)\/select$/);
    if (request.method() === "POST" && selectedMatch) {
      const id = selectedMatch[1];
      selectionOrder.push(id);
      if (id === "session-b") await firstSelection;
      selectedSession = id;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          session_id: id,
          active_timeline_id: `${id}-timeline`,
          title: id,
          turn_count: 0,
          created_at: "2026-09-16T00:00:00Z",
        }),
      });
      return;
    }
    const sessionItems = ["session-a", "session-b", "session-c"].map((id) => ({
      session_id: id,
      active_timeline_id: `${id}-timeline`,
      title: `任务 ${id.at(-1)?.toUpperCase()}`,
      turn_count: 0,
      created_at: "2026-09-16T00:00:00Z",
    }));
    const responses: Record<string, unknown> = {
      "/api/v1/status": {
        service: "ready",
        workspace: "/tmp/test",
        workspace_name: "test",
        branch: "main",
        model: "test-model",
        session_id: selectedSession,
        timeline_id: `${selectedSession}-timeline`,
        csrf_token: "csrf",
        active_operation: null,
        active_operations: [],
        pending_approvals: [],
      },
      "/api/v1/workspaces": { active: "/tmp/test", recent: [] },
      "/api/v1/sessions": { items: sessionItems, next_offset: null },
      "/api/v1/sessions/session-a/turns": {
        timeline_id: "session-a-timeline",
        turns: [],
        next_before_turn_number: null,
      },
      "/api/v1/sessions/session-b/turns": {
        timeline_id: "session-b-timeline",
        turns: [],
        next_before_turn_number: null,
      },
      "/api/v1/sessions/session-c/turns": {
        timeline_id: "session-c-timeline",
        turns: [],
        next_before_turn_number: null,
      },
      "/api/v1/workspace/status": {
        branch: "main",
        changed_file_count: 0,
        added_lines: 0,
        deleted_lines: 0,
        clean: true,
      },
      "/api/v1/workspace/diff": { files: [] },
    };
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(responses[path] ?? {}),
    });
  });

  await page.goto("/");
  await page.getByText("任务 B", { exact: true }).click();
  await expect.poll(() => selectionOrder).toEqual(["session-b"]);
  await page.getByText("任务 C", { exact: true }).click();
  releaseFirstSelection();

  await expect.poll(() => selectionOrder).toEqual(["session-b", "session-c"]);
  await expect(
    page.locator(".session-row.active").getByText("任务 C", { exact: true }),
  ).toBeVisible();
  expect(selectedSession).toBe("session-c");
});

test("SSE reconnect resumes from its cursor and ignores duplicate events", async ({
  page,
}) => {
  const cursors: string[] = [];
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/operations/operation-1/events") {
      cursors.push(new URL(request.url()).searchParams.get("after") ?? "");
      const attempt = cursors.length;
      await route.fulfill({
        contentType: "text/event-stream",
        body:
          attempt === 1
            ? 'id: 1\nevent: assistant.delta\ndata: {"sequence":1,"text":"片段一"}\n\n'
            : attempt === 2
              ? 'id: 1\nevent: assistant.delta\ndata: {"sequence":1,"text":"片段一"}\n\n' +
                "id: 2\nevent: assistant.delta\ndata: {invalid json}\n\n" +
                'id: 3\nevent: assistant.delta\ndata: {"sequence":3,"text":"片段二"}\n\n'
              : "",
      });
      return;
    }
    const responses: Record<string, unknown> = {
      "/api/v1/status": {
        service: "ready",
        workspace: "/tmp/test",
        workspace_name: "test",
        branch: "main",
        model: "test-model",
        session_id: "active",
        timeline_id: "timeline",
        csrf_token: "csrf",
        active_operation: {
          operation_id: "operation-1",
          session_id: "active",
          status: "running",
          kind: "turn",
        },
        active_operations: [
          {
            operation_id: "operation-1",
            session_id: "active",
            status: "running",
            kind: "turn",
          },
        ],
        pending_approvals: [],
      },
      "/api/v1/workspaces": { active: "/tmp/test", recent: [] },
      "/api/v1/sessions": { items: [], next_offset: null },
      "/api/v1/sessions/active/turns": {
        timeline_id: "timeline",
        turns: [],
        next_before_turn_number: null,
      },
      "/api/v1/workspace/status": {
        branch: "main",
        changed_file_count: 0,
        added_lines: 0,
        deleted_lines: 0,
        clean: true,
      },
      "/api/v1/workspace/diff": { files: [] },
    };
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(responses[path] ?? {}),
    });
  });

  await page.goto("/");
  await expect(page.locator(".streaming")).toContainText("片段一");
  await expect.poll(() => cursors.length, { timeout: 10_000 }).toBeGreaterThan(1);
  await expect(page.locator(".streaming")).toHaveText("片段一片段二");
  expect(cursors).toContain("1");
});

test("empty host opens a recent directory and enters its isolated workspace", async ({
  page,
}) => {
  let openedPath = "";
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/workspaces/open" && request.method() === "POST") {
      openedPath = (request.postDataJSON() as { path: string }).path;
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          service: "ready",
          workspace: openedPath,
          workspace_name: "project-a",
          repo_root: openedPath,
          branch: "main",
          model: "test-model",
          session_id: "project-a-session",
          timeline_id: "project-a-timeline",
          active_operation: null,
          pending_approvals: [],
        }),
      });
      return;
    }
    const responses: Record<string, unknown> = {
      "/api/v1/status": {
        service: "ready",
        workspace: null,
        workspace_name: null,
        repo_root: null,
        branch: null,
        model: "test-model",
        session_id: null,
        timeline_id: null,
        csrf_token: "csrf",
        active_operation: null,
        pending_approvals: [],
      },
      "/api/v1/workspaces": {
        active: null,
        recent: [
          {
            path: "/tmp/project-a",
            name: "project-a",
            active: false,
            available: true,
          },
        ],
      },
      "/api/v1/sessions": { items: [], next_offset: null },
      "/api/v1/sessions/project-a-session/turns": {
        timeline_id: "project-a-timeline",
        turns: [],
      },
      "/api/v1/workspace/status": {
        branch: "main",
        changed_file_count: 0,
        added_lines: 0,
        deleted_lines: 0,
        clean: true,
      },
      "/api/v1/workspace/diff": { files: [] },
    };
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(responses[path] ?? {}),
    });
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "打开工作目录" })).toBeVisible();
  await expect(page.getByText("/tmp/project-a")).toBeVisible();
  await page.getByRole("button", { name: /project-a/ }).click();
  await expect.poll(() => openedPath).toBe("/tmp/project-a");
  await expect(page.locator(".conversation")).toBeVisible();
  await expect(page.locator(".workspace-name")).toHaveText("project-a");
});

test("renders concurrent tool runs as independent live cards", async ({ page }) => {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/v1/operations/operation-1/events") {
      await route.fulfill({
        contentType: "text/event-stream",
        body:
          "id: 1\n" +
          "event: tool.started\n" +
          'data: {"run_id":"run-alpha","tool":"pytest-a","effect":"read_only",' +
          '"execution_group_id":"parallel","status":"queued"}\n\n' +
          "id: 2\n" +
          "event: tool.running\n" +
          'data: {"run_id":"run-alpha","tool":"pytest-a","status":"running"}\n\n' +
          "id: 3\n" +
          "event: tool.output\n" +
          'data: {"run_id":"run-alpha","cursor":1,"stream":"stdout",' +
          '"text":"collecting tests\\n"}\n\n' +
          "id: 4\n" +
          "event: tool.started\n" +
          'data: {"run_id":"run-beta","tool":"pytest-b","effect":"read_only",' +
          '"execution_group_id":"parallel","status":"queued"}\n\n' +
          "id: 5\n" +
          "event: tool.completed\n" +
          'data: {"run_id":"run-beta","tool":"pytest-b","status":"completed",' +
          '"duration_ms":842,"exit_code":0,"output_truncated":true,' +
          '"result_preview":"{\\"ok\\": true}"}\n\n' +
          "id: 6\n" +
          "event: context.window_usage\n" +
          'data: {"total_tokens":420000,"hard_limit":1000000,"usage_ratio":0.42}\n\n',
      });
      return;
    }
    const responses: Record<string, unknown> = {
      "/api/v1/status": {
        service: "ready",
        workspace: "/tmp/test",
        workspace_name: "test",
        branch: "main",
        model: "test-model",
        session_id: "active",
        timeline_id: "timeline",
        csrf_token: "csrf",
        active_operation: { operation_id: "operation-1", status: "running", kind: "turn" },
        pending_approvals: [],
      },
      "/api/v1/sessions": { items: [], next_offset: null },
      "/api/v1/sessions/active/turns": {
        timeline_id: "timeline",
        turns: [],
        next_before_turn_number: null,
      },
      "/api/v1/workspace/status": {
        branch: "main",
        changed_file_count: 0,
        added_lines: 0,
        deleted_lines: 0,
        clean: true,
      },
      "/api/v1/workspace/diff": { files: [] },
    };
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(responses[path] ?? {}),
    });
  });

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");
  await expect(page.locator(".tool-run-card")).toHaveCount(2);
  await expect(page.locator(".tool-run-card").nth(0)).toContainText("pytest-a");
  await expect(page.locator(".tool-run-card").nth(0)).toContainText("collecting tests");
  await expect(page.locator(".tool-run-card").nth(1)).toContainText("已完成");
  await expect(page.locator(".tool-run-card").nth(1)).toContainText("输出已截断");
  await expect(page.locator(".tool-run-grid")).toHaveAttribute("aria-label", "并行工具");
  await expect(page.getByText("上下文占用 42%")).toBeVisible();
  await page.screenshot({ path: "test-results/parallel-tools-desktop.png", fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  const cardWidths = await page.locator(".tool-run-card").evaluateAll((cards) =>
    cards.map((card) => ({
      width: card.getBoundingClientRect().width,
      parentWidth: card.parentElement?.getBoundingClientRect().width ?? 0,
    })),
  );
  expect(cardWidths.every(({ width, parentWidth }) => width <= parentWidth)).toBe(true);
  await page.screenshot({ path: "test-results/parallel-tools-mobile.png", fullPage: true });
});

test("cancelling parallel tools preserves the conversation", async ({ page }) => {
  let cancelled = false;
  let releaseEvents: () => void = () => {};
  const cancellation = new Promise<void>((resolve) => {
    releaseEvents = resolve;
  });
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/operations/operation-1/cancel") {
      cancelled = true;
      releaseEvents();
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({ operation_id: "operation-1", status: "cancel_requested" }),
      });
      return;
    }
    if (path === "/api/v1/operations/operation-1/events") {
      await cancellation;
      await route.fulfill({
        contentType: "text/event-stream",
        body:
          "id: 1\n" +
          "event: tool.started\n" +
          'data: {"run_id":"run-alpha","tool":"pytest-a","status":"running"}\n\n' +
          "id: 2\n" +
          "event: tool.started\n" +
          'data: {"run_id":"run-beta","tool":"pytest-b","status":"running"}\n\n' +
          "id: 3\n" +
          "event: operation.cancelled\n" +
          'data: {"operation_id":"operation-1","sequence":3}\n\n',
      });
      return;
    }
    const turns = [
      {
        turn_id: "turn-old",
        turn_number: 1,
        user_text: "保留的历史消息",
        assistant_text: "历史回复",
        status: "completed",
        created_at: "2026-09-10T12:00:00Z",
        snapshot_oid: "1234567890abcdef",
      },
      ...(cancelled
        ? [
            {
              turn_id: "turn-cancelled",
              turn_number: 2,
              user_text: "后台执行两个工具",
              assistant_text: "已取消，本轮终止了 2 个后台工具。",
              status: "cancelled",
              created_at: "2026-09-10T12:01:00Z",
              snapshot_oid: "1234567890abcdef",
            },
          ]
        : []),
    ];
    const responses: Record<string, unknown> = {
      "/api/v1/status": {
        service: "ready",
        workspace: "/tmp/test",
        workspace_name: "test",
        branch: "main",
        model: "test-model",
        session_id: "active",
        timeline_id: "timeline",
        csrf_token: "csrf",
        active_operation: cancelled
          ? null
          : { operation_id: "operation-1", status: "running", kind: "turn" },
        active_operations: cancelled
          ? []
          : [{ operation_id: "operation-1", status: "running", kind: "turn" }],
        pending_approvals: [],
      },
      "/api/v1/sessions": { items: [], next_offset: null },
      "/api/v1/sessions/active/turns": {
        timeline_id: "timeline",
        turns,
        next_before_turn_number: null,
      },
      "/api/v1/workspace/status": {
        branch: "main",
        changed_file_count: 0,
        added_lines: 0,
        deleted_lines: 0,
        clean: true,
      },
      "/api/v1/workspace/diff": { files: [] },
    };
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(responses[path] ?? {}),
    });
  });

  await page.goto("/");
  await expect(page.getByText("保留的历史消息")).toBeVisible();
  await expect(page.getByRole("button", { name: "恢复到此轮" })).toBeDisabled();
  await page.getByTitle("停止当前任务").click();

  await expect(page.getByText("后台执行两个工具")).toBeVisible();
  await expect(page.getByText("保留的历史消息")).toBeVisible();
  await expect(page.locator(".cancelled-label")).toHaveText("已取消");
  await expect(page.locator(".user-message")).toHaveCount(2);
});

test("loads session summaries page by page", async ({ page }) => {
  let sessionRequests = 0;
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === "/api/v1/sessions") {
      sessionRequests += 1;
      const offset = Number(url.searchParams.get("offset") ?? 0);
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(
          offset === 0
            ? {
                items: [
                  {
                    session_id: "active",
                    active_timeline_id: "timeline",
                    title: "当前任务",
                    turn_count: 1,
                    created_at: "2026-09-10T12:00:00Z",
                  },
                ],
                next_offset: 30,
              }
            : {
                items: [
                  {
                    session_id: "older",
                    active_timeline_id: "older-timeline",
                    title: "更早任务",
                    turn_count: 2,
                    created_at: "2026-09-09T12:00:00Z",
                  },
                ],
                next_offset: null,
              },
        ),
      });
      return;
    }
    const responses: Record<string, unknown> = {
      "/api/v1/status": {
        service: "ready",
        workspace: "/tmp/test",
        workspace_name: "test",
        branch: "main",
        model: "test-model",
        session_id: "active",
        timeline_id: "timeline",
        csrf_token: "csrf",
        active_operation: null,
        pending_approvals: [],
      },
      "/api/v1/workspaces": { active: "/tmp/test", recent: [] },
      "/api/v1/sessions/active/turns": {
        timeline_id: "timeline",
        turns: [],
        next_before_turn_number: null,
      },
      "/api/v1/workspace/status": {
        branch: "main",
        changed_file_count: 0,
        added_lines: 0,
        deleted_lines: 0,
        clean: true,
      },
      "/api/v1/workspace/diff": { files: [] },
    };
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify(responses[path] ?? {}),
    });
  });

  await page.goto("/");
  await expect(page.locator(".session-list").getByText("当前任务", { exact: true })).toBeVisible();
  expect(sessionRequests).toBe(1);
  await page.getByRole("button", { name: "更多任务", exact: true }).click();
  await expect(page.getByText("更早任务", { exact: true })).toBeVisible();
  expect(sessionRequests).toBe(2);
});

test("renders independent subagent cards and revision evidence", async ({ page }) => {
  const overrides: Record<string, unknown> = {};
  await mockWorkbench(page, overrides);
  const baseAttempt = {
    base_commit: "base1234",
    worktree_path: "/tmp/worktree",
    allowed_tools: ["mock_sleep", "write_deliverable"],
    started_at: "2026-09-14T12:00:00Z",
    ended_at: "2026-09-14T12:00:12Z",
    error: null,
  };
  overrides["/api/v1/sessions/active/turns"] = {
    timeline_id: "timeline",
    turns: [
      {
        turn_id: "turn-with-agents",
        turn_number: 1,
        user_text: "并行完成前后端任务",
        assistant_text: "两个子任务已经完成。",
        created_at: "2026-09-14T12:00:00Z",
        snapshot_oid: "snapshot1234",
      },
    ],
    next_before_turn_number: null,
  };
  overrides["/api/v1/sessions/active/subagent-runs"] = {
    runs: [{
          run_id: "demo-1",
          session_id: "active",
          status: "completed",
          base_commit: "base1234",
          created_at: "2026-09-14T12:00:00Z",
          ended_at: "2026-09-14T12:00:24Z",
          turn_id: "turn-with-agents",
          expected_task_count: 2,
          tasks: [
            {
              task_id: "task-a",
              name: "Agent A",
              objective: "生成通过验收的异步执行报告",
              output_path: ".coding-agent-demo/demo-1/agent-a.md",
              scope: [".coding-agent-demo/demo-1/agent-a.md"],
              acceptance: ["verification_status 必须为 pass"],
              status: "merged",
              active_attempt_id: "attempt-a",
              accepted_attempt_id: "attempt-a",
              feedback: null,
              integration_commit: "aaaaaaaa11111111",
              created_at: "2026-09-14T12:00:00Z",
              updated_at: "2026-09-14T12:00:12Z",
              attempts: [
                {
                  ...baseAttempt,
                  attempt_id: "attempt-a",
                  attempt_number: 1,
                  status: "accepted",
                  result_commit: "aaaaaaaa11111111",
                },
              ],
              events: [
                {
                  event_id: 1,
                  attempt_id: "attempt-a",
                  event_type: "review.accepted",
                  payload: {
                    decision: "ACCEPT",
                    changed_paths: [".coding-agent-demo/demo-1/agent-a.md"],
                  },
                  created_at: "2026-09-14T12:00:12Z",
                },
              ],
            },
            {
              task_id: "task-b",
              name: "Agent B",
              objective: "根据主 Agent 反馈完成返工",
              output_path: ".coding-agent-demo/demo-1/agent-b.md",
              scope: [".coding-agent-demo/demo-1/agent-b.md"],
              acceptance: ["verification_status 必须为 pass"],
              status: "merged",
              active_attempt_id: "attempt-b2",
              accepted_attempt_id: "attempt-b2",
              feedback: "verification_status 未通过，请修正为 pass。",
              integration_commit: "bbbbbbbb22222222",
              created_at: "2026-09-14T12:00:00Z",
              updated_at: "2026-09-14T12:00:24Z",
              attempts: [
                {
                  ...baseAttempt,
                  attempt_id: "attempt-b1",
                  attempt_number: 1,
                  status: "rejected",
                  result_commit: "cccccccc33333333",
                },
                {
                  ...baseAttempt,
                  attempt_id: "attempt-b2",
                  attempt_number: 2,
                  status: "accepted",
                  result_commit: "bbbbbbbb22222222",
                  started_at: "2026-09-14T12:00:12Z",
                  ended_at: "2026-09-14T12:00:24Z",
                },
              ],
              events: [
                {
                  event_id: 2,
                  attempt_id: "attempt-b1",
                  event_type: "review.rejected",
                  payload: { decision: "REVISE" },
                  created_at: "2026-09-14T12:00:12Z",
                },
                {
                  event_id: 3,
                  attempt_id: "attempt-b2",
                  event_type: "integration.completed",
                  payload: { integration_commit: "bbbbbbbb22222222" },
                  created_at: "2026-09-14T12:00:24Z",
                },
              ],
            },
          ],
    }],
  };

  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");
  await expect(page.locator(".subagent-card")).toHaveCount(2);
  await expect(page.getByText("2/2 已集成")).toBeVisible();
  await expect(page.getByText("#1 未通过")).toBeVisible();
  await expect(page.getByText("#2 通过")).toBeVisible();
  await expect(page.getByText("主 Agent 反馈", { exact: true })).toBeVisible();
  await expect(page.getByText("结果已集成")).toBeVisible();
  await expect(page.locator(".turn-group .subagent-demo")).toHaveCount(1);
  await page.getByText("任务契约与结果证据").first().click();
  await expect(page.getByText("verification_status 必须为 pass").first()).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  );
  expect(overflow).toBe(false);
  await page.screenshot({ path: "test-results/subagent-cards.png", fullPage: true });
});
