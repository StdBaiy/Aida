from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.tools import tool
from pydantic import ValidationError

from coding_agent.config import AgentConfig, TrustedSkillCommandConfig
from coding_agent.errors import CodingAgentError
from coding_agent.execution.host import HostExecutionBackend
from coding_agent.runtime import AgentRuntime
from coding_agent.skill_execution import SkillExecutionManager, activate_skill_thread
from coding_agent.skills import (
    Skill,
    SkillManifest,
    SkillRegistry,
    bind_trusted_skill_commands,
)
from coding_agent.workspace.paths import PathGuard


def _manager(
    tmp_path: Path,
    manifest: dict[str, Any],
) -> tuple[SkillExecutionManager, Skill]:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("---\nname: alpha\ndescription: Test.\n---\n", encoding="utf-8")
    skill = Skill(
        "alpha",
        "Test.",
        "workspace",
        skill_path.resolve(),
        SkillManifest.model_validate(manifest),
    )
    registry = SkillRegistry([skill], max_read_bytes=4096)
    manager = SkillExecutionManager(
        registry,
        workspace_root=tmp_path,
        config=AgentConfig(model="test", api_key="key"),
        execution_service=cast(Any, HostExecutionBackend(PathGuard(tmp_path))),
    )
    return manager, skill


def test_skill_command_requires_thread_authorization(tmp_path: Path) -> None:
    manager, _ = _manager(
        tmp_path,
        {
            "commands": {
                "echo": {
                    "description": "Echo one argument.",
                    "argv": ["printf", "%s"],
                }
            }
        },
    )

    with activate_skill_thread("thread-1"):
        denied = manager.tools[1].invoke(
            {"skill_name": "alpha", "command_name": "echo", "extra_args": ["hello"]}
        )
        manager.authorize("thread-1", "alpha")
        activated = manager.activate("alpha")
        result = manager.run_command("alpha", "echo", ["hello"])

    assert denied["error_code"] == "SKILL_APPROVAL_REQUIRED"
    assert activated["commands"] == {"echo": "Echo one argument."}
    assert result["exit_code"] == 0
    assert result["stdout"] == "hello"
    manager.close()


def test_skill_authorization_is_scoped_to_thread(tmp_path: Path) -> None:
    manager, _ = _manager(
        tmp_path,
        {"commands": {"echo": {"description": "Echo.", "argv": ["printf", "%s"]}}},
    )
    manager.authorize("thread-1", "alpha")

    with activate_skill_thread("thread-2"), pytest.raises(CodingAgentError) as exc_info:
        manager.activate("alpha")

    assert exc_info.value.code == "SKILL_APPROVAL_REQUIRED"
    manager.close()


def test_skill_authorization_is_scoped_to_namespaced_id(tmp_path: Path) -> None:
    workspace_path = tmp_path / "workspace" / "SKILL.md"
    workspace_path.parent.mkdir()
    workspace_path.write_text("---\nname: alpha\ndescription: Workspace.\n---\n")
    user_path = tmp_path / "user" / "SKILL.md"
    user_path.parent.mkdir()
    user_path.write_text("---\nname: alpha\ndescription: User.\n---\n")
    manifest = SkillManifest.model_validate(
        {"commands": {"echo": {"description": "Echo.", "argv": ["printf", "%s"]}}}
    )
    registry = SkillRegistry(
        [
            Skill("alpha", "Workspace.", "workspace", workspace_path.resolve(), manifest),
            Skill("alpha", "User.", "user", user_path.resolve(), manifest),
        ],
        max_read_bytes=4096,
    )
    manager = SkillExecutionManager(
        registry,
        workspace_root=tmp_path,
        config=AgentConfig(model="test", api_key="key"),
        execution_service=cast(Any, HostExecutionBackend(PathGuard(tmp_path))),
    )
    manager.authorize("thread", "alpha")

    with activate_skill_thread("thread"), pytest.raises(CodingAgentError) as exc_info:
        manager.activate("user:alpha")

    assert exc_info.value.code == "SKILL_APPROVAL_REQUIRED"
    manager.close()


def test_skill_script_must_resolve_inside_skill_directory(tmp_path: Path) -> None:
    manager, skill = _manager(
        tmp_path,
        {
            "commands": {
                "script": {
                    "description": "Run script.",
                    "script": "scripts/task.py",
                    "interpreter": "python3",
                }
            }
        },
    )
    script = skill.path.parent / "scripts" / "task.py"
    script.parent.mkdir()
    script.write_text("import sys\nprint(sys.argv[1])\n", encoding="utf-8")
    manager.authorize("thread", "alpha")

    with activate_skill_thread("thread"):
        result = manager.run_command("alpha", "script", ["done"])

    assert result["exit_code"] == 0
    assert result["stdout"] == "done\n"
    manager.close()


def test_trusted_skill_command_uses_host_home_and_reports_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("---\nname: alpha\ndescription: Test.\n---\n")
    cli = tmp_path / "trusted-cli"
    cli.write_text(
        '#!/bin/sh\nprintf \'%s|%s\' "$HOME" "$1"\n',
        encoding="utf-8",
    )
    cli.chmod(0o700)
    home = tmp_path / "real-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    [skill] = bind_trusted_skill_commands(
        [Skill("alpha", "Test.", "user", skill_path.resolve())],
        {
            "user:alpha": {
                "cli": TrustedSkillCommandConfig(
                    description="Run trusted CLI.",
                    argv=[str(cli)],
                )
            }
        },
    )

    class SandboxMustNotRun:
        def run(self, *_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("trusted command entered the sandbox")

    manager = SkillExecutionManager(
        SkillRegistry([skill], max_read_bytes=4096),
        workspace_root=tmp_path,
        config=AgentConfig(model="test", api_key="key"),
        execution_service=cast(Any, SandboxMustNotRun()),
    )
    manager.authorize("thread", "user:alpha")

    with activate_skill_thread("thread"):
        result = manager.run_command("user:alpha", "cli", ["hello"])

    assert result["stdout"] == f"{home}|hello"
    assert result["provider"] == "trusted_host"
    assert result["isolation_level"] == "none"
    assert result["skill_id"] == "user:alpha"
    assert result["approval_scope"] == "thread"
    manager.close()


def test_trusted_skill_command_fails_if_executable_changes(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text("---\nname: alpha\ndescription: Test.\n---\n")
    cli = tmp_path / "trusted-cli"
    cli.write_text("#!/bin/sh\nprintf original\n", encoding="utf-8")
    cli.chmod(0o700)
    [skill] = bind_trusted_skill_commands(
        [Skill("alpha", "Test.", "user", skill_path.resolve())],
        {
            "user:alpha": {
                "cli": TrustedSkillCommandConfig(
                    description="Run trusted CLI.",
                    argv=[str(cli)],
                )
            }
        },
    )
    manager = SkillExecutionManager(
        SkillRegistry([skill], max_read_bytes=4096),
        workspace_root=tmp_path,
        config=AgentConfig(model="test", api_key="key"),
        execution_service=cast(Any, HostExecutionBackend(PathGuard(tmp_path))),
    )
    manager.authorize("thread", "user:alpha")
    cli.write_text("#!/bin/sh\nprintf changed\n", encoding="utf-8")

    with activate_skill_thread("thread"), pytest.raises(CodingAgentError) as exc_info:
        manager.run_command("user:alpha", "cli", [])

    assert exc_info.value.code == "TRUSTED_SKILL_EXECUTABLE_CHANGED"
    manager.close()


def test_skill_manifest_rejects_script_path_traversal() -> None:
    with pytest.raises(ValidationError):
        SkillManifest.model_validate(
            {
                "commands": {
                    "bad": {
                        "description": "Bad.",
                        "script": "../outside.py",
                        "interpreter": "python3",
                    }
                }
            }
        )


def test_skill_command_resolves_environment_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, _ = _manager(
        tmp_path,
        {
            "commands": {
                "env": {
                    "description": "Read mapped environment.",
                    "script": "env.py",
                    "interpreter": "python3",
                    "env_from_env": {"SKILL_TOKEN": "SOURCE_TOKEN"},
                }
            }
        },
    )
    skill = manager.registry.get("alpha")
    (skill.path.parent / "env.py").write_text(
        "import os\nprint(os.environ['SKILL_TOKEN'])\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SOURCE_TOKEN", "mapped-value")
    manager.authorize("thread", "alpha")

    with activate_skill_thread("thread"):
        result = manager.run_command("alpha", "env", [])

    assert result["stdout"] == "mapped-value\n"
    manager.close()


def test_skill_mcp_connects_only_after_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @tool
    def ping(value: str) -> str:
        """Return a test response."""
        return f"pong:{value}"

    class FakeProvider:
        created = 0
        closed = 0

        def __init__(self, servers: object) -> None:
            assert servers
            self.tools = [ping]
            FakeProvider.created += 1

        def close(self) -> None:
            FakeProvider.closed += 1

    monkeypatch.setattr("coding_agent.skill_execution.MCPToolProvider", FakeProvider)
    manager, _ = _manager(
        tmp_path,
        {"mcp_servers": {"docs": {"url": "https://example.com/mcp"}}},
    )

    assert FakeProvider.created == 0
    manager.authorize("thread", "alpha")
    with activate_skill_thread("thread"):
        activated = manager.activate("alpha")
        activated_again = manager.activate("alpha")
        result = manager.call_mcp("alpha", "ping", {"value": "ok"})

    activated_tools = cast("list[dict[str, object]]", activated["mcp_tools"])
    activated_again_tools = cast("list[dict[str, object]]", activated_again["mcp_tools"])
    assert FakeProvider.created == 1
    assert activated_tools[0]["name"] == "ping"
    assert activated_tools[0]["input_schema"]["properties"]["value"]["type"] == "string"  # type: ignore[index]
    assert activated_again_tools[0]["name"] == "ping"
    assert result["result"] == "pong:ok"
    manager.close()
    assert FakeProvider.closed == 1


def test_runtime_approval_is_reused_for_same_thread(tmp_path: Path) -> None:
    manager, _ = _manager(
        tmp_path,
        {"commands": {"echo": {"description": "Echo.", "argv": ["printf", "%s"]}}},
    )
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.skill_execution = manager
    requests: list[dict[str, Any]] = []

    def approve(request: dict[str, Any]) -> bool:
        requests.append(request)
        return True

    activation = {"name": "activate_skill", "args": {"name": "alpha"}}

    assert runtime._approve_request("thread", activation, approve)
    assert runtime._approve_request("thread", activation, approve)
    assert len(requests) == 1
    assert requests[0]["skill_capabilities"] == {
        "skill": "alpha",
        "skill_id": "workspace:alpha",
        "source": "workspace",
        "commands": [
            {
                "name": "echo",
                "description": "Echo.",
                "execution": "sandbox",
                "argv": ["printf", "%s"],
                "allow_args": True,
                "environment": [],
            }
        ],
        "mcp_servers": [],
    }
    manager.close()
