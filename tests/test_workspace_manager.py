import asyncio
import subprocess
from pathlib import Path
from typing import Any

from coding_agent.application.service import WorkspaceManager
from coding_agent.config import load_recent_workspaces
from coding_agent.workspace import resolve_workspace
from coding_agent.workspace.lock import RepositoryLock


def init_repository(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init"], check=True, capture_output=True)


def test_workspace_manager_starts_empty_and_switches_isolated_repositories(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    init_repository(first)
    init_repository(second)
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("CODING_AGENT_API_KEY", "test-key")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGCHAIN_API_KEY", raising=False)

    async def scenario() -> tuple[str, str]:
        manager = WorkspaceManager(
            workspace_path=None,
            model=None,
            base_url=None,
            config_path=config_path,
            session_id=None,
            langsmith_enabled=False,
        )
        await manager.start()
        assert manager.status()["workspace"] is None

        first_status = await manager.open_workspace(str(first))
        first_session = str(first_status["session_id"])
        second_status = await manager.open_workspace(str(second))
        second_session = str(second_status["session_id"])
        assert manager.status()["workspace"] == str(second)
        assert [item["path"] for item in manager.workspaces()["recent"]] == [
            str(second),
            str(first),
        ]
        await manager.close()
        return first_session, second_session

    first_session, second_session = asyncio.run(scenario())

    assert first_session != second_session
    assert (first / ".git" / "coding-agent" / "agent.db").exists()
    assert (second / ".git" / "coding-agent" / "agent.db").exists()
    assert load_recent_workspaces(config_path) == [str(second), str(first)]

    first_workspace = resolve_workspace(first)
    with RepositoryLock(first_workspace.data_dir / "locks" / "repository.lock"):
        pass
