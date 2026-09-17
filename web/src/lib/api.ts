export type HostStatus = {
  service: string;
  workspace: string | null;
  workspace_name: string | null;
  branch: string | null;
  model: string;
  session_id: string | null;
  timeline_id: string | null;
  csrf_token: string;
  active_operation: Operation | null;
  active_operations?: Operation[];
  active_subagent_demo: { run_id: string; status: string } | null;
  pending_approvals: Approval[];
};

export type SubagentEvent = {
  event_id: number;
  attempt_id: string | null;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type SubagentAttempt = {
  attempt_id: string;
  attempt_number: number;
  status: string;
  base_commit: string;
  result_commit: string | null;
  worktree_path: string | null;
  workspace_mode?: "none" | "auto" | "required";
  workspace_state?: "unallocated" | "ready";
  thread_id?: string | null;
  checkpoint_id?: string | null;
  context_usage?: ContextUsage | null;
  allowed_tools: string[];
  started_at: string | null;
  ended_at: string | null;
  error: string | null;
  result: {
    result_kind: "analysis" | "patch";
    summary: string;
    changed_files: string[];
    result_commit: string | null;
    checks: Array<{ command: string; exit_code: number | null; artifact_ref: string | null }>;
    evidence: Array<{
      kind: string;
      title: string;
      source_ref: string | null;
      tool: string | null;
    }>;
    provenance: Array<{
      provider: string;
      tool: string | null;
      artifact_ref: string | null;
    }>;
    risks: string[];
    unresolved: string[];
    legacy_raw_output: string | null;
  } | null;
};

export type SubagentTask = {
  task_id: string;
  name: string;
  objective: string;
  output_path: string;
  scope: string[];
  acceptance: string[];
  status: string;
  active_attempt_id: string | null;
  accepted_attempt_id: string | null;
  feedback: string | null;
  integration_commit: string | null;
  created_at: string;
  updated_at: string;
  attempts: SubagentAttempt[];
  events: SubagentEvent[];
};

export type SubagentDemo = {
  run_id: string;
  session_id: string;
  status: string;
  base_commit: string;
  created_at: string;
  ended_at: string | null;
  turn_id: string | null;
  expected_task_count: number;
  tasks: SubagentTask[];
};

export type WorkspaceEntry = {
  path: string;
  name: string;
  active: boolean;
  available: boolean;
};

export type Workspaces = {
  active: string | null;
  recent: WorkspaceEntry[];
};

export type Operation = {
  operation_id: string;
  session_id?: string;
  status: string;
  kind: string;
};

export type Approval = {
  approval_id: string;
  operation_id: string;
  session_id: string;
  request_hash: string;
  request: {
    name?: string;
    args?: { name?: string; argv?: string[]; cwd?: string; timeout_seconds?: number };
    skill_capabilities?: {
      commands: Array<{ name: string; description: string }>;
      mcp_servers: Array<{ name: string; url: string; environment: string[] }>;
    };
  };
};

export type Session = {
  session_id: string;
  active_timeline_id: string;
  title: string;
  turn_count: number;
  created_at: string;
};

export type SessionPage = {
  items: Session[];
  next_offset: number | null;
};

export type SettingsConfig = {
  model: string;
  base_url: string | null;
  langsmith_enabled: boolean;
  langsmith_project: string;
  model_timeout_seconds: number;
  main_agent_model_call_limit: number;
  subagent_model_call_limit: number;
  command_timeout_seconds: number;
  max_parallel_sessions: number;
  context_auto_compact_ratio: number;
  context_recent_user_inputs_max_tokens: number;
  context_summary_max_tokens: number;
  context_compaction_enabled: boolean;
  context_window_tokens: number | null;
  sandbox_enabled: boolean;
  sandbox_provider: string;
  sandbox_image: string | null;
  sandbox_health: {
    available: boolean;
    provider: string;
    isolation_level: "seatbelt" | "container";
    context: string | null;
    rootless: boolean;
    image_digest: string | null;
    policy_digest: string | null;
    error_code: string | null;
    message: string | null;
  } | null;
  model_api_key_configured: boolean;
  langsmith_api_key_configured: boolean;
};

export type Turn = {
  turn_id: string;
  turn_number: number;
  user_text: string;
  assistant_text: string;
  status?: "completed" | "cancelled";
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  duration_ms?: number | null;
  snapshot_oid: string;
};

export type TurnPage = {
  timeline_id: string;
  turns: Turn[];
  notices: Array<{
    notice_id: string;
    notice_kind: "context.compression.completed";
    text: string;
    created_at: string;
  }>;
  next_before_turn_number: number | null;
};

export type ContextUsage = {
  context_owner_id: string;
  session_id: string;
  timeline_id: string | null;
  used_tokens: number;
  max_tokens: number;
  usage_ratio: number;
  message_count: number;
  compression_count: number;
  updated_at: string;
};

export type ToolRunView = {
  run_id: string;
  tool: string;
  effect: string;
  execution_group_id: string | null;
  status: string;
  created_at?: string;
  started_at?: string;
  ended_at?: string;
  output: string;
  output_cursor: number;
  exit_code?: number | null;
  timed_out?: boolean;
  duration_ms?: number | null;
  error?: string | null;
  result_preview?: string;
  output_truncated?: boolean;
};

export type WorkspaceStatus = {
  branch: string;
  changed_file_count: number;
  added_lines: number;
  deleted_lines: number;
  clean: boolean;
};

export type DiffFile = {
  path: string;
  status: string;
  additions: number;
  deletions: number;
  patch: string;
  truncated: boolean;
};

export type WorkspaceFile = { path: string; size: number };

export type FileContent = {
  path: string;
  sha256: string;
  byte_size: number;
  binary: boolean;
  truncated: boolean;
  content: string;
};

export type TraceSpan = {
  span_id: string;
  parent_span_id: string | null;
  trace_id: string;
  trace_depth: number;
  trace_label: string;
  is_trace_root: boolean;
  kind: string;
  name: string;
  status: string;
  started_at: string;
  ended_at: string | null;
  attributes_json: string;
  attributes_json_truncated?: boolean;
};

export type TraceSummary = {
  trace_id: string;
  parent_trace_id: string | null;
  parent_span_id: string | null;
  root_span_id: string;
  logical_run_id: string;
  label: string;
  role: string;
  task_id: string | null;
  depth: number;
  status: string;
  started_at: string;
  ended_at: string | null;
  input_tokens: number;
  output_tokens: number;
  spans: TraceSpan[];
};

export type TraceChain = {
  trace_id: string;
  input_tokens: number;
  output_tokens: number;
  spans: TraceSpan[];
  traces: TraceSummary[];
};

export type StreamEvent = {
  type: string;
  data: Record<string, unknown>;
  sequence: number;
};

export type StreamConnectionState =
  | "connecting"
  | "connected"
  | "reconnecting"
  | "closed";

let csrfToken = "";

export async function bootstrap(): Promise<HostStatus> {
  const value = await request<HostStatus>("/api/v1/status");
  csrfToken = value.csrf_token;
  return value;
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: {
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...(init.method && init.method !== "GET" ? { "X-CSRF-Token": csrfToken } : {}),
      ...init.headers,
    },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = body.error ?? { code: "HTTP_ERROR", message: response.statusText };
    throw new Error(`${error.code}: ${error.message}`);
  }
  return body as T;
}

export function subscribe(
  operationId: string,
  onEvent: (event: StreamEvent) => void,
  onDone: () => void,
  options: {
    after?: number;
    onCursor?: (sequence: number) => void;
    onState?: (state: StreamConnectionState) => void;
    onError?: (message: string) => void;
  } = {},
): () => void {
  let cursor = options.after ?? 0;
  let source: EventSource | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let stopped = false;
  const types = [
    "turn.started",
    "user.message",
    "assistant.delta",
    "assistant.completed",
    "step.started",
    "step.completed",
    "tool.started",
    "tool.running",
    "tool.output",
    "tool.cancelling",
    "tool.completed",
    "tool.failed",
    "tool.cancelled",
    "approval.required",
    "approval.resolved",
    "orchestration.started",
    "plan.completed",
    "delegation.queued",
    "delegation.started",
    "delegation.progress",
    "delegation.blocked",
    "delegation.completed",
    "audit.completed",
    "integration.started",
    "integration.conflict",
    "integration.completed",
    "verification.completed",
    "orchestration.completed",
    "orchestration.failed",
    "orchestration.cancelled",
    "context.window_usage",
    "context.compression.started",
    "context.compression.progress",
    "context.compression.completed",
    "context.compression.failed",
    "workspace.changed",
    "turn.committed",
    "operation.completed",
    "operation.failed",
    "operation.cancellation_requested",
    "operation.cancelled",
    "operation.recovery_required",
  ];
  const terminalTypes = new Set([
    "operation.completed",
    "operation.failed",
    "operation.cancelled",
    "operation.recovery_required",
  ]);

  const connect = () => {
    if (stopped) return;
    options.onState?.(cursor ? "reconnecting" : "connecting");
    source = new EventSource(
      `/api/v1/operations/${operationId}/events?after=${cursor}`,
    );
    source.onopen = () => options.onState?.("connected");
    for (const type of types) {
      source.addEventListener(type, (raw) => {
        const message = raw as MessageEvent<string>;
        let data: Record<string, unknown>;
        try {
          data = JSON.parse(message.data) as Record<string, unknown>;
        } catch {
          options.onError?.(`无法解析 ${type} 事件`);
          return;
        }
        const sequence = Number(data.sequence ?? message.lastEventId);
        if (!Number.isFinite(sequence) || sequence <= cursor) return;
        cursor = sequence;
        options.onCursor?.(sequence);
        onEvent({ type, data, sequence });
        if (terminalTypes.has(type)) {
          stopped = true;
          source?.close();
          options.onState?.("closed");
          onDone();
        }
      });
    }
    source.onerror = () => {
      if (stopped) return;
      source?.close();
      options.onState?.("reconnecting");
      reconnectTimer = setTimeout(connect, 500);
    };
  };
  connect();
  return () => {
    stopped = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    source?.close();
  };
}

export function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, { method: "POST", body: JSON.stringify(body) });
}
