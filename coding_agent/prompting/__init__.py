"""Versioned prompt resources and deterministic, bounded context composition.

Escaping preserves document structure; it is not an authorization mechanism or a
guarantee against semantic prompt injection. Runtime permissions remain authoritative.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from html import escape
from importlib.resources import files
from typing import Annotated, Any, Literal

from langchain_core.tools import BaseTool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from coding_agent.errors import fail


def canonical_json(value: Any) -> str:
    """Serialize data once, without evaluating it as a template."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PromptDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z_]+$")
    version: str
    file: str = Field(pattern=r"^[a-z_]+\.md$")
    tools: tuple[str, ...]


class PromptManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: str
    version: str
    owner: str
    max_bundle_chars: int = Field(gt=0)
    components: tuple[PromptDefinition, ...]


class ChildContract(BaseModel):
    """Task input, deliberately separate from the platform's child instructions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    objective: str = Field(min_length=1, max_length=16000)
    scope: list[Annotated[str, Field(min_length=1, max_length=2048)]] = Field(
        min_length=1,
        max_length=200,
    )
    acceptance: list[Annotated[str, Field(min_length=1, max_length=4096)]] = Field(
        min_length=1,
        max_length=100,
    )
    feedback: str = Field(default="", max_length=16000)
    parent_responses: list[Annotated[str, Field(max_length=16000)]] = Field(
        default_factory=list,
        max_length=100,
    )
    workspace_mode: Literal["none", "auto", "required"]


_ASSETS = files(__package__).joinpath("assets")
MANIFEST = PromptManifest.model_validate_json(
    _ASSETS.joinpath("manifest.json").read_text(encoding="utf-8")
)
_TEXTS = {
    definition.id: _ASSETS.joinpath(definition.file).read_text(encoding="utf-8").strip()
    for definition in MANIFEST.components
}
_EVENTS: dict[str, str] = json.loads(_ASSETS.joinpath("events.json").read_text(encoding="utf-8"))
if len(_TEXTS) != len(MANIFEST.components) or not _TEXTS.get("core"):
    raise ValueError("Prompt manifest requires unique components and a non-empty core")
BASE_PROMPT = _TEXTS["core"]


@dataclass(frozen=True)
class PromptComponent:
    id: str
    version: str
    source: str
    trust: str
    text: str

    def metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "source": self.source,
            "trust": self.trust,
            "digest": digest(self.text),
            "chars": len(self.text),
        }


@dataclass(frozen=True)
class PromptBundle:
    components: tuple[PromptComponent, ...]
    tool_schema_digest: str

    @property
    def text(self) -> str:
        return "\n\n".join(component.text for component in self.components)

    def metadata(self) -> dict[str, Any]:
        """Return audit metadata without persisting dynamic context plaintext."""
        components = [component.metadata() for component in self.components]
        identity = {
            "profile": MANIFEST.profile,
            "version": MANIFEST.version,
            "components": components,
            "tool_schema_digest": self.tool_schema_digest,
            "events_digest": digest(canonical_json(_EVENTS)),
        }
        return {
            **identity,
            "bundle_digest": digest(canonical_json(identity)),
            "system_digest": digest(self.text),
        }


def data_section(name: str, text: str, *, limit: int = 32768) -> str:
    """Bound data and escape tag delimiters, including nested/forged closing tags."""
    if name not in {"skill_catalog", "task_contract", "runtime_event", "phase_context"}:
        raise ValueError(f"Unknown prompt data section: {name}")
    if len(text) > limit:
        raise fail("PROMPT_CONTEXT_TOO_LARGE", f"Prompt section {name} exceeds {limit} characters.")
    return f'<{name} trust="data">\n{escape(text, quote=False)}\n</{name}>'


def child_contract(**values: Any) -> str:
    contract = ChildContract.model_validate(values)
    text = canonical_json(contract.model_dump())
    # Validate the combined size as well as individual field limits.
    data_section("task_contract", text, limit=65536)
    return text


def event_text(kind: str, payload: dict[str, Any] | None = None) -> str:
    """Render only known platform event instructions; never interpolate payload as code."""
    instruction = _EVENTS[kind]
    data = data_section("runtime_event", canonical_json(payload or {}), limit=65536)
    return f"{instruction}\n\n{data}"


def phase_context(objective: str, analysis: str) -> str:
    return (
        event_text("workspace_ready")
        + "\n\n"
        + data_section(
            "phase_context",
            canonical_json({"objective": objective, "prior_analysis": analysis}),
            limit=65536,
        )
    )


def assemble_prompt(
    *,
    skill_catalog: str = "",
    role_instruction: str | None = None,
    tools: list[BaseTool] | None = None,
) -> PromptBundle:
    """Select instructions using actual capabilities and render untrusted data once."""
    names = {tool.name for tool in tools or []}
    components: list[PromptComponent] = []
    for definition in MANIFEST.components:
        if definition.id != "core" and not set(definition.tools) <= names:
            continue
        if definition.id in {"skills", "skill_execution"} and not skill_catalog.strip():
            continue
        components.append(
            PromptComponent(
                definition.id,
                definition.version,
                f"bundled:{definition.file}",
                "platform",
                _TEXTS[definition.id],
            )
        )
    # Compatibility callers can render a catalog without constructing a live Runtime.
    if skill_catalog.strip() and (tools is None or "load_skill" in names):
        if tools is None:
            components.append(
                PromptComponent(
                    "skills",
                    "2.0.0",
                    "bundled:skills.md",
                    "platform",
                    _TEXTS["skills"],
                )
            )
        components.append(
            PromptComponent(
                "skill_catalog",
                "1.0.0",
                "skill_registry",
                "data",
                data_section("skill_catalog", skill_catalog),
            )
        )
    if role_instruction:
        components.append(
            PromptComponent(
                "task_contract",
                "1.0.0",
                "parent_agent",
                "data",
                data_section("task_contract", role_instruction, limit=65536),
            )
        )
    schemas = [convert_to_openai_tool(tool) for tool in tools or []]
    bundle = PromptBundle(tuple(components), digest(canonical_json(schemas)))
    if len(bundle.text) > MANIFEST.max_bundle_chars:
        raise fail("PROMPT_CONTEXT_TOO_LARGE", "The assembled prompt exceeds its size budget.")
    return bundle
