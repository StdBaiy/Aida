"""Configuration loading."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator

from coding_agent.errors import fail

_DEFAULT_CONFIG_PATH = Path("~/.config/langchain-coding-agent/config.json").expanduser()


class MCPServerConfig(BaseModel):
    """One trusted MCP server reached over Streamable HTTP."""

    model_config = ConfigDict(extra="forbid")

    url: AnyHttpUrl
    timeout_seconds: int = Field(default=30, ge=1, le=1800)
    headers_from_env: dict[str, str] = Field(default_factory=dict)

    @field_validator("headers_from_env")
    @classmethod
    def validate_header_environment_names(cls, values: dict[str, str]) -> dict[str, str]:
        """Accept header names and environment-variable references, never secret values."""
        for header, environment_name in values.items():
            if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", header):
                raise ValueError(f"Invalid HTTP header name: {header}")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", environment_name):
                raise ValueError(f"Invalid environment variable name: {environment_name}")
        return values


class TrustedSkillCommandConfig(BaseModel):
    """One user-configured host command bound to a namespaced Skill."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=500)
    argv: list[str] = Field(min_length=1, max_length=64)
    allow_args: bool = True
    timeout_seconds: int = Field(default=120, ge=1, le=1800)
    env_from_env: dict[str, str] = Field(default_factory=dict)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, values: list[str]) -> list[str]:
        if any(not value or any(char in value for char in "\x00\n\r") for value in values):
            raise ValueError("command argv entries must be non-empty and contain no control chars")
        return values

    @field_validator("env_from_env")
    @classmethod
    def validate_environment_names(cls, values: dict[str, str]) -> dict[str, str]:
        for target_name, source_name in values.items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", target_name):
                raise ValueError(f"Invalid target environment variable: {target_name}")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", source_name):
                raise ValueError(f"Invalid source environment variable: {source_name}")
        return values


class AgentConfig(BaseModel):
    """Validated runtime configuration."""

    model: str
    base_url: str | None = None
    api_key: str | None = None
    model_timeout_seconds: int = Field(default=180, ge=1, le=1800)
    main_agent_model_call_limit: int = Field(default=20, ge=1, le=200)
    subagent_model_call_limit: int = Field(default=20, ge=1, le=200)
    command_timeout_seconds: int = Field(default=120, ge=1, le=1800)
    max_read_bytes: int = Field(default=1_048_576, ge=1024)
    max_command_output_bytes: int = Field(default=65_536, ge=1024)
    max_parallel_sessions: int = Field(default=4, ge=1, le=16)
    max_parallel_tools: int = Field(default=4, ge=1, le=16)
    tool_probe_interval_seconds: int = Field(default=15, ge=1, le=300)
    max_tool_scheduler_wakes: int = Field(default=20, ge=1, le=100)
    langsmith_enabled: bool = False
    langsmith_project: str = "coding-agent-evaluation"
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)
    trusted_skill_commands: dict[str, dict[str, TrustedSkillCommandConfig]] = Field(
        default_factory=dict
    )
    sandbox_enabled: bool = True
    sandbox_provider: Literal["seatbelt", "docker"] = "seatbelt"
    sandbox_image: str | None = None
    sandbox_read_only_paths: list[str] = Field(
        default_factory=lambda: [
            "/System",
            "/Library",
            "/usr",
            "/bin",
            "/sbin",
            "/opt/homebrew",
            "/usr/local",
            "/private/var/db/timezone",
        ]
    )
    sandbox_cpu_limit: float = Field(default=2.0, gt=0, le=4)
    sandbox_memory_bytes: int = Field(default=2 * 1024**3, ge=64 * 1024**2, le=8 * 1024**3)
    sandbox_pid_limit: int = Field(default=256, ge=16, le=1024)
    sandbox_nofile_limit: int = Field(default=4096, ge=256, le=16384)
    sandbox_tmpfs_bytes: int = Field(default=512 * 1024**2, ge=16 * 1024**2, le=2 * 1024**3)
    sandbox_home_tmpfs_bytes: int = Field(
        default=64 * 1024**2,
        ge=8 * 1024**2,
        le=256 * 1024**2,
    )
    sandbox_output_bytes: int = Field(default=10 * 1024**2, ge=1024, le=50 * 1024**2)
    sandbox_workspace_growth_bytes: int = Field(
        default=1024**3,
        ge=1024**2,
        le=5 * 1024**3,
    )
    sandbox_max_parallel_per_session: int = Field(default=2, ge=1, le=16)
    sandbox_max_parallel_global: int = Field(default=4, ge=1, le=64)
    sandbox_cleanup_grace_seconds: int = Field(default=3, ge=1, le=30)
    sandbox_user: str = "1000:1000"

    @field_validator("mcp_servers")
    @classmethod
    def validate_mcp_server_names(
        cls, servers: dict[str, MCPServerConfig]
    ) -> dict[str, MCPServerConfig]:
        """Keep server names safe for deterministic tool names."""
        invalid = [
            name for name in servers if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name)
        ]
        if invalid:
            raise ValueError(
                "MCP server names must start with a letter and contain only "
                f"letters, digits, underscores, or hyphens: {', '.join(sorted(invalid))}"
            )
        return servers

    @field_validator("trusted_skill_commands")
    @classmethod
    def validate_trusted_skill_commands(
        cls,
        skills: dict[str, dict[str, TrustedSkillCommandConfig]],
    ) -> dict[str, dict[str, TrustedSkillCommandConfig]]:
        skill_id_pattern = r"[A-Za-z0-9_-]{1,64}:[A-Za-z0-9_-]{1,64}"
        capability_pattern = r"[A-Za-z0-9_-]{1,64}"
        invalid_skills = [
            skill_id for skill_id in skills if not re.fullmatch(skill_id_pattern, skill_id)
        ]
        if invalid_skills:
            raise ValueError(
                "trusted_skill_commands keys must be namespaced Skill IDs: "
                + ", ".join(sorted(invalid_skills))
            )
        workspace_skills = [skill_id for skill_id in skills if skill_id.startswith("workspace:")]
        if workspace_skills:
            raise ValueError(
                "Workspace Skills cannot receive trusted host commands: "
                + ", ".join(sorted(workspace_skills))
            )
        invalid_commands = [
            f"{skill_id}:{command_name}"
            for skill_id, commands in skills.items()
            for command_name in commands
            if not re.fullmatch(capability_pattern, command_name)
        ]
        if invalid_commands:
            raise ValueError(
                "Trusted Skill command names contain invalid characters: "
                + ", ".join(sorted(invalid_commands))
            )
        return skills

    @model_validator(mode="after")
    def validate_credentials(self) -> AgentConfig:
        """Require an API key without persisting it."""
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY or CODING_AGENT_API_KEY is required")
        return self

    @model_validator(mode="after")
    def validate_sandbox(self) -> AgentConfig:
        if self.sandbox_provider == "docker":
            if not self.sandbox_image or not self.sandbox_image.strip():
                raise ValueError("sandbox_image is required for the Docker provider")
            if not re.fullmatch(r"[0-9]+:[0-9]+", self.sandbox_user):
                raise ValueError("sandbox_user must use numeric uid:gid")
        if any(not value.startswith("/") for value in self.sandbox_read_only_paths):
            raise ValueError("sandbox_read_only_paths must contain absolute paths")
        if self.sandbox_max_parallel_per_session > self.sandbox_max_parallel_global:
            raise ValueError(
                "sandbox_max_parallel_per_session cannot exceed sandbox_max_parallel_global"
            )
        return self


def load_config(
    *,
    model: str | None,
    base_url: str | None,
    config_path: Path | None,
    langsmith_enabled: bool | None = None,
) -> AgentConfig:
    """Load CLI, environment, and JSON configuration in precedence order."""
    path = config_path or _DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise fail("INVALID_CONFIG", f"Cannot read config file: {exc}") from exc

    langsmith_setting = os.getenv("CODING_AGENT_LANGSMITH_ENABLED")
    if langsmith_setting is None:
        langsmith_setting = raw.get("langsmith_enabled")
    langsmith_credentials_present = bool(
        os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")
    )
    configured_langsmith_enabled = (
        _parse_bool(langsmith_setting)
        if langsmith_setting is not None
        else langsmith_credentials_present
    )
    values = {
        **raw,
        "model": model or os.getenv("CODING_AGENT_MODEL") or raw.get("model"),
        "base_url": base_url
        or os.getenv("CODING_AGENT_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or raw.get("base_url"),
        "api_key": os.getenv("CODING_AGENT_API_KEY") or os.getenv("OPENAI_API_KEY"),
        "langsmith_enabled": (
            langsmith_enabled if langsmith_enabled is not None else configured_langsmith_enabled
        ),
        "langsmith_project": os.getenv("CODING_AGENT_LANGSMITH_PROJECT")
        or raw.get("langsmith_project", "coding-agent-evaluation"),
        "max_parallel_sessions": os.getenv("CODING_AGENT_MAX_PARALLEL_SESSIONS")
        or raw.get("max_parallel_sessions", 4),
        "main_agent_model_call_limit": os.getenv(
            "CODING_AGENT_MAIN_AGENT_MODEL_CALL_LIMIT"
        )
        or raw.get("main_agent_model_call_limit", 20),
        "subagent_model_call_limit": os.getenv("CODING_AGENT_SUBAGENT_MODEL_CALL_LIMIT")
        or raw.get("subagent_model_call_limit", 20),
        "sandbox_enabled": os.getenv("CODING_AGENT_SANDBOX_ENABLED")
        or raw.get("sandbox_enabled", True),
        "sandbox_provider": os.getenv("CODING_AGENT_SANDBOX_PROVIDER")
        or raw.get("sandbox_provider", "seatbelt"),
        "sandbox_image": os.getenv("CODING_AGENT_SANDBOX_IMAGE")
        or raw.get("sandbox_image"),
    }
    if not values["model"]:
        raise fail(
            "MODEL_CONFIGURATION_ERROR",
            "Set --model or CODING_AGENT_MODEL before starting.",
        )
    try:
        return AgentConfig.model_validate(values)
    except ValueError as exc:
        raise fail("MODEL_CONFIGURATION_ERROR", str(exc)) from exc


def save_non_secret_config(config: AgentConfig, config_path: Path | None) -> None:
    """Atomically persist browser-editable configuration without credentials."""
    path = config_path or _DEFAULT_CONFIG_PATH
    existing = _read_raw_config(path)
    existing.update(
        {
            "model": config.model,
            "base_url": config.base_url,
            "langsmith_enabled": config.langsmith_enabled,
            "langsmith_project": config.langsmith_project,
            "model_timeout_seconds": config.model_timeout_seconds,
            "main_agent_model_call_limit": config.main_agent_model_call_limit,
            "subagent_model_call_limit": config.subagent_model_call_limit,
            "command_timeout_seconds": config.command_timeout_seconds,
            "max_parallel_sessions": config.max_parallel_sessions,
        }
    )
    _write_raw_config(path, existing)


def load_recent_workspaces(config_path: Path | None) -> list[str]:
    """Load the bounded recent-workspace list from non-secret user config."""
    raw = _read_raw_config(config_path or _DEFAULT_CONFIG_PATH)
    values = raw.get("recent_workspaces", [])
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str)][:10]


def save_recent_workspaces(workspaces: list[str], config_path: Path | None) -> None:
    """Persist a de-duplicated recent-workspace list."""
    path = config_path or _DEFAULT_CONFIG_PATH
    raw = _read_raw_config(path)
    raw["recent_workspaces"] = list(dict.fromkeys(workspaces))[:10]
    _write_raw_config(path, raw)


def _read_raw_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise fail("INVALID_CONFIG", f"Cannot read config file: {exc}") from exc
    if not isinstance(value, dict):
        raise fail("INVALID_CONFIG", "Config root must be a JSON object.")
    return value


def _write_raw_config(path: Path, values: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for secret_name in ("api_key", "openai_api_key", "langsmith_api_key"):
        values.pop(secret_name, None)
    fd, temporary_name = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(values, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as exc:
        raise fail("CONFIG_WRITE_ERROR", f"Cannot save config file: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    os.chmod(path, 0o600)


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}
