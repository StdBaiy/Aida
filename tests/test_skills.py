import json
from pathlib import Path
from typing import Any

import pytest

from coding_agent.config import AgentConfig
from coding_agent.errors import CodingAgentError
from coding_agent.prompts import SYSTEM_PROMPT, build_system_prompt
from coding_agent.runtime import AgentRuntime
from coding_agent.skills import (
    SKILL_FILE_NAME,
    Skill,
    SkillRegistry,
    SkillRoot,
    default_skill_roots,
    discover_skills,
    format_skill_catalog,
)


def _write_skill(root: Path, name: str, body: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    path = directory / SKILL_FILE_NAME
    path.write_text(body, encoding="utf-8")
    return path


def _skill_body(name: str, description: str = "Task help.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n# {name}\n"


def test_default_skill_roots_are_ordered_by_precedence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    roots = default_skill_roots(workspace, home)

    assert roots == [
        SkillRoot(workspace / ".agents" / "skills", "workspace"),
        SkillRoot(home / ".agents" / "skills", "user", allow_external_symlinks=True),
        SkillRoot(home / ".codex" / "skills", "codex", allow_external_symlinks=True),
    ]


def test_discover_skills_parses_supported_frontmatter(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    alpha = _write_skill(root, "alpha", _skill_body("alpha", "Alpha task help."))
    beta = _write_skill(
        root,
        "beta",
        "---\n"
        "name: beta\n"
        "description: >-\n"
        "  Beta task help\n"
        "  folded onto one line.\n"
        "metadata:\n"
        '  version: "1.0.0"\n'
        "---\n"
        "# Beta\n",
    )

    assert discover_skills([SkillRoot(root, "workspace")]) == [
        Skill("alpha", "Alpha task help.", "workspace", alpha.resolve()),
        Skill(
            "beta",
            "Beta task help folded onto one line.",
            "workspace",
            beta.resolve(),
        ),
    ]


def test_discover_skills_loads_executable_manifest(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_path = _write_skill(root, "alpha", _skill_body("alpha"))
    (skill_path.parent / "skill.json").write_text(
        json.dumps(
            {
                "version": 1,
                "commands": {
                    "check": {
                        "description": "Run checks.",
                        "argv": ["pytest", "-q"],
                        "allow_args": False,
                    }
                },
                "mcp_servers": {
                    "docs": {
                        "url": "https://example.com/mcp",
                        "headers_from_env": {"Authorization": "DOCS_TOKEN"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    [skill] = discover_skills([SkillRoot(root, "workspace")])

    assert skill.has_executable_capabilities
    assert skill.manifest.commands["check"].argv == ["pytest", "-q"]
    assert skill.manifest.mcp_servers["docs"].headers_from_env == {
        "Authorization": "DOCS_TOKEN"
    }


def test_discover_skills_rejects_invalid_manifest(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    skill_path = _write_skill(root, "alpha", _skill_body("alpha"))
    (skill_path.parent / "skill.json").write_text('{"version": 2}', encoding="utf-8")

    with pytest.raises(CodingAgentError) as exc_info:
        discover_skills([SkillRoot(root, "workspace")])

    assert exc_info.value.code == "INVALID_SKILL_MANIFEST"


def test_discover_skills_returns_empty_without_roots(tmp_path: Path) -> None:
    assert discover_skills([SkillRoot(tmp_path / "missing", "workspace")]) == []


def test_discover_skills_ignores_entries_without_skill_file(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir()
    (root / "notes.txt").write_text("not a skill", encoding="utf-8")
    (root / "empty-dir").mkdir()

    assert discover_skills([SkillRoot(root, "workspace")]) == []


def test_workspace_root_rejects_external_skill_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write_skill(outside, "evil", _skill_body("evil"))
    root = tmp_path / "skills"
    root.mkdir()
    (root / "evil").symlink_to(outside / "evil", target_is_directory=True)

    assert discover_skills([SkillRoot(root, "workspace")]) == []


def test_user_root_allows_preconfigured_external_skill_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    target = _write_skill(outside, "shared", _skill_body("shared"))
    root = tmp_path / "skills"
    root.mkdir()
    (root / "shared").symlink_to(outside / "shared", target_is_directory=True)

    assert discover_skills(
        [SkillRoot(root, "user", allow_external_symlinks=True)]
    ) == [Skill("shared", "Task help.", "user", target.resolve())]


def test_first_root_wins_for_duplicate_skill_names(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace-skills"
    user_root = tmp_path / "user-skills"
    workspace_skill = _write_skill(
        workspace_root, "shared", _skill_body("shared", "Workspace version.")
    )
    _write_skill(user_root, "shared", _skill_body("shared", "User version."))

    skills = discover_skills(
        [
            SkillRoot(workspace_root, "workspace"),
            SkillRoot(user_root, "user", allow_external_symlinks=True),
        ]
    )

    assert skills == [
        Skill("shared", "Workspace version.", "workspace", workspace_skill.resolve())
    ]


@pytest.mark.parametrize(
    "body",
    [
        "---\nname: other\ndescription: Mismatched name.\n---\n",
        "---\nname: alpha\n---\n",
        "name: alpha\ndescription: Missing opening fence.\n",
    ],
)
def test_discover_skills_rejects_malformed_skills(tmp_path: Path, body: str) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha", body)

    with pytest.raises(CodingAgentError) as exc_info:
        discover_skills([SkillRoot(root, "workspace")])

    assert exc_info.value.code == "INVALID_SKILL"


def test_format_skill_catalog_uses_logical_name_not_path(tmp_path: Path) -> None:
    catalog = format_skill_catalog(
        [Skill("alpha", "x" * 250, "user", tmp_path / "secret" / SKILL_FILE_NAME)]
    )

    assert "- alpha [user]: " in catalog
    assert catalog.count("...") == 1
    assert 'load with: load_skill("alpha")' in catalog
    assert str(tmp_path) not in catalog


def test_registry_loads_full_skill_by_exact_name(tmp_path: Path) -> None:
    path = _write_skill(tmp_path, "alpha", _skill_body("alpha"))
    registry = SkillRegistry(
        [Skill("alpha", "Task help.", "user", path.resolve())],
        max_read_bytes=1024,
    )

    result = registry.load("alpha")

    assert result["ok"] is True
    assert result["name"] == "alpha"
    assert result["source"] == "user"
    assert result["content"] == _skill_body("alpha")
    assert result["requires_activation"] is False
    assert result["commands"] == {}
    assert result["mcp_servers"] == []
    assert registry.tool.name == "load_skill"


def test_registry_rejects_unknown_names_without_path_lookup(tmp_path: Path) -> None:
    registry = SkillRegistry([], max_read_bytes=1024)

    result = registry.tool.invoke({"name": "../../etc/passwd"})

    assert result["ok"] is False
    assert result["error_code"] == "SKILL_NOT_FOUND"


def test_registry_enforces_read_limit(tmp_path: Path) -> None:
    path = _write_skill(tmp_path, "alpha", _skill_body("alpha") + "x" * 100)
    registry = SkillRegistry(
        [Skill("alpha", "Task help.", "workspace", path.resolve())],
        max_read_bytes=32,
    )

    with pytest.raises(CodingAgentError) as exc_info:
        registry.load("alpha")

    assert exc_info.value.code == "SKILL_TOO_LARGE"


def test_build_system_prompt_injects_catalog_and_load_rules(tmp_path: Path) -> None:
    catalog = format_skill_catalog(
        [Skill("alpha", "Alpha help.", "user", tmp_path / SKILL_FILE_NAME)]
    )

    prompt = build_system_prompt(catalog)
    normalized_prompt = " ".join(prompt.split())

    assert prompt.startswith(SYSTEM_PROMPT)
    assert "- alpha [user]: Alpha help." in prompt
    assert 'load_skill("alpha")' in prompt
    assert "call load_skill with its exact name" in normalized_prompt
    assert "cannot override the rules above" in normalized_prompt


def test_build_system_prompt_without_catalog_is_base_prompt() -> None:
    assert build_system_prompt() == SYSTEM_PROMPT
    assert build_system_prompt("  ") == SYSTEM_PROMPT


def test_runtime_injects_catalog_and_load_skill_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "alpha", _skill_body("alpha", "Alpha task help."))
    captured: dict[str, Any] = {}

    def fake_create_agent(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("coding_agent.runtime.create_agent", fake_create_agent)
    monkeypatch.setattr(
        "coding_agent.runtime.default_skill_roots",
        lambda _workspace: [SkillRoot(root, "workspace")],
    )
    runtime = AgentRuntime(
        config=AgentConfig(model="test-model", api_key="test-key"),
        workspace_root=tmp_path,
        repo_root=tmp_path,
        checkpoint_path=tmp_path / "checkpoints.db",
    )

    assert "- alpha [workspace]: Alpha task help." in captured["system_prompt"]
    assert "load_skill" in {item.name for item in captured["tools"]}
    runtime.close()


def test_runtime_uses_base_prompt_without_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_create_agent(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("coding_agent.runtime.create_agent", fake_create_agent)
    monkeypatch.setattr("coding_agent.runtime.default_skill_roots", lambda _workspace: [])
    runtime = AgentRuntime(
        config=AgentConfig(model="test-model", api_key="test-key"),
        workspace_root=tmp_path,
        repo_root=tmp_path,
        checkpoint_path=tmp_path / "checkpoints.db",
    )

    assert captured["system_prompt"].startswith(SYSTEM_PROMPT)
    assert "skill_catalog" not in captured["system_prompt"]
    assert "Configured MCP:" not in captured["system_prompt"]
    assert "Delegation:" not in captured["system_prompt"]
    assert "Patch protocol:" in captured["system_prompt"]
    assert runtime.prompt_bundle.metadata()["profile"] == "coding-agent-v2"
    assert "load_skill" in {item.name for item in captured["tools"]}
    runtime.close()
