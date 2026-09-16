import {
  AlertCircle,
  ArrowDown,
  ArrowUp,
  Bot,
  Check,
  ChevronDown,
  CircleDot,
  Clock3,
  Code2,
  File,
  FileCode2,
  FileDiff,
  Files,
  Folder,
  FolderOpen,
  GitBranch,
  GitCommitHorizontal,
  History,
  LoaderCircle,
  Menu,
  MessageSquare,
  Monitor,
  MoreHorizontal,
  Paperclip,
  PanelRightClose,
  PanelRightOpen,
  Play,
  Plus,
  RotateCcw,
  Search,
  Send,
  Settings,
  Square,
  TerminalSquare,
  Trash2,
  User,
  Wrench,
  X,
} from "lucide-react";
import {
  FormEvent,
  SetStateAction,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  type InfiniteData,
  useInfiniteQuery,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Approval,
  DiffFile,
  FileContent,
  HostStatus,
  Session,
  SessionPage,
  SettingsConfig,
  StreamConnectionState,
  StreamEvent,
  SubagentDemo,
  SubagentEvent,
  SubagentTask,
  ToolRunView,
  TraceSpan,
  Turn,
  TurnPage,
  WorkspaceFile,
  Workspaces,
  WorkspaceStatus,
  bootstrap,
  post,
  request,
  subscribe,
} from "./lib/api";

type InspectorTab = "changes" | "files" | "preview";
type MobileView = "tasks" | "chat" | "workspace";
type SessionRuntimeState = {
  draft: string;
  pendingTurn: Turn | null;
  streamText: string;
  running: boolean;
  cancelling: boolean;
  operationId: string | null;
  events: StreamEvent[];
  toolRuns: Record<string, ToolRunView>;
  eventCursors: Record<string, number>;
  connectionState: StreamConnectionState;
  approval: Approval | null;
  error: string;
};

const NEW_TASK_RUNTIME_KEY = "__new_task__";

function createSessionRuntime(): SessionRuntimeState {
  return {
    draft: "",
    pendingTurn: null,
    streamText: "",
    running: false,
    cancelling: false,
    operationId: null,
    events: [],
    toolRuns: {},
    eventCursors: {},
    connectionState: "closed",
    approval: null,
    error: "",
  };
}

function resolveStateAction<T>(current: T, action: SetStateAction<T>): T {
  return typeof action === "function"
    ? (action as (value: T) => T)(current)
    : action;
}

export function App() {
  const queryClient = useQueryClient();
  const [runtimeBySession, setRuntimeBySession] = useState<
    Record<string, SessionRuntimeState>
  >({});
  const runtimeBySessionRef = useRef(runtimeBySession);
  runtimeBySessionRef.current = runtimeBySession;
  const [demoStarting, setDemoStarting] = useState(false);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>("changes");
  const [selectedDiff, setSelectedDiff] = useState<string | null>(null);
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [selectedTurn, setSelectedTurn] = useState<Turn | null>(null);
  const [restoreTurn, setRestoreTurn] = useState<Turn | null>(null);
  const [newTaskDraft, setNewTaskDraft] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsError, setSettingsError] = useState("");
  const [applyingSettings, setApplyingSettings] = useState(false);
  const [workspacePickerOpen, setWorkspacePickerOpen] = useState(false);
  const [workspacePath, setWorkspacePath] = useState("");
  const [workspaceError, setWorkspaceError] = useState("");
  const [openingWorkspace, setOpeningWorkspace] = useState(false);
  const [mobileView, setMobileView] = useState<MobileView>("chat");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [inspectorOpen, setInspectorOpen] = useState(
    () => localStorage.getItem("coding-agent.inspector-open") !== "false",
  );
  const subscriptions = useRef(
    new Map<string, { sessionId: string; stop: () => void }>(),
  );
  const selectionEpoch = useRef(0);
  const desiredSelection = useRef<{ id: string; epoch: number } | null>(null);
  const selectionWorkerRunning = useRef(false);
  const streamBuffers = useRef(new Map<string, string>());
  const streamFlushTimers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const messages = useRef<HTMLDivElement>(null);
  const followMessages = useRef(true);
  const messageEnd = useRef<HTMLDivElement>(null);

  const status = useQuery({
    queryKey: ["status"],
    queryFn: bootstrap,
    refetchInterval: (query) =>
      query.state.data?.active_operations?.length ? 1000 : false,
  });
  const workspaces = useQuery({
    queryKey: ["workspaces"],
    queryFn: () => request<Workspaces>("/api/v1/workspaces"),
    enabled: status.isSuccess,
  });
  const sessions = useInfiniteQuery({
    queryKey: ["sessions"],
    queryFn: ({ pageParam }) =>
      request<SessionPage>(`/api/v1/sessions?limit=30&offset=${pageParam}`),
    initialPageParam: 0,
    getNextPageParam: (page) => page.next_offset ?? undefined,
    enabled: Boolean(status.data?.workspace),
  });
  const sessionId = status.data?.session_id;
  const runtimeKey = newTaskDraft
    ? NEW_TASK_RUNTIME_KEY
    : (sessionId ?? NEW_TASK_RUNTIME_KEY);
  const runtime = runtimeBySession[runtimeKey] ?? createSessionRuntime();
  const {
    draft,
    pendingTurn,
    streamText,
    running,
    cancelling,
    operationId,
    events,
    toolRuns,
    approval,
    error,
  } = runtime;
  const updateRuntime = (
    key: string,
    update: (current: SessionRuntimeState) => SessionRuntimeState,
  ) => {
    setRuntimeBySession((current) => {
      const previous = current[key] ?? createSessionRuntime();
      const next = update(previous);
      return next === previous ? current : { ...current, [key]: next };
    });
  };
  const setRuntimeField = <K extends keyof SessionRuntimeState>(
    key: string,
    field: K,
    action: SetStateAction<SessionRuntimeState[K]>,
  ) => {
    updateRuntime(key, (current) => ({
      ...current,
      [field]: resolveStateAction(current[field], action),
    }));
  };
  const setDraft = (value: SetStateAction<string>) =>
    setRuntimeField(runtimeKey, "draft", value);
  const setPendingTurn = (value: SetStateAction<Turn | null>) =>
    setRuntimeField(runtimeKey, "pendingTurn", value);
  const setStreamText = (value: SetStateAction<string>) =>
    setRuntimeField(runtimeKey, "streamText", value);
  const setRunning = (value: SetStateAction<boolean>) =>
    setRuntimeField(runtimeKey, "running", value);
  const setCancelling = (value: SetStateAction<boolean>) =>
    setRuntimeField(runtimeKey, "cancelling", value);
  const setEvents = (value: SetStateAction<StreamEvent[]>) =>
    setRuntimeField(runtimeKey, "events", value);
  const setToolRuns = (value: SetStateAction<Record<string, ToolRunView>>) =>
    setRuntimeField(runtimeKey, "toolRuns", value);
  const setApproval = (value: SetStateAction<Approval | null>) =>
    setRuntimeField(runtimeKey, "approval", value);
  const setError = (value: SetStateAction<string>) =>
    setRuntimeField(runtimeKey, "error", value);
  const pendingApprovalSignature = status.data?.pending_approvals
    .map((item) => `${item.session_id}:${item.approval_id}`)
    .join("|");
  const subagentRuns = useQuery({
    queryKey: ["subagent-runs", sessionId],
    queryFn: () =>
      request<{ runs: SubagentDemo[] }>(
        `/api/v1/sessions/${sessionId}/subagent-runs`,
      ),
    enabled: Boolean(sessionId) && !newTaskDraft,
    refetchInterval: (query) =>
      running || query.state.data?.runs?.some((run) => run.status === "running")
        ? 750
        : false,
  });
  const turns = useInfiniteQuery({
    queryKey: ["turns", sessionId],
    queryFn: ({ pageParam }) =>
      request<TurnPage>(
        `/api/v1/sessions/${sessionId}/turns?limit=30${
          pageParam === null ? "" : `&before_turn_number=${pageParam}`
        }`,
      ),
    initialPageParam: null as number | null,
    getNextPageParam: (page) => page.next_before_turn_number ?? undefined,
    enabled: Boolean(sessionId) && !newTaskDraft,
  });
  const workspace = useQuery({
    queryKey: ["workspace-status"],
    queryFn: () => request<WorkspaceStatus>("/api/v1/workspace/status"),
    enabled: Boolean(status.data?.workspace),
  });
  const diff = useQuery({
    queryKey: ["diff"],
    queryFn: () => request<{ files: DiffFile[] }>("/api/v1/workspace/diff"),
    enabled: Boolean(status.data?.workspace),
  });
  const activeDiffPath = selectedDiff ?? diff.data?.files[0]?.path ?? null;
  const diffContent = useQuery({
    queryKey: ["diff-content", activeDiffPath],
    queryFn: () =>
      request<{ files: DiffFile[] }>(
        `/api/v1/workspace/diff?path=${encodeURIComponent(activeDiffPath!)}`,
      ),
    enabled: Boolean(status.data?.workspace && activeDiffPath),
  });
  const files = useQuery({
    queryKey: ["files"],
    queryFn: () => request<{ files: WorkspaceFile[] }>("/api/v1/workspace/files"),
    enabled: Boolean(status.data?.workspace) && inspectorTab === "files",
  });
  const fileContent = useQuery({
    queryKey: ["file-content", selectedFile],
    queryFn: () =>
      request<FileContent>(
        `/api/v1/workspace/files/content?path=${encodeURIComponent(selectedFile!)}`,
      ),
    enabled: Boolean(selectedFile),
  });
  const trace = useQuery({
    queryKey: ["trace", selectedTurn?.turn_id],
    queryFn: () =>
      request<{ spans: TraceSpan[] }>(`/api/v1/turns/${selectedTurn!.turn_id}/trace`),
    enabled: Boolean(selectedTurn?.user_text),
    retry: false,
  });
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: () => request<SettingsConfig>("/api/v1/settings"),
    enabled: settingsOpen,
  });

  const refresh = (targetSessionId: string | null = sessionId ?? null) => {
    void queryClient.invalidateQueries({ queryKey: ["status"] });
    void queryClient.invalidateQueries({ queryKey: ["sessions"] });
    void queryClient.invalidateQueries({ queryKey: ["workspace-status"] });
    void queryClient.invalidateQueries({ queryKey: ["diff"] });
    void queryClient.invalidateQueries({ queryKey: ["diff-content"] });
    if (targetSessionId) {
      void queryClient.invalidateQueries({
        queryKey: ["subagent-runs", targetSessionId],
      });
      return queryClient.invalidateQueries({
        queryKey: ["turns", targetSessionId],
      });
    }
    return queryClient.invalidateQueries({ queryKey: ["turns"] });
  };

  const handleEvent = (targetSessionId: string, event: StreamEvent) => {
    if (event.type === "assistant.delta") {
      streamBuffers.current.set(
        targetSessionId,
        (streamBuffers.current.get(targetSessionId) ?? "") +
          String(event.data.text ?? ""),
      );
      if (!streamFlushTimers.current.has(targetSessionId)) {
        const timer = setTimeout(() => {
          const text = streamBuffers.current.get(targetSessionId) ?? "";
          streamBuffers.current.delete(targetSessionId);
          streamFlushTimers.current.delete(targetSessionId);
          if (text) {
            updateRuntime(targetSessionId, (current) => ({
              ...current,
              streamText: current.streamText + text,
            }));
          }
        }, 50);
        streamFlushTimers.current.set(targetSessionId, timer);
      }
      return;
    }
    updateRuntime(targetSessionId, (current) => {
      let next: SessionRuntimeState = {
        ...current,
        events: [...current.events.slice(-99), event],
      };
      if (event.type === "approval.required") {
        next.approval = {
          ...(event.data as unknown as Approval),
          session_id: targetSessionId,
        };
      }
      if (event.type === "approval.resolved") next.approval = null;
      if (event.type.startsWith("tool.")) {
        const runId = String(event.data.run_id ?? "");
        if (runId) {
          const prior = next.toolRuns[runId];
          const toolRun: ToolRunView = {
            run_id: runId,
            tool: String(event.data.tool ?? prior?.tool ?? "tool"),
            effect: String(event.data.effect ?? prior?.effect ?? "unknown"),
            execution_group_id:
              typeof event.data.execution_group_id === "string"
                ? event.data.execution_group_id
                : (prior?.execution_group_id ?? null),
            status:
              event.type === "tool.output"
                ? (prior?.status ?? "running")
                : String(event.data.status ?? event.type.slice("tool.".length)),
            created_at: String(event.data.created_at ?? prior?.created_at ?? ""),
            started_at: String(event.data.started_at ?? prior?.started_at ?? ""),
            ended_at: String(event.data.ended_at ?? prior?.ended_at ?? ""),
            output:
              event.type === "tool.output"
                ? `${prior?.output ?? ""}${String(event.data.text ?? "")}`.slice(-4000)
                : (prior?.output ?? ""),
            output_cursor: Number(event.data.cursor ?? prior?.output_cursor ?? 0),
            exit_code:
              typeof event.data.exit_code === "number"
                ? event.data.exit_code
                : (prior?.exit_code ?? null),
            timed_out: Boolean(event.data.timed_out ?? prior?.timed_out ?? false),
            duration_ms:
              typeof event.data.duration_ms === "number"
                ? event.data.duration_ms
                : (prior?.duration_ms ?? null),
            error:
              typeof event.data.error === "string"
                ? event.data.error
                : (prior?.error ?? null),
            result_preview:
              typeof event.data.result_preview === "string"
                ? event.data.result_preview
                : prior?.result_preview,
            output_truncated:
              Boolean(event.data.output_truncated) ||
              Boolean(prior?.output_truncated) ||
              (event.type === "tool.output" &&
                `${prior?.output ?? ""}${String(event.data.text ?? "")}`.length >
                  4000),
          };
          return {
            ...next,
            toolRuns: { ...next.toolRuns, [runId]: toolRun },
          };
        }
      }
      if (
        event.type === "operation.failed" ||
        event.type === "operation.recovery_required"
      ) {
        const detail = event.data.error as { code?: string; message?: string };
        next.error = `${detail?.code ?? "RUNTIME_ERROR"}: ${
          detail?.message ??
          (event.type === "operation.recovery_required"
            ? "Host 重启后无法继续该任务"
            : "执行失败")
        }`;
      }
      if (event.type === "operation.cancellation_requested") {
        next.cancelling = true;
      }
      return next;
    });
  };

  const followOperation = (targetSessionId: string, id: string) => {
    if (subscriptions.current.has(id)) return;
    updateRuntime(targetSessionId, (current) => ({
      ...current,
      operationId: id,
      running: true,
      cancelling: false,
      connectionState: "connecting",
    }));
    const stop = subscribe(
      id,
      (event) => handleEvent(targetSessionId, event),
      () => {
        subscriptions.current.delete(id);
        const flushTimer = streamFlushTimers.current.get(targetSessionId);
        if (flushTimer) clearTimeout(flushTimer);
        streamFlushTimers.current.delete(targetSessionId);
        streamBuffers.current.delete(targetSessionId);
        updateRuntime(targetSessionId, (current) =>
          current.operationId === id
            ? {
                ...current,
                running: false,
                cancelling: false,
                operationId: null,
                streamText: "",
                pendingTurn: null,
                connectionState: "closed",
              }
            : current,
        );
        void refresh(targetSessionId);
      },
      {
        after:
          runtimeBySessionRef.current[targetSessionId]?.eventCursors[id] ?? 0,
        onCursor: (sequence) => {
          updateRuntime(targetSessionId, (current) => ({
            ...current,
            eventCursors: { ...current.eventCursors, [id]: sequence },
          }));
        },
        onState: (connectionState) => {
          updateRuntime(targetSessionId, (current) =>
            current.operationId === id
              ? { ...current, connectionState }
              : current,
          );
        },
        onError: (message) => {
          updateRuntime(targetSessionId, (current) => ({
            ...current,
            error: message,
          }));
        },
      },
    );
    subscriptions.current.set(id, { sessionId: targetSessionId, stop });
  };

  useEffect(() => {
    if (!status.data) return;
    const activeOperations = status.data.active_operations?.length
      ? status.data.active_operations
      : status.data.active_operation
        ? [status.data.active_operation]
        : [];
    for (const operation of activeOperations) {
      const targetSessionId =
        operation.session_id ??
        (operation.operation_id === status.data.active_operation?.operation_id
          ? status.data.session_id
          : null);
      if (targetSessionId) {
        followOperation(targetSessionId, operation.operation_id);
      }
    }
    setRuntimeBySession((current) => {
      const keys = new Set([
        ...Object.keys(current),
        ...status.data.pending_approvals.map((item) => item.session_id),
      ]);
      let changed = false;
      const next = { ...current };
      for (const key of keys) {
        if (key === NEW_TASK_RUNTIME_KEY) continue;
        const previous = current[key] ?? createSessionRuntime();
        const nextApproval =
          status.data.pending_approvals.find((item) => item.session_id === key) ??
          null;
        if (previous.approval?.approval_id !== nextApproval?.approval_id) {
          next[key] = { ...previous, approval: nextApproval };
          changed = true;
        }
      }
      return changed ? next : current;
    });
  }, [
    status.data?.active_operations
      ?.map((operation) => `${operation.session_id}:${operation.operation_id}`)
      .join("|"),
    status.data?.active_operation?.operation_id,
    pendingApprovalSignature,
  ]);

  useEffect(() => {
    return () => {
      for (const subscription of subscriptions.current.values()) {
        subscription.stop();
      }
      subscriptions.current.clear();
      for (const timer of streamFlushTimers.current.values()) clearTimeout(timer);
      streamFlushTimers.current.clear();
      streamBuffers.current.clear();
    };
  }, []);

  useEffect(() => {
    if (followMessages.current) {
      messageEnd.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [turns.data, streamText, events.length]);

  useEffect(() => {
    followMessages.current = true;
    messageEnd.current?.scrollIntoView();
  }, [runtimeKey]);

  useEffect(() => {
    localStorage.setItem("coding-agent.inspector-open", String(inspectorOpen));
  }, [inspectorOpen]);

  const sendTurn = async (event: FormEvent) => {
    event.preventDefault();
    const message = draft.trim();
    if (!message || !sessionId || !status.data?.timeline_id || running) {
      return;
    }
    const optimisticTurn: Turn = {
      turn_id: `pending-${crypto.randomUUID()}`,
      turn_number: Number.MAX_SAFE_INTEGER,
      user_text: message,
      assistant_text: "",
      created_at: new Date().toISOString(),
      snapshot_oid: "",
    };
    setDraft("");
    setPendingTurn(optimisticTurn);
    setStreamText("");
    setEvents([]);
    setToolRuns({});
    setError("");
    setRunning(true);
    let targetRuntimeKey = runtimeKey;
    try {
      let targetSessionId = sessionId;
      let targetTimelineId = status.data.timeline_id;
      if (newTaskDraft) {
        const created = await post<Session>("/api/v1/sessions", {});
        targetSessionId = created.session_id;
        targetTimelineId = created.active_timeline_id;
        const createdSummary: Session = {
          ...created,
          title: message.slice(0, 60),
          turn_count: 0,
        };
        queryClient.setQueryData<HostStatus>(["status"], (current) =>
          current
            ? {
                ...current,
                session_id: created.session_id,
                timeline_id: created.active_timeline_id,
                active_operation: null,
              }
            : current,
        );
        queryClient.setQueryData<InfiniteData<SessionPage>>(
          ["sessions"],
          (current) => {
            if (!current) {
              return {
                pages: [{ items: [createdSummary], next_offset: null }],
                pageParams: [0],
              };
            }
            return {
              ...current,
              pages: current.pages.map((page, index) => ({
                ...page,
                items:
                  index === 0
                    ? [
                        createdSummary,
                        ...page.items.filter(
                          (item) => item.session_id !== created.session_id,
                        ),
                      ]
                    : page.items.filter(
                        (item) => item.session_id !== created.session_id,
                      ),
              })),
            };
          },
        );
        setRuntimeBySession((current) => {
          const temporary = current[NEW_TASK_RUNTIME_KEY] ?? createSessionRuntime();
          const next = { ...current, [created.session_id]: temporary };
          delete next[NEW_TASK_RUNTIME_KEY];
          return next;
        });
        targetRuntimeKey = created.session_id;
        setNewTaskDraft(false);
      }
      const operation = await post<{ operation_id: string }>(
        `/api/v1/sessions/${targetSessionId}/turns`,
        {
          message,
          client_request_id: crypto.randomUUID(),
          expected_timeline_id: targetTimelineId,
        },
      );
      followOperation(targetSessionId, operation.operation_id);
    } catch (cause) {
      updateRuntime(targetRuntimeKey, (current) => ({
        ...current,
        running: false,
        pendingTurn: null,
        draft: message,
        error: cause instanceof Error ? cause.message : String(cause),
      }));
      refresh(targetRuntimeKey === NEW_TASK_RUNTIME_KEY ? null : targetRuntimeKey);
    }
  };

  const startNewTask = () => {
    selectionEpoch.current += 1;
    desiredSelection.current = null;
    setRuntimeBySession((current) => ({
      ...current,
      [NEW_TASK_RUNTIME_KEY]: createSessionRuntime(),
    }));
    setNewTaskDraft(true);
    setSelectedTurn(null);
    setRestoreTurn(null);
    setMobileView("chat");
  };

  const cancelOperation = async () => {
    if (!operationId || cancelling) return;
    setCancelling(true);
    setError("");
    try {
      await post(`/api/v1/operations/${operationId}/cancel`, {});
    } catch (cause) {
      setCancelling(false);
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  const startSubagentDemo = async () => {
    if (!sessionId || demoStarting) return;
    setDemoStarting(true);
    setError("");
    try {
      const run = await post<SubagentDemo>(
        `/api/v1/sessions/${sessionId}/subagent-demo`,
        {},
      );
      queryClient.setQueryData<{ runs: SubagentDemo[] }>(
        ["subagent-runs", sessionId],
        (current) => ({ runs: [...(current?.runs ?? []), run] }),
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setDemoStarting(false);
    }
  };

  const cancelSubagentTask = async (taskId: string) => {
    setError("");
    try {
      await post(`/api/v1/subagent-tasks/${taskId}/cancel`, {});
      await queryClient.invalidateQueries({ queryKey: ["subagent-runs", sessionId] });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  const runSelectionWorker = async () => {
    if (selectionWorkerRunning.current) return;
    selectionWorkerRunning.current = true;
    try {
      while (desiredSelection.current) {
        const request = desiredSelection.current;
        desiredSelection.current = null;
        try {
          const selected = await post<Session>(
            `/api/v1/sessions/${request.id}/select`,
            {},
          );
          if (
            request.epoch !== selectionEpoch.current ||
            desiredSelection.current
          ) {
            continue;
          }
          queryClient.setQueryData<HostStatus>(["status"], (current) =>
            current
              ? {
                  ...current,
                  session_id: selected.session_id,
                  timeline_id: selected.active_timeline_id,
                  active_operation:
                    current.active_operations?.find(
                      (operation) =>
                        operation.session_id === selected.session_id,
                    ) ?? null,
                }
              : current,
          );
          setNewTaskDraft(false);
          setSelectedTurn(null);
          setRestoreTurn(null);
          void refresh(selected.session_id);
        } catch (cause) {
          if (
            request.epoch === selectionEpoch.current &&
            !desiredSelection.current
          ) {
            updateRuntime(request.id, (current) => ({
              ...current,
              error: String(cause),
            }));
          }
        }
      }
    } finally {
      selectionWorkerRunning.current = false;
      if (desiredSelection.current) void runSelectionWorker();
    }
  };

  const selectSession = (id: string) => {
    if (
      id === sessionId &&
      !newTaskDraft &&
      !selectionWorkerRunning.current &&
      !desiredSelection.current
    ) {
      return;
    }
    const epoch = selectionEpoch.current + 1;
    selectionEpoch.current = epoch;
    desiredSelection.current = { id, epoch };
    void runSelectionWorker();
  };

  const decide = async (decision: "approve" | "reject") => {
    if (!approval) return;
    try {
      await post(`/api/v1/approvals/${approval.approval_id}/decision`, {
        operation_id: approval.operation_id,
        request_hash: approval.request_hash,
        decision,
      });
      setApproval(null);
    } catch (cause) {
      setError(String(cause));
    }
  };

  const restore = async () => {
    if (!restoreTurn || !sessionId || !status.data) return;
    if (
      (status.data.active_operations?.length ?? 0) > 0 ||
      status.data.active_operation
    ) {
      setError("WORKSPACE_BUSY: 工作区有任务正在运行，暂时不能恢复历史。");
      return;
    }
    try {
      const operation = await post<{ operation_id: string }>(
        `/api/v1/sessions/${sessionId}/restore`,
        {
          turn_number: restoreTurn.turn_number,
          expected_timeline_id: status.data.timeline_id,
        },
      );
      setRestoreTurn(null);
      setEvents([]);
      setToolRuns({});
      followOperation(sessionId, operation.operation_id);
    } catch (cause) {
      setError(String(cause));
    }
  };

  const applySettings = async (values: SettingsConfig) => {
    setApplyingSettings(true);
    setSettingsError("");
    try {
      const updated = await post<SettingsConfig>("/api/v1/settings", {
        model: values.model,
        base_url: values.base_url,
        langsmith_enabled: values.langsmith_enabled,
        langsmith_project: values.langsmith_project,
        model_timeout_seconds: values.model_timeout_seconds,
        command_timeout_seconds: values.command_timeout_seconds,
        max_parallel_sessions: values.max_parallel_sessions,
      });
      queryClient.setQueryData(["settings"], updated);
      queryClient.setQueryData<HostStatus>(["status"], (current) =>
        current ? { ...current, model: updated.model } : current,
      );
      setSettingsOpen(false);
    } catch (cause) {
      setSettingsError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setApplyingSettings(false);
    }
  };

  const openWorkspace = async (path: string) => {
    const requestedPath = path.trim();
    if (!requestedPath || (status.data?.active_operations?.length ?? 0) > 0) return;
    setOpeningWorkspace(true);
    setWorkspaceError("");
    try {
      const opened = await post<Partial<HostStatus>>("/api/v1/workspaces/open", {
        path: requestedPath,
      });
      queryClient.setQueryData<HostStatus>(["status"], (current) =>
        current ? { ...current, ...opened } : current,
      );
      setWorkspacePath("");
      setWorkspacePickerOpen(false);
      setNewTaskDraft(false);
      setRuntimeBySession({});
      setSelectedTurn(null);
      setSelectedDiff(null);
      setSelectedFile(null);
      queryClient.removeQueries({ queryKey: ["turns"] });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["workspaces"] }),
        queryClient.invalidateQueries({ queryKey: ["sessions"] }),
        queryClient.invalidateQueries({ queryKey: ["workspace-status"] }),
        queryClient.invalidateQueries({ queryKey: ["diff"] }),
        queryClient.invalidateQueries({ queryKey: ["files"] }),
      ]);
    } catch (cause) {
      setWorkspaceError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setOpeningWorkspace(false);
    }
  };

  const removeWorkspace = async (path: string) => {
    try {
      const updated = await post<Workspaces>("/api/v1/workspaces/remove", { path });
      queryClient.setQueryData(["workspaces"], updated);
    } catch (cause) {
      setWorkspaceError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  if (status.isLoading) return <LoadingScreen label="正在连接本地 Agent Host" />;
  if (status.isError) return <FatalScreen message={String(status.error)} />;
  if (!status.data) return <FatalScreen message="Host 未返回状态。" />;
  const host = status.data;
  const workspaceBusy =
    (host.active_operations?.length ?? 0) > 0 ||
    Boolean(host.active_operation);
  const sessionItems = sessions.data?.pages.flatMap((page) => page.items) ?? [];
  const sessionStatuses = new Map(
    (host.active_operations ?? []).flatMap((operation) =>
      operation.session_id
        ? [[operation.session_id, operation.status] as const]
        : [],
    ),
  );
  for (const pending of host.pending_approvals) {
    sessionStatuses.set(pending.session_id, "waiting_approval");
  }
  const visibleTurns = newTaskDraft
    ? []
    : [...(turns.data?.pages ?? [])]
        .reverse()
        .flatMap((page) => page.turns)
        .filter((turn) => turn.user_text || turn.assistant_text);
  const agentRuns = newTaskDraft ? [] : (subagentRuns.data?.runs ?? []);
  const visibleTurnIds = new Set(visibleTurns.map((turn) => turn.turn_id));
  const unboundRuns = agentRuns.filter((run) => run.turn_id === null);
  const pendingRuns = agentRuns.filter(
    (run) => run.turn_id !== null && !visibleTurnIds.has(run.turn_id) && run.status === "running",
  );
  const visibleToolRuns = Object.values(toolRuns);
  const diffFiles = diff.data?.files ?? [];
  const activeDiffMetadata =
    diffFiles.find((item) => item.path === activeDiffPath) ?? diffFiles[0] ?? null;
  const activeDiff = diffContent.data?.files[0] ?? activeDiffMetadata;
  const recentWorkspaces = workspaces.data?.recent ?? [];
  const settingsDialog = settingsOpen ? (
    <SettingsDialog
      value={settings.data}
      loading={settings.isLoading}
      saving={applyingSettings}
      error={settingsError || (settings.isError ? String(settings.error) : "")}
      onCancel={() => setSettingsOpen(false)}
      onApply={(value) => void applySettings(value)}
    />
  ) : null;

  if (!host.workspace) {
    return (
      <div className="app-shell empty-workspace-shell">
        <TopBar
          host={host}
          sidebarOpen={false}
          onToggleSidebar={() => undefined}
          onOpenWorkspace={() => undefined}
          onOpenSettings={() => {
            setSettingsError("");
            setSettingsOpen(true);
          }}
          settingsDisabled={false}
          inspectorOpen={false}
          onToggleInspector={() => undefined}
          hasWorkspace={false}
        />
        <WorkspaceChooser
          recent={recentWorkspaces}
          path={workspacePath}
          error={workspaceError}
          opening={openingWorkspace}
          onPath={setWorkspacePath}
          onOpen={(path) => void openWorkspace(path)}
          onRemove={(path) => void removeWorkspace(path)}
        />
        <StatusBar host={host} />
        {settingsDialog}
      </div>
    );
  }

  return (
    <div className="app-shell">
      <TopBar
        host={host}
        workspace={workspace.data}
        sidebarOpen={sidebarOpen}
        onToggleSidebar={() => setSidebarOpen((value) => !value)}
        onOpenWorkspace={() => {
          setWorkspaceError("");
          setWorkspacePickerOpen(true);
        }}
        onOpenSettings={() => {
          setSettingsError("");
          setSettingsOpen(true);
        }}
        settingsDisabled={(host.active_operations?.length ?? 0) > 0}
        inspectorOpen={inspectorOpen}
        onToggleInspector={() => setInspectorOpen((value) => !value)}
        hasWorkspace
      />
      <MobileNav value={mobileView} onChange={setMobileView} />
      <div
        className={[
          "workspace-grid",
          sidebarOpen ? "" : "sidebar-collapsed",
          inspectorOpen ? "" : "inspector-collapsed",
        ].join(" ")}
      >
        <Sidebar
          visible={mobileView === "tasks"}
          open={sidebarOpen}
          sessions={sessionItems.filter((session) => session.turn_count >= 0)}
          activeId={newTaskDraft ? undefined : (sessionId ?? undefined)}
          sessionStatuses={sessionStatuses}
          disabled={false}
          workspaceName={host.workspace_name ?? host.workspace}
          workspaces={recentWorkspaces}
          onOpenWorkspace={() => setWorkspacePickerOpen(true)}
          onSwitchWorkspace={(path) => void openWorkspace(path)}
          onCreate={startNewTask}
          onSelect={(id) => void selectSession(id)}
          hasMore={sessions.hasNextPage}
          loadingMore={sessions.isFetchingNextPage}
          onLoadMore={() => void sessions.fetchNextPage()}
        />
        <main className={`conversation ${mobileView === "chat" ? "mobile-visible" : ""}`}>
          <ConversationHeader
            running={running}
            turnCount={visibleTurns.length}
            demoRunning={agentRuns.some((run) => run.status === "running")}
            demoStarting={demoStarting}
            onStartDemo={() => void startSubagentDemo()}
          />
          <div
            className="messages"
            ref={messages}
            onScroll={() => {
              const element = messages.current;
              if (!element) return;
              followMessages.current =
                element.scrollHeight - element.scrollTop - element.clientHeight < 80;
            }}
          >
            {turns.hasNextPage && !newTaskDraft ? (
              <button
                className="history-load"
                onClick={() => void turns.fetchNextPage()}
                disabled={turns.isFetchingNextPage}
              >
                {turns.isFetchingNextPage ? (
                  <LoaderCircle size={13} className="spin" />
                ) : (
                  <History size={13} />
                )}
                {turns.isFetchingNextPage ? "正在加载" : "加载更早记录"}
              </button>
            ) : null}
            {turns.isError || subagentRuns.isError ? (
              <InlineQueryError
                message="对话记录加载失败"
                onRetry={() => {
                  void turns.refetch();
                  void subagentRuns.refetch();
                }}
              />
            ) : null}
            {visibleTurns.length === 0 && !running && agentRuns.length === 0 ? (
              <EmptyConversation />
            ) : null}
            {visibleTurns.map((turn) => (
              <div className="turn-group" key={turn.turn_id}>
                <TurnBlock
                  turn={turn}
                  selected={selectedTurn?.turn_id === turn.turn_id}
                  onInspect={() => setSelectedTurn(turn)}
                  onRestore={() => setRestoreTurn(turn)}
                  restoreDisabled={workspaceBusy}
                />
                {agentRuns
                  .filter((run) => run.turn_id === turn.turn_id)
                  .map((run) => (
                    <SubagentDemoPanel
                      demo={run}
                      key={run.run_id}
                      onCancel={(taskId) => void cancelSubagentTask(taskId)}
                    />
                  ))}
              </div>
            ))}
            {unboundRuns.map((run) => (
              <SubagentDemoPanel
                demo={run}
                key={run.run_id}
                onCancel={(taskId) => void cancelSubagentTask(taskId)}
              />
            ))}
            {pendingTurn ? (
              <TurnBlock
                turn={pendingTurn}
                selected={false}
                onInspect={() => undefined}
                onRestore={() => undefined}
                restoreDisabled
              />
            ) : null}
            {running ? (
              <RunningBlock
                text={streamText}
                events={events}
                operationId={operationId}
                toolRuns={visibleToolRuns}
                connectionState={runtime.connectionState}
              />
            ) : null}
            {pendingRuns.map((run) => (
              <SubagentDemoPanel
                demo={run}
                key={run.run_id}
                onCancel={(taskId) => void cancelSubagentTask(taskId)}
              />
            ))}
            {error ? (
              <div className="error-banner" role="alert">
                <AlertCircle size={17} />
                <span>{error}</span>
                <button className="icon-button" onClick={() => setError("")} title="关闭错误">
                  <X size={16} />
                </button>
              </div>
            ) : null}
            <div ref={messageEnd} />
          </div>
          <div className="composer-stack">
            {approval ? (
              <ApprovalPanel
                approval={approval}
                onDecision={(value) => void decide(value)}
              />
            ) : null}
            <Composer
              value={draft}
              disabled={running}
              cancelling={cancelling}
              onChange={setDraft}
              onSubmit={(event) => void sendTurn(event)}
              onCancel={() => void cancelOperation()}
            />
          </div>
        </main>
        <aside
          className={[
            "inspector",
            inspectorOpen ? "" : "collapsed",
            mobileView === "workspace" ? "mobile-visible" : "",
          ].join(" ")}
        >
          <Inspector
            tab={inspectorTab}
            onTab={setInspectorTab}
            diffFiles={diffFiles}
            activeDiff={activeDiff}
            onDiff={setSelectedDiff}
            files={files.data?.files ?? []}
            selectedFile={selectedFile}
            onFile={setSelectedFile}
            fileContent={fileContent.data}
            spans={trace.data?.spans ?? []}
            traceLoading={trace.isLoading}
            diffError={diff.isError || diffContent.isError}
            filesError={files.isError || fileContent.isError}
            traceError={trace.isError}
            onRetryDiff={() => {
              void diff.refetch();
              void diffContent.refetch();
            }}
            onRetryFiles={() => {
              void files.refetch();
              void fileContent.refetch();
            }}
            onRetryTrace={() => void trace.refetch()}
            selectedTurn={selectedTurn}
          />
          <InspectorFooter workspace={workspace.data} />
        </aside>
      </div>
      <StatusBar host={host} workspace={workspace.data} />
      {restoreTurn ? (
        <RestoreDialog
          turn={restoreTurn}
          disabled={workspaceBusy}
          onCancel={() => setRestoreTurn(null)}
          onConfirm={() => void restore()}
        />
      ) : null}
      {workspacePickerOpen ? (
        <WorkspaceDialog
          recent={recentWorkspaces}
          path={workspacePath}
          error={workspaceError}
          opening={openingWorkspace}
          onPath={setWorkspacePath}
          onOpen={(path) => void openWorkspace(path)}
          onRemove={(path) => void removeWorkspace(path)}
          onCancel={() => setWorkspacePickerOpen(false)}
        />
      ) : null}
      {settingsDialog}
    </div>
  );
}

function TopBar({
  host,
  workspace,
  sidebarOpen,
  onToggleSidebar,
  onOpenWorkspace,
  onOpenSettings,
  settingsDisabled,
  inspectorOpen,
  onToggleInspector,
  hasWorkspace,
}: {
  host: HostStatus;
  workspace?: WorkspaceStatus;
  sidebarOpen: boolean;
  onToggleSidebar: () => void;
  onOpenWorkspace: () => void;
  onOpenSettings: () => void;
  settingsDisabled: boolean;
  inspectorOpen: boolean;
  onToggleInspector: () => void;
  hasWorkspace: boolean;
}) {
  return (
    <header className="topbar">
      <button
        className="icon-button sidebar-toggle"
        onClick={onToggleSidebar}
        disabled={!hasWorkspace}
        title={sidebarOpen ? "收起任务栏" : "展开任务栏"}
      >
        <Menu size={18} />
      </button>
      <div className="product-mark">
        <Code2 size={19} />
        <strong>Coding Agent</strong>
      </div>
      <div className="top-context">
        <span className="workspace-name">{host.workspace_name ?? "未打开目录"}</span>
        {hasWorkspace ? (
          <>
            <span className="context-divider" />
            <span className="branch">
              <GitBranch size={14} />
              {workspace?.branch ?? host.branch}
            </span>
          </>
        ) : null}
      </div>
      <div className="connection-state">
        <span className="online-dot" />
        本地服务
      </div>
      <button className="icon-button" onClick={onOpenWorkspace} title="打开或切换目录">
        <FolderOpen size={18} />
      </button>
      <button
        className="icon-button inspector-toggle"
        onClick={onToggleInspector}
        disabled={!hasWorkspace}
        title={inspectorOpen ? "收起工作区" : "展开工作区"}
      >
        {inspectorOpen ? <PanelRightClose size={18} /> : <PanelRightOpen size={18} />}
      </button>
      <button
        className="icon-button"
        onClick={onOpenSettings}
        disabled={settingsDisabled}
        title={settingsDisabled ? "任务执行期间不能修改设置" : "设置"}
      >
        <Settings size={18} />
      </button>
    </header>
  );
}

function MobileNav({
  value,
  onChange,
}: {
  value: MobileView;
  onChange: (value: MobileView) => void;
}) {
  const items: Array<[MobileView, typeof MessageSquare, string]> = [
    ["tasks", History, "任务"],
    ["chat", MessageSquare, "对话"],
    ["workspace", FileDiff, "工作区"],
  ];
  return (
    <nav className="mobile-nav">
      {items.map(([id, Icon, label]) => (
        <button className={value === id ? "active" : ""} onClick={() => onChange(id)} key={id}>
          <Icon size={16} />
          {label}
        </button>
      ))}
    </nav>
  );
}

function Sidebar({
  visible,
  open,
  sessions,
  activeId,
  sessionStatuses,
  disabled,
  workspaceName,
  workspaces,
  onOpenWorkspace,
  onSwitchWorkspace,
  onCreate,
  onSelect,
  hasMore,
  loadingMore,
  onLoadMore,
}: {
  visible: boolean;
  open: boolean;
  sessions: Session[];
  activeId?: string;
  sessionStatuses: Map<string, string>;
  disabled: boolean;
  workspaceName: string;
  workspaces: Workspaces["recent"];
  onOpenWorkspace: () => void;
  onSwitchWorkspace: (path: string) => void;
  onCreate: () => void;
  onSelect: (id: string) => void;
  hasMore: boolean;
  loadingMore: boolean;
  onLoadMore: () => void;
}) {
  return (
    <aside className={`sidebar ${open ? "" : "hidden"} ${visible ? "mobile-visible" : ""}`}>
      <div className="sidebar-actions">
        <button className="primary-button full" onClick={onCreate} disabled={disabled}>
          <Plus size={16} />
          新建任务
        </button>
      </div>
      <div className="nav-section">
        <div className="section-label">当前项目</div>
        <button className="project-row active" onClick={onOpenWorkspace} disabled={disabled}>
          <span className="project-icon">CA</span>
          <span>
            <strong>{workspaceName}</strong>
            <small>切换工作目录</small>
          </span>
          <ChevronDown size={14} />
        </button>
        {workspaces.filter((item) => !item.active).slice(0, 3).map((item) => (
          <button
            className="workspace-shortcut"
            key={item.path}
            onClick={() => onSwitchWorkspace(item.path)}
            disabled={disabled || !item.available}
            title={item.path}
          >
            <Folder size={13} />
            <span>{item.name}</span>
          </button>
        ))}
      </div>
      <div className="session-section">
        <div className="section-label">
          <span>最近任务</span>
          <button className="icon-button compact" disabled title="任务搜索（暂未实现）">
            <Search size={14} />
          </button>
        </div>
        <div className="session-list">
          {sessions.map((session) => {
            const sessionStatus = sessionStatuses.get(session.session_id);
            return (
              <button
                key={session.session_id}
                className={`session-row ${session.session_id === activeId ? "active" : ""}`}
                onClick={() => onSelect(session.session_id)}
                disabled={disabled}
              >
                <MessageSquare size={15} />
                <span>
                  <strong>{session.title}</strong>
                  <small>
                    {session.turn_count} 轮 · {relativeTime(session.created_at)}
                    {sessionStatus ? ` · ${operationStatusLabel(sessionStatus)}` : ""}
                  </small>
                </span>
                {sessionStatus ? (
                  <LoaderCircle
                    size={13}
                    className="spin"
                    aria-label={operationStatusLabel(sessionStatus)}
                  />
                ) : session.session_id === activeId ? (
                  <CircleDot size={13} />
                ) : null}
              </button>
            );
          })}
          {hasMore ? (
            <button
              className="session-load-more"
              onClick={onLoadMore}
              disabled={disabled || loadingMore}
            >
              {loadingMore ? <LoaderCircle size={13} className="spin" /> : <History size={13} />}
              {loadingMore ? "正在加载" : "更多任务"}
            </button>
          ) : null}
        </div>
      </div>
      <div className="sidebar-account">
        <span className="avatar">
          <User size={15} />
        </span>
        <span>
          <strong>本地用户</strong>
          <small>仅此设备</small>
        </span>
        <MoreHorizontal size={16} />
      </div>
    </aside>
  );
}

function ConversationHeader({
  running,
  turnCount,
  demoRunning,
  demoStarting,
  onStartDemo,
}: {
  running: boolean;
  turnCount: number;
  demoRunning: boolean;
  demoStarting: boolean;
  onStartDemo: () => void;
}) {
  return (
    <div className="conversation-header">
      <div>
        <h1>{turnCount ? "当前任务" : "新任务"}</h1>
        <span className={`status-pill ${running ? "running" : ""}`}>
          {running ? <LoaderCircle size={12} className="spin" /> : <Check size={12} />}
          {running ? "执行中" : "就绪"}
        </span>
      </div>
      <button
        className="secondary-button demo-launch"
        onClick={onStartDemo}
        disabled={demoRunning || demoStarting}
      >
        {demoRunning || demoStarting ? (
          <LoaderCircle size={14} className="spin" />
        ) : (
          <Play size={14} />
        )}
        {demoRunning ? "双 Agent 执行中" : demoStarting ? "正在启动" : "运行双 Agent Demo"}
      </button>
    </div>
  );
}

function EmptyConversation() {
  return (
    <div className="empty-conversation">
      <div className="empty-icon">
        <Bot size={26} />
      </div>
      <h2>从一个具体任务开始</h2>
      <p>描述要修改的代码、期望行为和验证方式。</p>
    </div>
  );
}

function SubagentDemoPanel({
  demo,
  onCancel,
}: {
  demo: SubagentDemo;
  onCancel: (taskId: string) => void;
}) {
  const completed = demo.tasks.filter((task) => task.status === "merged").length;
  return (
    <section className="subagent-demo" aria-label="双子 Agent 演示">
      <div className="subagent-demo-header">
        <div>
          <span className="subagent-kicker">SUBAGENT RUN</span>
          <strong>并行交付与验收</strong>
        </div>
        <div className="subagent-run-state">
          {demo.status === "running" ? (
            <LoaderCircle size={14} className="spin" />
          ) : (
            <Check size={14} />
          )}
          {completed}/{demo.expected_task_count ?? demo.tasks.length} 已集成
        </div>
      </div>
      <div className="subagent-grid">
        {demo.tasks.map((task) => (
          <SubagentCard task={task} key={task.task_id} onCancel={onCancel} />
        ))}
      </div>
    </section>
  );
}

function SubagentCard({
  task,
  onCancel,
}: {
  task: SubagentTask;
  onCancel: (taskId: string) => void;
}) {
  const activeAttempt =
    task.attempts.find((attempt) => attempt.attempt_id === task.active_attempt_id) ??
    task.attempts.at(-1);
  const running = ["queued", "running", "cancelling"].includes(task.status);
  const cancellable = [
    "queued",
    "running",
    "awaiting_review",
    "revision_required",
    "waiting_parent",
    "waiting_capability",
  ].includes(task.status);
  const failed = task.status === "failed";
  const result = [...task.events]
    .reverse()
    .find((event) => event.event_type === "result.committed");
  const review = [...task.events]
    .reverse()
    .find((event) =>
      ["review.accepted", "review.rejected", "integration.failed"].includes(event.event_type),
    );
  const changedPaths = Array.isArray(result?.payload.changed_paths)
    ? result.payload.changed_paths.map(String)
    : [];
  const summary =
    activeAttempt?.result?.summary ??
    (typeof result?.payload.summary === "string" ? result.payload.summary : null);
  const findings = Array.isArray(review?.payload.findings)
    ? review.payload.findings.map(String)
    : [];
  const elapsed = activeAttempt?.started_at
    ? formatDuration(
        Math.max(
          0,
          Date.parse(activeAttempt.ended_at ?? new Date().toISOString()) -
            Date.parse(activeAttempt.started_at),
        ),
      )
    : "等待调度";
  return (
    <article
      className={`subagent-card ${task.status === "merged" ? "accepted" : ""} ${
        failed ? "failed" : ""
      }`}
    >
      <header>
        <span className="subagent-avatar">
          <Bot size={16} />
        </span>
        <div>
          <strong>{task.name}</strong>
          <small>{task.objective}</small>
        </div>
        <span className={`subagent-status ${task.status}`}>
          {running ? <LoaderCircle size={12} className="spin" /> : null}
          {taskStatusLabel(task.status)}
        </span>
        {cancellable ? (
          <button
            className="icon-button compact subagent-cancel"
            onClick={() => onCancel(task.task_id)}
            title="取消此子任务"
            aria-label={`取消 ${task.name}`}
          >
            <X size={13} />
          </button>
        ) : null}
      </header>
      <div className="subagent-meta">
        <span>
          <Clock3 size={12} />
          {elapsed}
        </span>
        <span>
          <GitBranch size={12} />
          Attempt {activeAttempt?.attempt_number ?? 1}
        </span>
        <span>
          <Wrench size={12} />
          {activeAttempt?.allowed_tools.length ?? 0} tools
        </span>
      </div>
      <div className="tool-grants">
        <Wrench size={11} />
        <code>{activeAttempt?.allowed_tools.join(" · ") ?? "等待授权"}</code>
      </div>
      <div className="attempt-strip">
        {task.attempts.map((attempt) => (
          <span className={attempt.status} key={attempt.attempt_id}>
            #{attempt.attempt_number} {attemptStatusLabel(attempt.status)}
          </span>
        ))}
      </div>
      {task.feedback ? (
        <div className="subagent-feedback">
          <AlertCircle size={14} />
          <span>
            <strong>主 Agent 反馈</strong>
            {task.feedback}
          </span>
        </div>
      ) : null}
      <details className="subagent-details">
        <summary>
          <span>任务契约与结果证据</span>
          <ChevronDown size={13} />
        </summary>
        <div>
          <strong>允许范围</strong>
          <code>{task.scope?.join(" · ") || task.output_path}</code>
        </div>
        <div>
          <strong>验收条件</strong>
          <ul>
            {(task.acceptance ?? []).map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
        {changedPaths.length ? (
          <div>
            <strong>改动文件</strong>
            <code>{changedPaths.join(" · ")}</code>
          </div>
        ) : null}
        {summary ? (
          <div>
            <strong>子 Agent 总结</strong>
            <p>{summary}</p>
          </div>
        ) : null}
        {findings.length ? (
          <div>
            <strong>验收发现</strong>
            <ul>
              {findings.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {activeAttempt?.result?.checks.length ? (
          <div>
            <strong>检查</strong>
            <ul>
              {activeAttempt.result.checks.map((item, index) => (
                <li key={`${item.command}-${index}`}>
                  {item.command}：{item.exit_code === 0 ? "通过" : `退出码 ${item.exit_code ?? "未知"}`}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {activeAttempt?.result?.evidence.length ? (
          <div>
            <strong>证据</strong>
            <ul>
              {activeAttempt.result.evidence.map((item, index) => (
                <li key={`${item.title}-${index}`}>{item.title}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {activeAttempt?.result?.risks.length ? (
          <div>
            <strong>风险</strong>
            <ul>
              {activeAttempt.result.risks.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </div>
        ) : null}
        {activeAttempt?.result?.provenance.length ? (
          <div>
            <strong>来源</strong>
            <ul>
              {activeAttempt.result.provenance.map((item, index) => (
                <li key={`${item.provider}-${item.tool ?? index}`}>
                  {item.provider}
                  {item.tool ? ` / ${item.tool}` : ""}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {activeAttempt?.result?.legacy_raw_output ? (
          <div className="legacy-result">
            <strong>Legacy 原始输出</strong>
            <pre>{activeAttempt.result.legacy_raw_output}</pre>
          </div>
        ) : null}
        {activeAttempt?.error ? (
          <div className="subagent-detail-error">
            <strong>错误</strong>
            <p>{activeAttempt.error}</p>
          </div>
        ) : null}
      </details>
      <div className="subagent-events">
        {task.events.slice(-12).map((event) => (
          <SubagentEventRow event={event} key={event.event_id} />
        ))}
      </div>
      <footer>
        <code>{task.output_path}</code>
        {task.integration_commit ? (
          <span title={task.integration_commit}>
            <GitCommitHorizontal size={12} />
            {task.integration_commit.slice(0, 8)}
          </span>
        ) : null}
      </footer>
    </article>
  );
}

function SubagentEventRow({ event }: { event: SubagentEvent }) {
  const terminal = [
    "tool.completed",
    "result.committed",
    "review.accepted",
    "integration.completed",
  ].includes(event.event_type);
  const rejected = event.event_type === "review.rejected";
  return (
    <div className={rejected ? "rejected" : ""}>
      <span className="event-marker">
        {rejected ? (
          <X size={11} />
        ) : terminal ? (
          <Check size={11} />
        ) : (
          <CircleDot size={10} />
        )}
      </span>
      <span>{subagentEventLabel(event)}</span>
      <time>{formatTime(event.created_at)}</time>
    </div>
  );
}

function taskStatusLabel(status: string) {
  const labels: Record<string, string> = {
    queued: "排队中",
    running: "执行中",
    cancelling: "取消中",
    waiting_parent: "等待主 Agent",
    waiting_capability: "等待工具授权",
    awaiting_review: "验收中",
    revision_required: "等待返工",
    integrating: "集成中",
    merged: "已验收",
    failed: "失败",
    cancelled: "已取消",
  };
  return labels[status] ?? status;
}

function attemptStatusLabel(status: string) {
  const labels: Record<string, string> = {
    queued: "排队",
    running: "执行",
    waiting_parent: "等待回复",
    waiting_capability: "等待授权",
    completed: "待验收",
    rejected: "未通过",
    accepted: "通过",
    failed: "失败",
    cancelled: "已取消",
  };
  return labels[status] ?? status;
}

function subagentEventLabel(event: SubagentEvent) {
  const payload = event.payload;
  switch (event.event_type) {
    case "attempt.queued":
      return `Attempt ${payload.attempt_number} 已进入队列`;
    case "agent.started":
      return payload.workspace_state === "ready" ? "独立 worktree 已就绪" : "只读分析已开始";
    case "workspace.allocated":
      return "已按需创建独立 worktree";
    case "plan.updated":
      return String(payload.summary ?? "执行计划已更新");
    case "tool.started":
      return `调用 ${String(payload.tool ?? "tool")}`;
    case "tool.progress":
      return `${String(payload.tool ?? "tool")} ${String(payload.elapsed_seconds ?? 0)}/${String(
        payload.total_seconds ?? 0,
      )}s`;
    case "tool.completed":
      return `${String(payload.tool ?? "tool")} 执行完成`;
    case "result.committed":
      return payload.result_commit
        ? `结果已提交 ${String(payload.result_commit).slice(0, 8)}`
        : "分析结果已提交";
    case "review.started":
      return "主 Agent 开始验收";
    case "review.rejected":
      return "验收未通过，要求返工";
    case "parent.feedback":
      return "已接收主 Agent 反馈";
    case "parent_input.requested":
      return `请求主 Agent 信息：${String(payload.question ?? "")}`;
    case "capability.requested":
      return "请求额外工具授权";
    case "capability.granted":
      return "主 Agent 已追加工具授权";
    case "parent.response":
      return "已收到主 Agent 回复并恢复";
    case "review.accepted":
      return "验收通过";
    case "integration.completed":
      return "结果已集成";
    case "integration.failed":
      return `集成失败：${String(payload.error ?? "")}`;
    case "cancellation.requested":
      return "主 Agent 已请求取消";
    case "agent.cancelled":
      return "子任务已取消";
    case "agent.failed":
      return `执行失败：${String(payload.error ?? "")}`;
    default:
      return event.event_type;
  }
}

function TurnBlock({
  turn,
  selected,
  onInspect,
  onRestore,
  restoreDisabled,
}: {
  turn: Turn;
  selected: boolean;
  onInspect: () => void;
  onRestore: () => void;
  restoreDisabled: boolean;
}) {
  const cancelled = turn.status === "cancelled";
  return (
    <article
      className={`turn-block ${selected ? "selected" : ""} ${cancelled ? "cancelled" : ""}`}
    >
      <div className="message user-message">
        <div className="message-body">
          <div className="message-meta">
            <strong>你</strong>
            <time>{formatTime(turn.created_at)}</time>
          </div>
          <p>{turn.user_text}</p>
        </div>
        <span className="message-avatar">
          <User size={15} />
        </span>
      </div>
      {turn.assistant_text || cancelled ? (
        <div className="message agent-message">
          <span className="message-avatar agent">
            <Bot size={15} />
          </span>
          <div>
            <div className="message-meta">
              <strong>Agent</strong>
              <span className={cancelled ? "cancelled-label" : "completed-label"}>
                {cancelled ? <X size={12} /> : <Check size={12} />}
                {cancelled ? " 已取消" : " 已完成"}
              </span>
            </div>
            {turn.assistant_text ? (
              <MarkdownMessage>{turn.assistant_text}</MarkdownMessage>
            ) : null}
            <div className="message-actions">
              {!cancelled ? (
                <button onClick={onInspect}>
                  <Play size={13} /> 执行详情
                </button>
              ) : null}
              <button
                onClick={onRestore}
                disabled={restoreDisabled}
                title={
                  restoreDisabled
                    ? "工作区有任务正在运行，暂时不能恢复"
                    : "恢复到此轮"
                }
              >
                <RotateCcw size={13} /> 恢复到此轮
              </button>
              <code>{turn.snapshot_oid.slice(0, 8)}</code>
            </div>
          </div>
        </div>
      ) : null}
    </article>
  );
}

function RunningBlock({
  text,
  events,
  operationId,
  toolRuns,
  connectionState,
}: {
  text: string;
  events: StreamEvent[];
  operationId: string | null;
  toolRuns: ToolRunView[];
  connectionState: StreamConnectionState;
}) {
  const steps = events.filter((event) =>
    [
      "turn.started",
      "step.started",
      "step.completed",
      "approval.required",
      "context.window_usage",
    ].includes(event.type),
  );
  return (
    <div className="running-block">
      <div className="message agent-message">
        <span className="message-avatar agent">
          <Bot size={15} />
        </span>
        <div>
          <div className="message-meta">
            <strong>Agent</strong>
            <span className="running-label">
              <LoaderCircle size={12} className="spin" />
              {connectionState === "reconnecting" ? "正在重连" : "正在处理"}
            </span>
          </div>
          {text ? (
            <div className="streaming">
              <MarkdownMessage>{text}</MarkdownMessage>
            </div>
          ) : null}
          {toolRuns.length ? (
            <div className="tool-run-grid" aria-label="并行工具">
              {toolRuns.map((run) => (
                <ToolRunCard run={run} key={run.run_id} />
              ))}
            </div>
          ) : null}
          <div className="live-steps">
            {steps.length ? (
              steps.map((step, index) => (
                <div key={`${step.type}-${index}`}>
                  <Check size={13} />
                  <span>{eventLabel(step)}</span>
                </div>
              ))
            ) : (
              <div>
                <LoaderCircle size={13} className="spin" />
                <span>分析任务并准备上下文</span>
              </div>
            )}
          </div>
          {operationId ? <code className="operation-id">op {operationId.slice(0, 8)}</code> : null}
        </div>
      </div>
    </div>
  );
}

function ToolRunCard({ run }: { run: ToolRunView }) {
  const terminal = ["completed", "failed", "cancelled"].includes(run.status);
  const failed = run.status === "failed" || run.status === "cancelled";
  const detail =
    run.output.trim() ||
    (terminal && run.result_preview ? run.result_preview : "") ||
    (run.status === "queued" ? "等待执行" : "等待输出");
  const statusLabel: Record<string, string> = {
    queued: "排队中",
    running: "运行中",
    cancelling: "取消中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  };
  return (
    <article className={`tool-run-card ${terminal ? "terminal" : ""} ${failed ? "failed" : ""}`}>
      <header>
        <span className="tool-run-icon">
          {terminal ? (
            failed ? (
              <X size={14} />
            ) : (
              <Check size={14} />
            )
          ) : (
            <LoaderCircle size={14} className="spin" />
          )}
        </span>
        <strong title={run.tool}>{run.tool}</strong>
        <span className="tool-run-status">{statusLabel[run.status] ?? run.status}</span>
      </header>
      <div className="tool-run-meta">
        <span>
          <Wrench size={11} />
          {effectLabel(run.effect)}
        </span>
        <code>{run.run_id.slice(0, 8)}</code>
        {run.duration_ms != null ? (
          <span>
            <Clock3 size={11} />
            {formatDuration(run.duration_ms)}
          </span>
        ) : null}
      </div>
      <pre>{detail}</pre>
      {run.execution_group_id || run.output_truncated ? (
        <footer title={run.execution_group_id ?? undefined}>
          {run.output_truncated ? "输出已截断" : null}
          {run.output_truncated && run.execution_group_id ? " · " : null}
          {run.execution_group_id ? `组 ${run.execution_group_id}` : null}
        </footer>
      ) : null}
    </article>
  );
}

function Composer({
  value,
  disabled,
  cancelling,
  onChange,
  onSubmit,
  onCancel,
}: {
  value: string;
  disabled: boolean;
  cancelling: boolean;
  onChange: (value: string) => void;
  onSubmit: (event: FormEvent) => void;
  onCancel: () => void;
}) {
  return (
    <form className="composer-wrap" onSubmit={onSubmit}>
      <div className={`composer ${disabled ? "disabled" : ""}`}>
        <textarea
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
          placeholder={disabled ? "Agent 正在执行当前任务…" : "描述你希望 Agent 完成的任务"}
          disabled={disabled}
          rows={3}
        />
        <div className="composer-toolbar">
          <div>
            <button type="button" className="icon-button" disabled title="附件（暂未实现）">
              <Paperclip size={17} />
            </button>
            <button type="button" className="mode-button" disabled title="模式切换（暂未实现）">
              Agent 模式 <ChevronDown size={13} />
            </button>
            <label className="auto-toggle" title="自动模式（暂未实现）">
              <input type="checkbox" disabled />
              <span />
              自动
            </label>
          </div>
          <button
            className={`send-button ${disabled ? "stop" : ""}`}
            type={disabled ? "button" : "submit"}
            onClick={disabled ? onCancel : undefined}
            disabled={disabled ? cancelling : !value.trim()}
            title={disabled ? "停止当前任务" : "发送任务"}
          >
            {disabled ? (
              cancelling ? (
                <LoaderCircle size={17} className="spin" />
              ) : (
                <Square size={15} fill="currentColor" />
              )
            ) : (
              <Send size={17} />
            )}
          </button>
        </div>
      </div>
      <small>Enter 发送，Shift + Enter 换行</small>
    </form>
  );
}

function Inspector({
  tab,
  onTab,
  diffFiles,
  activeDiff,
  onDiff,
  files,
  selectedFile,
  onFile,
  fileContent,
  spans,
  traceLoading,
  diffError,
  filesError,
  traceError,
  onRetryDiff,
  onRetryFiles,
  onRetryTrace,
  selectedTurn,
}: {
  tab: InspectorTab;
  onTab: (tab: InspectorTab) => void;
  diffFiles: DiffFile[];
  activeDiff: DiffFile | null;
  onDiff: (path: string) => void;
  files: WorkspaceFile[];
  selectedFile: string | null;
  onFile: (path: string) => void;
  fileContent?: FileContent;
  spans: TraceSpan[];
  traceLoading: boolean;
  diffError: boolean;
  filesError: boolean;
  traceError: boolean;
  onRetryDiff: () => void;
  onRetryFiles: () => void;
  onRetryTrace: () => void;
  selectedTurn: Turn | null;
}) {
  const [bottomTab, setBottomTab] = useState<"terminal" | "problems">("terminal");
  const commands = useMemo(() => spans.filter((span) => span.kind === "tool"), [spans]);
  return (
    <>
      <section className="inspector-top">
        <div className="tabbar">
          <button className={tab === "changes" ? "active" : ""} onClick={() => onTab("changes")}>
            改动 <span>{diffFiles.length}</span>
          </button>
          <button className={tab === "files" ? "active" : ""} onClick={() => onTab("files")}>
            文件
          </button>
          <button
            className={tab === "preview" ? "active" : ""}
            onClick={() => onTab("preview")}
            title="安全预览将在后续版本提供"
          >
            预览 <small>未开放</small>
          </button>
        </div>
        {tab === "changes" && diffError ? (
          <InlineQueryError message="Diff 加载失败" onRetry={onRetryDiff} />
        ) : tab === "changes" ? (
          <DiffViewer files={diffFiles} active={activeDiff} onSelect={onDiff} />
        ) : null}
        {tab === "files" && filesError ? (
          <InlineQueryError message="文件内容加载失败" onRetry={onRetryFiles} />
        ) : tab === "files" ? (
          <FileViewer
            files={files}
            selected={selectedFile}
            content={fileContent}
            onSelect={onFile}
          />
        ) : null}
        {tab === "preview" ? (
          <Unavailable
            icon={Monitor}
            title="预览尚未开放"
            detail="需要隔离沙箱与独立 Origin 后才能安全执行仓库应用。"
          />
        ) : null}
      </section>
      <div className="panel-resizer" />
      <section className="inspector-bottom">
        <div className="tabbar compact-tabs">
          <button
            className={bottomTab === "terminal" ? "active" : ""}
            onClick={() => setBottomTab("terminal")}
          >
            <TerminalSquare size={14} /> 命令输出
          </button>
          <button
            className={bottomTab === "problems" ? "active" : ""}
            onClick={() => setBottomTab("problems")}
          >
            问题 <span>0</span>
          </button>
        </div>
        {bottomTab === "terminal" && traceError ? (
          <InlineQueryError message="执行详情加载失败" onRetry={onRetryTrace} />
        ) : bottomTab === "terminal" ? (
          <CommandOutput commands={commands} loading={traceLoading} selectedTurn={selectedTurn} />
        ) : (
          <Unavailable
            icon={Check}
            title="没有诊断问题"
            detail="静态诊断聚合将在后续版本接入。"
          />
        )}
      </section>
    </>
  );
}

function DiffViewer({
  files,
  active,
  onSelect,
}: {
  files: DiffFile[];
  active: DiffFile | null;
  onSelect: (path: string) => void;
}) {
  if (!files.length) {
    return <Unavailable icon={FileDiff} title="工作区无改动" detail="Agent 修改文件后会在此显示 Diff。" />;
  }
  return (
    <div className="split-view">
      <div className="file-list">
        <div className="list-heading">已更改文件</div>
        {files.map((file) => (
          <button
            key={file.path}
            className={active?.path === file.path ? "active" : ""}
            onClick={() => onSelect(file.path)}
          >
            <FileCode2 size={14} />
            <span title={file.path}>{file.path}</span>
            <small className="diff-stat">
              <b>+{file.additions}</b> <i>-{file.deletions}</i>
            </small>
          </button>
        ))}
      </div>
      <div className="code-pane">
        <div className="code-header">
          <span>{active?.path}</span>
          <div>
            {active?.truncated ? (
              <small className="truncation-label">
                <AlertCircle size={11} /> 仅显示前 1 MiB
              </small>
            ) : null}
            <span className="added">+{active?.additions}</span>
            <span className="deleted">-{active?.deletions}</span>
          </div>
        </div>
        <pre className="diff-code">
          {(active?.patch ?? "").split("\n").map((line, index) => (
            <code
              className={
                line.startsWith("+") && !line.startsWith("+++")
                  ? "line-add"
                  : line.startsWith("-") && !line.startsWith("---")
                    ? "line-delete"
                    : line.startsWith("@@")
                      ? "line-hunk"
                      : ""
              }
              key={index}
            >
              <span>{index + 1}</span>
              {line || " "}
            </code>
          ))}
        </pre>
      </div>
    </div>
  );
}

function FileViewer({
  files,
  selected,
  content,
  onSelect,
}: {
  files: WorkspaceFile[];
  selected: string | null;
  content?: FileContent;
  onSelect: (path: string) => void;
}) {
  return (
    <div className="split-view">
      <div className="file-list">
        <div className="list-heading">工作区文件</div>
        {files.map((file) => (
          <button
            key={file.path}
            className={selected === file.path ? "active" : ""}
            onClick={() => onSelect(file.path)}
          >
            <File size={14} />
            <span title={file.path}>{file.path}</span>
          </button>
        ))}
      </div>
      <div className="code-pane">
        {content ? (
          <>
            <div className="code-header">
              <span>{content.path}</span>
              <div>
                {content.truncated ? (
                  <small className="truncation-label">
                    <AlertCircle size={11} /> 内容已截断
                  </small>
                ) : null}
                <small>{formatBytes(content.byte_size)}</small>
              </div>
            </div>
            {content.binary ? (
              <Unavailable icon={Files} title="二进制文件" detail="此文件不能作为文本预览。" />
            ) : (
              <pre className="file-code">
                {content.content.split("\n").map((line, index) => (
                  <code key={index}>
                    <span>{index + 1}</span>
                    {line || " "}
                  </code>
                ))}
              </pre>
            )}
          </>
        ) : (
          <Unavailable icon={FileCode2} title="选择文件" detail="文件以只读方式打开。" />
        )}
      </div>
    </div>
  );
}

function CommandOutput({
  commands,
  loading,
  selectedTurn,
}: {
  commands: TraceSpan[];
  loading: boolean;
  selectedTurn: Turn | null;
}) {
  if (loading) return <div className="terminal-empty"><LoaderCircle className="spin" size={17} />加载执行详情</div>;
  if (!selectedTurn) {
    return <Unavailable icon={TerminalSquare} title="选择一个已完成轮次" detail="命令和工具输出将显示在这里。" />;
  }
  if (!commands.length) {
    return <Unavailable icon={Check} title="本轮未执行工具" detail="模型没有产生工具调用。" />;
  }
  return (
    <div className="command-list">
      {commands.map((span) => (
        <details key={span.span_id} open>
          <summary>
            <span className={`command-status ${span.status}`}>
              {span.status === "ok" ? <Check size={12} /> : <X size={12} />}
            </span>
            <strong>{span.name}</strong>
            {span.attributes_json_truncated ? (
              <span className="truncation-label">输出已截断</span>
            ) : null}
            <time>{duration(span.started_at, span.ended_at)}</time>
          </summary>
          <pre>{prettyAttributes(span.attributes_json)}</pre>
        </details>
      ))}
    </div>
  );
}

function InspectorFooter({ workspace }: { workspace?: WorkspaceStatus }) {
  return (
    <div className="inspector-footer">
      <button disabled title="恢复需要从某个历史轮次发起">
        <RotateCcw size={14} /> 撤销改动
      </button>
      <button disabled title="Git commit 将在后续版本提供">
        <GitBranch size={14} /> 提交改动
      </button>
      <span>
        {workspace?.changed_file_count ?? 0} 个文件
        <b> +{workspace?.added_lines ?? 0}</b>
        <i> -{workspace?.deleted_lines ?? 0}</i>
      </span>
    </div>
  );
}

function ApprovalPanel({
  approval,
  onDecision,
}: {
  approval: Approval;
  onDecision: (decision: "approve" | "reject") => void;
}) {
  const args = approval.request.args ?? {};
  const activatingSkill = approval.request.name === "activate_skill";
  return (
    <section className="inline-approval" aria-live="polite">
      <div className="inline-approval-icon">
        <TerminalSquare size={17} />
      </div>
      <div className="inline-approval-body">
        <div className="inline-approval-heading">
          <strong>{activatingSkill ? "启用此 Skill？" : "允许执行此命令？"}</strong>
          <span>
            {activatingSkill ? "授权在当前会话内有效" : "仅对此次精确命令有效"}
          </span>
        </div>
        <code>
          {activatingSkill
            ? String(args.name ?? "unknown")
            : (args.argv ?? []).join(" ") || approval.request.name || "run_command"}
        </code>
        {activatingSkill ? (
          <div className="inline-approval-meta">
            {approval.request.skill_capabilities?.commands.map((command) => (
              <span key={`command-${command.name}`}>命令：{command.name}</span>
            ))}
            {approval.request.skill_capabilities?.mcp_servers.map((server) => (
              <span key={`mcp-${server.name}`}>
                MCP：{server.name} ({server.url})
              </span>
            ))}
          </div>
        ) : (
          <div className="inline-approval-meta">
            <span>目录：{args.cwd ?? "."}</span>
            <span>超时：{args.timeout_seconds ?? 120} 秒</span>
          </div>
        )}
      </div>
      <div className="inline-approval-actions">
        <button className="secondary-button" onClick={() => onDecision("reject")}>
          <X size={14} /> 拒绝
        </button>
        <button className="primary-button" onClick={() => onDecision("approve")}>
          <Check size={14} /> 允许
        </button>
      </div>
    </section>
  );
}

function RestoreDialog({
  turn,
  disabled,
  onCancel,
  onConfirm,
}: {
  turn: Turn;
  disabled: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div className="modal-backdrop">
      <div className="modal" role="dialog" aria-modal="true">
        <div className="modal-icon">
          <History size={20} />
        </div>
        <div>
          <span className="eyebrow">恢复历史</span>
          <h2>恢复到第 {turn.turn_number} 轮？</h2>
          <p>
            {disabled
              ? "工作区有任务正在运行。请等待任务结束后再恢复。"
              : "系统会创建新 timeline。旧 timeline 保持只读，ignored 文件和外部副作用不会恢复。"}
          </p>
        </div>
        <div className="restore-target">
          <strong>{turn.user_text}</strong>
          <code>{turn.snapshot_oid.slice(0, 12)}</code>
        </div>
        <div className="modal-actions">
          <button className="secondary-button" onClick={onCancel}>取消</button>
          <button className="danger-button" onClick={onConfirm} disabled={disabled}>
            <RotateCcw size={15} /> 确认恢复
          </button>
        </div>
      </div>
    </div>
  );
}

type WorkspacePickerProps = {
  recent: Workspaces["recent"];
  path: string;
  error: string;
  opening: boolean;
  onPath: (path: string) => void;
  onOpen: (path: string) => void;
  onRemove: (path: string) => void;
};

function WorkspaceChooser(props: WorkspacePickerProps) {
  return (
    <main className="workspace-welcome">
      <section className="workspace-launcher">
        <div className="workspace-launcher-icon">
          <FolderOpen size={25} />
        </div>
        <h1>打开工作目录</h1>
        <p>输入本机 Git 仓库目录开始工作。</p>
        <WorkspacePickerFields {...props} />
      </section>
    </main>
  );
}

function WorkspaceDialog({
  onCancel,
  ...props
}: WorkspacePickerProps & { onCancel: () => void }) {
  return (
    <div className="modal-backdrop">
      <div className="modal workspace-dialog" role="dialog" aria-modal="true">
        <div className="modal-icon">
          <FolderOpen size={20} />
        </div>
        <div>
          <span className="eyebrow">工作目录</span>
          <h2>打开或切换目录</h2>
          <p>切换前当前目录必须处于空闲状态。</p>
        </div>
        <WorkspacePickerFields {...props} />
        <div className="modal-actions">
          <button className="secondary-button" onClick={onCancel} disabled={props.opening}>
            取消
          </button>
        </div>
      </div>
    </div>
  );
}

function WorkspacePickerFields({
  recent,
  path,
  error,
  opening,
  onPath,
  onOpen,
  onRemove,
}: WorkspacePickerProps) {
  return (
    <div className="workspace-picker-fields">
      <form
        className="workspace-path-form"
        onSubmit={(event) => {
          event.preventDefault();
          onOpen(path);
        }}
      >
        <label>
          <span>目录路径</span>
          <input
            value={path}
            onChange={(event) => onPath(event.target.value)}
            placeholder="/Users/name/project"
            required
            autoFocus
          />
        </label>
        <button className="primary-button" type="submit" disabled={opening || !path.trim()}>
          {opening ? <LoaderCircle className="spin" size={15} /> : <FolderOpen size={15} />}
          {opening ? "正在打开" : "打开"}
        </button>
      </form>
      {error ? (
        <div className="workspace-error" role="alert">
          <AlertCircle size={15} />
          {error}
        </div>
      ) : null}
      <div className="recent-workspaces">
        <div className="recent-workspaces-heading">最近使用</div>
        {recent.length ? (
          recent.map((workspace) => (
            <div className={`recent-workspace-row ${workspace.active ? "active" : ""}`} key={workspace.path}>
              <button
                className="recent-workspace-open"
                onClick={() => onOpen(workspace.path)}
                disabled={opening || workspace.active || !workspace.available}
                title={workspace.path}
              >
                <Folder size={16} />
                <span>
                  <strong>{workspace.name}</strong>
                  <small>{workspace.path}</small>
                </span>
                {workspace.active ? <CircleDot size={13} /> : null}
              </button>
              {!workspace.active ? (
                <button
                  className="icon-button compact"
                  onClick={() => onRemove(workspace.path)}
                  disabled={opening}
                  title="从最近目录移除"
                >
                  <Trash2 size={14} />
                </button>
              ) : null}
            </div>
          ))
        ) : (
          <div className="recent-workspaces-empty">暂无最近目录</div>
        )}
      </div>
    </div>
  );
}

function SettingsDialog({
  value,
  loading,
  saving,
  error,
  onCancel,
  onApply,
}: {
  value?: SettingsConfig;
  loading: boolean;
  saving: boolean;
  error: string;
  onCancel: () => void;
  onApply: (value: SettingsConfig) => void;
}) {
  const [form, setForm] = useState<SettingsConfig | null>(value ?? null);

  useEffect(() => {
    if (value) setForm(value);
  }, [value]);

  return (
    <div className="modal-backdrop">
      <div className="modal settings-modal" role="dialog" aria-modal="true">
        <div className="modal-icon">
          <Settings size={20} />
        </div>
        <div>
          <span className="eyebrow">运行配置</span>
          <h2>Settings</h2>
          <p>API Key 继续从启动 Host 的环境变量读取。</p>
        </div>
        {loading || !form ? (
          <div className="settings-loading">
            <LoaderCircle className="spin" size={18} />
            正在读取配置
          </div>
        ) : (
          <form
            className="settings-form"
            onSubmit={(event) => {
              event.preventDefault();
              onApply(form);
            }}
          >
            <label className="setting-field">
              <span>模型名称</span>
              <input
                value={form.model}
                onChange={(event) => setForm({ ...form, model: event.target.value })}
                required
                maxLength={200}
                autoFocus
              />
            </label>
            <label className="setting-field">
              <span>Base URL</span>
              <input
                value={form.base_url ?? ""}
                onChange={(event) =>
                  setForm({ ...form, base_url: event.target.value || null })
                }
                placeholder="https://api.example.com/v1"
                type="url"
                maxLength={2048}
              />
            </label>
            <label className="setting-field">
              <span>模型调用超时</span>
              <div className="number-field">
                <input
                  value={form.model_timeout_seconds}
                  onChange={(event) =>
                    setForm({
                      ...form,
                      model_timeout_seconds: Number(event.target.value),
                    })
                  }
                  type="number"
                  min={1}
                  max={1800}
                  required
                />
                <span>秒</span>
              </div>
            </label>
            <label className="setting-field">
              <span>命令超时</span>
              <div className="number-field">
                <input
                  value={form.command_timeout_seconds}
                  onChange={(event) =>
                    setForm({
                      ...form,
                      command_timeout_seconds: Number(event.target.value),
                    })
                  }
                  type="number"
                  min={1}
                  max={1800}
                  required
                />
                <span>秒</span>
              </div>
            </label>
            <label className="setting-field">
              <span>并行 Session</span>
              <div className="number-field">
                <input
                  value={form.max_parallel_sessions}
                  onChange={(event) =>
                    setForm({
                      ...form,
                      max_parallel_sessions: Number(event.target.value),
                    })
                  }
                  type="number"
                  min={1}
                  max={16}
                  required
                />
                <span>个</span>
              </div>
            </label>
            <label className="settings-switch-row">
              <span>
                <strong>LangSmith</strong>
                <small>
                  {form.langsmith_api_key_configured
                    ? "环境变量已配置"
                    : "未检测到 LangSmith API Key"}
                </small>
              </span>
              <input
                checked={form.langsmith_enabled}
                onChange={(event) =>
                  setForm({ ...form, langsmith_enabled: event.target.checked })
                }
                type="checkbox"
              />
            </label>
            <label className="setting-field">
              <span>LangSmith 项目</span>
              <input
                value={form.langsmith_project}
                onChange={(event) =>
                  setForm({ ...form, langsmith_project: event.target.value })
                }
                disabled={!form.langsmith_enabled}
                required
                maxLength={200}
              />
            </label>
            <div className="credential-status">
              <span className={form.model_api_key_configured ? "configured" : "missing"}>
                <CircleDot size={12} />
                模型 API Key
              </span>
              <strong>{form.model_api_key_configured ? "已通过环境变量配置" : "未配置"}</strong>
            </div>
            <div className="credential-status">
              <span
                className={
                  form.sandbox_enabled && form.sandbox_health?.available
                    ? "configured"
                    : "missing"
                }
              >
                <CircleDot size={12} />
                命令沙箱
              </span>
              <strong>
                {!form.sandbox_enabled
                  ? "已禁用，命令执行将被拒绝"
                  : form.sandbox_health?.available
                    ? `${form.sandbox_provider} · ${form.sandbox_health.isolation_level}`
                    : form.sandbox_health?.error_code ?? "打开工作区后检查"}
              </strong>
            </div>
            {form.sandbox_provider === "docker" ? (
              <div className="credential-status">
                <span>执行镜像</span>
                <strong title={form.sandbox_health?.image_digest ?? form.sandbox_image ?? ""}>
                  {form.sandbox_image ?? "未配置"}
                </strong>
              </div>
            ) : null}
            {error ? (
              <div className="settings-error" role="alert">
                <AlertCircle size={15} />
                {error}
              </div>
            ) : null}
            <div className="modal-actions">
              <button
                className="secondary-button"
                type="button"
                onClick={onCancel}
                disabled={saving}
              >
                取消
              </button>
              <button className="primary-button" type="submit" disabled={saving}>
                {saving ? <LoaderCircle size={15} className="spin" /> : <Check size={15} />}
                {saving ? "应用中" : "应用"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

function StatusBar({ host, workspace }: { host: HostStatus; workspace?: WorkspaceStatus }) {
  return (
    <footer className="statusbar">
      <span>
        {host.workspace ? <GitBranch size={12} /> : <Folder size={12} />}
        {host.workspace ? (workspace?.branch ?? host.branch) : "未打开目录"}
      </span>
      {host.workspace ? <span><CircleDot size={11} /> 本地工作区</span> : null}
      <span className="status-spacer" />
      <span>{host.model}</span>
      {host.workspace ? (
        <span>
          <ArrowUp size={11} /> {workspace?.added_lines ?? 0}
          <ArrowDown size={11} /> {workspace?.deleted_lines ?? 0}
        </span>
      ) : null}
    </footer>
  );
}

function InlineQueryError({
  message,
  onRetry,
}: {
  message: string;
  onRetry: () => void;
}) {
  return (
    <div className="inline-query-error" role="alert">
      <AlertCircle size={16} />
      <span>{message}</span>
      <button className="secondary-button" onClick={onRetry}>
        重试
      </button>
    </div>
  );
}

function Unavailable({
  icon: Icon,
  title,
  detail,
}: {
  icon: typeof File;
  title: string;
  detail: string;
}) {
  return (
    <div className="unavailable">
      <Icon size={22} />
      <strong>{title}</strong>
      <span>{detail}</span>
    </div>
  );
}

function MarkdownMessage({ children }: { children: string }) {
  return (
    <div className="assistant-copy">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ children: label, ...props }) => (
            <a {...props} target="_blank" rel="noreferrer">
              {label}
            </a>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}

function LoadingScreen({ label }: { label: string }) {
  return <div className="full-screen-state"><LoaderCircle className="spin" size={24} /><span>{label}</span></div>;
}

function FatalScreen({ message }: { message: string }) {
  return <div className="full-screen-state error"><AlertCircle size={24} /><strong>无法连接 Coding Agent</strong><span>{message}</span></div>;
}

function relativeTime(value: string) {
  const minutes = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 60000));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)} 小时前`;
  return `${Math.floor(minutes / 1440)} 天前`;
}

function formatTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 / 1024).toFixed(1)} MiB`;
}

function formatDuration(value: number) {
  return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(1)} s`;
}

function effectLabel(effect: string) {
  if (effect === "read_only") return "只读";
  if (effect === "workspace_write") return "工作区";
  if (effect === "external") return "外部服务";
  return effect;
}

function operationStatusLabel(status: string) {
  const labels: Record<string, string> = {
    queued: "排队中",
    running: "执行中",
    waiting_approval: "等待审批",
    cancel_requested: "取消中",
    cancelling: "取消中",
    committing: "提交中",
  };
  return labels[status] ?? status;
}

function duration(start: string, end: string | null) {
  if (!end) return "进行中";
  const milliseconds = new Date(end).getTime() - new Date(start).getTime();
  return milliseconds < 1000 ? `${milliseconds} ms` : `${(milliseconds / 1000).toFixed(1)} s`;
}

function prettyAttributes(value: string) {
  try {
    return JSON.stringify(JSON.parse(value), null, 2);
  } catch {
    return value;
  }
}

function eventLabel(event: StreamEvent) {
  if (event.type === "turn.started") return "任务已开始";
  if (event.type === "approval.required") return "等待用户审批";
  if (event.type === "step.started") return `开始 ${String(event.data.kind ?? "步骤")}`;
  if (event.type === "step.completed") return `完成 ${String(event.data.kind ?? "步骤")}`;
  if (event.type === "context.window_usage") {
    const ratio = Number(event.data.usage_ratio ?? 0);
    return `上下文占用 ${Math.round(ratio * 100)}%`;
  }
  return event.type;
}
