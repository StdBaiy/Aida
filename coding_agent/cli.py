"""Interactive command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from coding_agent.config import load_config
from coding_agent.coordinator import TurnCoordinator
from coding_agent.errors import CodingAgentError
from coding_agent.repository import SqliteCheckpointRepository
from coding_agent.runtime import AgentRuntime, approve_by_default
from coding_agent.tracing import MetricsLangSmithExporter, TraceStore
from coding_agent.workspace import GitSnapshotStore, resolve_workspace
from coding_agent.workspace.lock import RepositoryLock


class _StreamingOutput:
    """Render assistant deltas without printing the final message twice."""

    def __init__(self) -> None:
        self.started = False

    def write(self, token: str) -> None:
        """Write one model delta immediately."""
        if not self.started:
            print("\nagent> ", end="", flush=True)
            self.started = True
        print(token, end="", flush=True)

    def finish(self, response: str) -> None:
        """Terminate a streamed line or print a non-streamed fallback."""
        if self.started:
            print(flush=True)
        else:
            print(f"\nagent> {response}")

    def terminate(self) -> None:
        """Terminate a partially streamed line before printing an error."""
        if self.started:
            print(flush=True)


class _InputReader:
    """Read terminal input with correct Unicode width and IME handling."""

    def __init__(self) -> None:
        self.session: PromptSession[str] = PromptSession()

    def read(self, prompt: str) -> str:
        """Read one editable line while protecting it from background output."""
        with patch_stdout(raw=True):
            return self.session.prompt(prompt)


def build_parser() -> argparse.ArgumentParser:
    """Build CLI arguments."""
    parser = argparse.ArgumentParser(prog="coding-agent")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--session")
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--langsmith",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="enable or disable metrics-only LangSmith export",
    )
    return parser


def build_web_parser() -> argparse.ArgumentParser:
    """Build local Web host arguments."""
    parser = argparse.ArgumentParser(prog="coding-agent web")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--session")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--langsmith",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="enable or disable metrics-only LangSmith export",
    )
    return parser


def _print_help() -> None:
    print(
        "/help                 Show commands\n"
        "/status               Show active session and timeline\n"
        "/history              Show committed turns\n"
        "/trace <turn>         Show a local trace summary\n"
        "/restore <turn>       Fork and jointly restore a historical turn\n"
        "/quit                 Exit\n"
    )


def _approve_request(reader: _InputReader, request: dict[str, Any]) -> bool:
    """Prompt only when a Skill requests executable capabilities."""
    if request.get("name") != "activate_skill":
        return approve_by_default(request)
    arguments = request.get("args")
    skill_name = arguments.get("name") if isinstance(arguments, dict) else None
    capabilities = request.get("skill_capabilities")
    if isinstance(capabilities, dict):
        print(json.dumps(capabilities, indent=2, ensure_ascii=False))
    answer = reader.read(
        f"Enable executable capabilities for Skill {skill_name or '<unknown>'} "
        "in this session? [y/N] "
    )
    return answer.strip().lower() in {"y", "yes"}


def _interactive(
    coordinator: TurnCoordinator,
    repository: SqliteCheckpointRepository,
    reader: _InputReader | None = None,
) -> None:
    reader = reader or _InputReader()
    print("Coding Agent MVP. Type /help for commands.")
    while True:
        try:
            print()
            text = reader.read("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not text:
            continue
        if text == "/quit":
            return
        if text == "/help":
            _print_help()
            continue
        if text == "/status":
            session = repository.get_session(coordinator.session_id)
            timeline = repository.active_timeline(coordinator.session_id)
            print(
                json.dumps(
                    {
                        "session_id": session.session_id,
                        "timeline_id": timeline.timeline_id,
                        "thread_id": timeline.thread_id,
                        "workspace": session.workspace_root,
                    },
                    indent=2,
                )
            )
            continue
        if text == "/history":
            print(json.dumps(coordinator.history(), indent=2, ensure_ascii=False))
            continue
        if text.startswith("/trace "):
            try:
                turn_number = int(text.split(maxsplit=1)[1])
                print(
                    json.dumps(
                        coordinator.trace(turn_number),
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            except (ValueError, CodingAgentError) as exc:
                _print_error(exc)
            continue
        if text.startswith("/restore "):
            try:
                turn_number = int(text.split(maxsplit=1)[1])
                answer = reader.read(
                    f"Restore turn {turn_number} and fork a new timeline? "
                    "Ignored files and external side effects are not restored. [y/N] "
                )
                if answer.strip().lower() in {"y", "yes"}:
                    coordinator.restore(turn_number)
                    print(f"Restored turn {turn_number} into a new active timeline.")
            except (ValueError, CodingAgentError) as exc:
                _print_error(exc)
            continue
        if text.startswith("/"):
            print("Unknown command. Type /help.")
            continue
        try:
            output = _StreamingOutput()
            response = coordinator.run_turn(
                text,
                lambda request: _approve_request(reader, request),
                output.write,
            )
            output.finish(response)
        except CodingAgentError as exc:
            output.terminate()
            _print_error(exc)
        except Exception as exc:
            output.terminate()
            print(f"[RUNTIME_ERROR] {exc}", file=sys.stderr)


def _print_error(exc: Exception) -> None:
    if isinstance(exc, CodingAgentError):
        print(f"[{exc.code}] {exc.user_message}", file=sys.stderr)
    else:
        print(f"[INVALID_INPUT] {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    """Run the coding agent CLI."""
    arguments = list(argv if argv is not None else sys.argv[1:])
    if arguments[:1] == ["web"]:
        return _run_web(build_web_parser().parse_args(arguments[1:]))
    args = build_parser().parse_args(arguments)
    try:
        workspace = resolve_workspace(args.workspace)
        lock_path = workspace.data_dir / "locks" / "repository.lock"
        with RepositoryLock(lock_path):
            repository = SqliteCheckpointRepository(workspace.data_dir / "agent.db")
            runtime: AgentRuntime | None = None
            trace_store: TraceStore | None = None
            trace_exporter: MetricsLangSmithExporter | None = None
            try:
                stored = (
                    repository.validate_workspace(args.session, workspace) if args.session else None
                )
                config = load_config(
                    model=args.model or (stored.model if stored else None),
                    base_url=args.base_url,
                    config_path=args.config,
                    langsmith_enabled=args.langsmith,
                )
                session = stored or repository.create_session(workspace, config.model)
                trace_store = TraceStore(
                    workspace.data_dir / "agent.db",
                    workspace.data_dir / "artifacts",
                )
                trace_exporter = MetricsLangSmithExporter(
                    trace_store,
                    enabled=config.langsmith_enabled,
                    project=config.langsmith_project,
                )
                trace_exporter.retry_pending()
                runtime = AgentRuntime(
                    config=config,
                    workspace_root=workspace.root,
                    repo_root=workspace.repo_root,
                    checkpoint_path=workspace.data_dir / "checkpoints.db",
                )
                coordinator = TurnCoordinator(
                    session_id=session.session_id,
                    repository=repository,
                    runtime=runtime,
                    snapshots=GitSnapshotStore(workspace),
                    trace_store=trace_store,
                    trace_exporter=trace_exporter,
                    model_id=config.model,
                    trace_secrets=(config.api_key or "",),
                )
                coordinator.ensure_baseline()
                print(f"session: {session.session_id}")
                _interactive(coordinator, repository)
            finally:
                if runtime is not None:
                    runtime.close()
                if trace_exporter is not None:
                    trace_exporter.close()
                if trace_store is not None:
                    trace_store.close()
                repository.close()
        return 0
    except CodingAgentError as exc:
        _print_error(exc)
        return 2


def _run_web(args: argparse.Namespace) -> int:
    """Run the local API and bundled React application."""
    import uvicorn

    from coding_agent.api import create_app
    from coding_agent.application.service import WorkspaceManager

    static_dir = Path(__file__).parent / "web_dist"
    source_dist = Path(__file__).parent.parent / "web" / "dist"
    if not static_dir.exists() and source_dist.exists():
        static_dir = source_dist
    if args.session and args.workspace is None:
        print("coding-agent web: --session requires --workspace", file=sys.stderr)
        return 2
    host = WorkspaceManager(
        workspace_path=args.workspace,
        model=args.model,
        base_url=args.base_url,
        config_path=args.config,
        session_id=args.session,
        langsmith_enabled=args.langsmith,
    )
    app = create_app(host, static_dir)
    print(f"Coding Agent Web: http://127.0.0.1:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
