"""Progressive discovery and loading for workspace and user Skills."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from coding_agent.config import MCPServerConfig
from coding_agent.errors import CodingAgentError, fail

SKILLS_DIRECTORY = Path(".agents", "skills")
SKILL_FILE_NAME = "SKILL.md"
SKILL_MANIFEST_NAME = "skill.json"
MAX_FRONTMATTER_BYTES = 65_536
MAX_MANIFEST_BYTES = 65_536
MAX_CATALOG_DESCRIPTION_CHARS = 200
_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_ENV_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class SkillCommandConfig(BaseModel):
    """One manifest-declared command with a fixed executable prefix."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=500)
    argv: list[str] | None = Field(default=None, min_length=1, max_length=64)
    script: str | None = None
    interpreter: Literal["python", "python3", "node", "ruby", "perl"] | None = None
    allow_args: bool = True
    timeout_seconds: int = Field(default=120, ge=1, le=1800)
    env_from_env: dict[str, str] = Field(default_factory=dict)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return values
        if any(not value or "\x00" in value or "\n" in value or "\r" in value for value in values):
            raise ValueError("command argv entries must be non-empty and contain no control chars")
        return values

    @field_validator("script")
    @classmethod
    def validate_script_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts:
            raise ValueError("script must be a relative path inside the Skill directory")
        return value

    @field_validator("env_from_env")
    @classmethod
    def validate_environment_names(cls, values: dict[str, str]) -> dict[str, str]:
        for target_name, source_name in values.items():
            if not _ENV_RE.fullmatch(target_name) or not _ENV_RE.fullmatch(source_name):
                raise ValueError("command environment mappings must contain valid variable names")
        return values

    @model_validator(mode="after")
    def validate_execution_mode(self) -> SkillCommandConfig:
        if (self.argv is None) == (self.script is None):
            raise ValueError("exactly one of argv or script must be configured")
        if self.script is None and self.interpreter is not None:
            raise ValueError("interpreter is only valid with script")
        if self.script is not None and self.interpreter is None:
            raise ValueError("script requires an interpreter")
        return self


class SkillManifest(BaseModel):
    """Optional executable capabilities declared beside a SKILL.md."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    commands: dict[str, SkillCommandConfig] = Field(default_factory=dict)
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)

    @field_validator("commands", "mcp_servers")
    @classmethod
    def validate_capability_names(cls, values: dict[str, object]) -> dict[str, object]:
        invalid = [name for name in values if not _NAME_RE.fullmatch(name)]
        if invalid:
            raise ValueError(
                "Capability names must contain only letters, digits, underscores, or hyphens: "
                + ", ".join(sorted(invalid))
            )
        return values


@dataclass(frozen=True)
class SkillRoot:
    """One ordered directory from which Skills may be discovered."""

    path: Path
    source: str
    allow_external_symlinks: bool = False


@dataclass(frozen=True)
class Skill:
    """One validated Skill addressable by its logical name."""

    name: str
    description: str
    source: str
    path: Path = field(repr=False)
    manifest: SkillManifest = field(default_factory=SkillManifest, repr=False)

    @property
    def has_executable_capabilities(self) -> bool:
        return bool(self.manifest.commands or self.manifest.mcp_servers)


def default_skill_roots(workspace_root: Path, home: Path | None = None) -> list[SkillRoot]:
    """Return Skill roots from highest to lowest precedence."""
    workspace = workspace_root.resolve(strict=True)
    user_home = (home or Path.home()).expanduser().resolve()
    return [
        SkillRoot(workspace / SKILLS_DIRECTORY, "workspace"),
        SkillRoot(user_home / ".agents" / "skills", "user", allow_external_symlinks=True),
        SkillRoot(user_home / ".codex" / "skills", "codex", allow_external_symlinks=True),
    ]


def discover_skills(roots: Iterable[SkillRoot]) -> list[Skill]:
    """Discover valid Skills, keeping the first occurrence of each name."""
    skills: dict[str, Skill] = {}
    for configured_root in roots:
        root = configured_root.path.expanduser()
        if not root.is_dir():
            continue
        resolved_root = root.resolve(strict=True)
        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            if entry.name in skills:
                continue
            skill_file = entry / SKILL_FILE_NAME
            if not entry.is_dir() or not skill_file.is_file():
                continue
            resolved = skill_file.resolve()
            if (
                not configured_root.allow_external_symlinks
                and not resolved.is_relative_to(resolved_root)
            ):
                continue
            location = f"{configured_root.source}:{entry.name}/{SKILL_FILE_NAME}"
            metadata = _parse_frontmatter(_read_frontmatter(resolved), entry.name, location)
            manifest = _read_manifest(skill_file.parent / SKILL_MANIFEST_NAME, location)
            skills[entry.name] = Skill(
                name=metadata["name"],
                description=metadata["description"],
                source=configured_root.source,
                path=resolved,
                manifest=manifest,
            )
    return list(skills.values())


def format_skill_catalog(skills: Iterable[Skill]) -> str:
    """Render the compact catalog injected into the system prompt."""
    entries = list(skills)
    if not entries:
        return ""
    lines = ["Available skills:"]
    for skill in entries:
        description = re.sub(r"\s+", " ", skill.description).strip()
        if len(description) > MAX_CATALOG_DESCRIPTION_CHARS:
            description = description[: MAX_CATALOG_DESCRIPTION_CHARS - 3] + "..."
        capability = " executable" if skill.has_executable_capabilities else ""
        lines.append(f"- {skill.name} [{skill.source}{capability}]: {description}")
        lines.append(f'  load with: load_skill("{skill.name}")')
    return "\n".join(lines)


class SkillRegistry:
    """Map validated logical Skill names to bounded instruction-file reads."""

    def __init__(self, skills: Iterable[Skill], max_read_bytes: int) -> None:
        self.skills = list(skills)
        self._by_name = {skill.name: skill for skill in self.skills}
        self._max_read_bytes = max_read_bytes

        @tool
        def load_skill(name: str) -> dict[str, object]:
            """Load the complete SKILL.md for one exact name from the available Skill catalog."""
            try:
                return self.load(name)
            except CodingAgentError as exc:
                return {"ok": False, "error_code": exc.code, "message": exc.user_message}
            except Exception as exc:
                return {"ok": False, "error_code": "TOOL_ERROR", "message": str(exc)}

        self.tool: BaseTool = load_skill

    def load(self, name: str) -> dict[str, object]:
        """Load one pre-discovered Skill without accepting a filesystem path."""
        skill = self.get(name)
        try:
            current_path = skill.path.resolve(strict=True)
            if current_path != skill.path:
                raise fail("SKILL_CHANGED", f"Skill path changed; restart required: {name}")
            with current_path.open("rb") as file:
                content = file.read(self._max_read_bytes + 1)
        except CodingAgentError:
            raise
        except OSError as exc:
            raise fail("SKILL_READ_ERROR", f"Cannot read Skill {name}: {exc}") from exc
        if len(content) > self._max_read_bytes:
            raise fail("SKILL_TOO_LARGE", f"Skill exceeds the read limit: {name}")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise fail("INVALID_SKILL", f"Skill must be UTF-8 text: {name}") from exc
        return {
            "ok": True,
            "name": skill.name,
            "source": skill.source,
            "content": text,
            "requires_activation": skill.has_executable_capabilities,
            "commands": {
                command_name: command.description
                for command_name, command in skill.manifest.commands.items()
            },
            "mcp_servers": sorted(skill.manifest.mcp_servers),
        }

    def get(self, name: str) -> Skill:
        """Return one pre-discovered Skill or a stable user-facing error."""
        skill = self._by_name.get(name)
        if skill is None:
            raise fail("SKILL_NOT_FOUND", f"Skill is not available: {name}")
        return skill


def _read_frontmatter(path: Path) -> str:
    try:
        with path.open("rb") as file:
            raw = file.read(MAX_FRONTMATTER_BYTES)
    except OSError as exc:
        raise fail("INVALID_SKILL", f"Cannot read Skill file {path}: {exc}") from exc
    lines = raw.splitlines()
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == b"---"), None)
    frontmatter = b"\n".join(lines if end is None else lines[: end + 1])
    try:
        return frontmatter.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise fail("INVALID_SKILL", f"Skill file must be UTF-8 text: {path}") from exc


def _read_manifest(path: Path, location: str) -> SkillManifest:
    if not path.exists():
        return SkillManifest()
    try:
        with path.open("rb") as file:
            raw = file.read(MAX_MANIFEST_BYTES + 1)
    except OSError as exc:
        raise fail("INVALID_SKILL_MANIFEST", f"Cannot read manifest for {location}: {exc}") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise fail("INVALID_SKILL_MANIFEST", f"Manifest is too large for {location}.")
    try:
        return SkillManifest.model_validate_json(raw)
    except ValueError as exc:
        raise fail("INVALID_SKILL_MANIFEST", f"Invalid manifest for {location}: {exc}") from exc


def _parse_frontmatter(text: str, directory_name: str, location: str) -> dict[str, str]:
    """Parse the required scalar fields from a small YAML-style frontmatter block."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise fail("INVALID_SKILL", f"Skill {location} must begin with a frontmatter block.")
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        raise fail("INVALID_SKILL", f"Skill {location} frontmatter is not closed by ---.")

    metadata: dict[str, str] = {}
    index = 1
    while index < end:
        line = lines[index]
        if not line.strip() or line[0] in {" ", "\t"}:
            index += 1
            continue
        key, separator, raw_value = line.partition(":")
        if not separator or not _KEY_RE.fullmatch(key):
            index += 1
            continue
        value = raw_value.strip()
        if key not in {"name", "description"}:
            index += 1
            continue
        if value.startswith((">", "|")):
            block: list[str] = []
            index += 1
            while index < end and (not lines[index].strip() or lines[index][0] in {" ", "\t"}):
                if lines[index].strip():
                    block.append(lines[index].strip())
                index += 1
            if not block:
                raise fail("INVALID_SKILL", f"Skill {location} has an empty {key}.")
            metadata[key] = "\n".join(block) if value.startswith("|") else " ".join(block)
            continue
        metadata[key] = value.strip('"').strip("'")
        index += 1

    name = metadata.get("name", "").strip()
    if not _NAME_RE.fullmatch(name) or name != directory_name:
        raise fail(
            "INVALID_SKILL",
            f"Skill {location} must declare name: {directory_name} in frontmatter.",
        )
    description = re.sub(r"\s+", " ", metadata.get("description", "")).strip()
    if not description:
        raise fail("INVALID_SKILL", f"Skill {location} must declare a description.")
    return {"name": name, "description": description}
