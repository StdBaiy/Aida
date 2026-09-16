"""Validated, rollback-capable structured text patches."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from coding_agent.errors import fail
from coding_agent.workspace.paths import PathGuard

MAX_PATCH_ARGUMENT_CHARS = 32_000


def sha256_bytes(value: bytes) -> str:
    """Hash file content for optimistic concurrency."""
    return hashlib.sha256(value).hexdigest()


class Replacement(BaseModel):
    """One exact text replacement."""

    old_text: str
    new_text: str
    expected_occurrences: int = Field(default=1, ge=1)


class AddOperation(BaseModel):
    """Add a new UTF-8 file."""

    op: Literal["add"]
    path: str
    content: str


class UpdateOperation(BaseModel):
    """Update an existing file at an expected version."""

    op: Literal["update"]
    path: str
    expected_sha256: str
    replacements: list[Replacement] = Field(min_length=1)


class DeleteOperation(BaseModel):
    """Delete an existing file at an expected version."""

    op: Literal["delete"]
    path: str
    expected_sha256: str


Operation = Annotated[
    AddOperation | UpdateOperation | DeleteOperation,
    Field(discriminator="op"),
]


class ApplyPatchInput(BaseModel):
    """A bounded atomic-intent patch."""

    operations: list[Operation] = Field(min_length=1, max_length=50)


class PatchService:
    """Validate a complete patch before writing and roll back partial failures."""

    def __init__(
        self,
        guard: PathGuard,
        *,
        max_bytes: int = 2 * 1024 * 1024,
        max_argument_chars: int = MAX_PATCH_ARGUMENT_CHARS,
    ) -> None:
        self.guard = guard
        self.max_bytes = max_bytes
        self.max_argument_chars = max_argument_chars

    def apply(self, patch: ApplyPatchInput) -> dict[str, object]:
        """Apply all operations after validating the entire request."""
        argument_chars = len(
            json.dumps(
                patch.model_dump(mode="json"),
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
        if argument_chars > self.max_argument_chars:
            raise fail(
                "PATCH_TOO_LARGE",
                f"Patch arguments contain {argument_chars} characters; the hard limit is "
                f"{self.max_argument_chars}. Split the same file into smaller sequential "
                "apply_patch calls.",
            )

        added_paths = [
            operation.path for operation in patch.operations if operation.op == "add"
        ]
        if len(added_paths) > 1:
            raise fail(
                "PATCH_TOO_MANY_NEW_FILES",
                "One apply_patch call may add only one file. Create files in separate calls.",
            )

        planned: list[tuple[Path, bytes | None, int]] = []
        originals: dict[Path, tuple[bytes, int] | None] = {}
        input_size = 0
        output_size = 0
        seen: set[Path] = set()

        for operation in patch.operations:
            target = self.guard.resolve_for_write(operation.path)
            if target in seen:
                raise fail("PATCH_CONFLICT", f"Path appears more than once: {operation.path}")
            seen.add(target)
            current = target.read_bytes() if target.exists() else None
            mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
            originals[target] = (current, mode) if current is not None else None

            if operation.op == "add":
                if current is not None:
                    raise fail("PATCH_CONFLICT", f"File already exists: {operation.path}")
                new_bytes = operation.content.encode()
                input_size += len(new_bytes)
            else:
                if current is None or not target.is_file():
                    raise fail("FILE_NOT_FOUND", f"File does not exist: {operation.path}")
                if sha256_bytes(current) != operation.expected_sha256:
                    raise fail("STALE_FILE", f"File changed since it was read: {operation.path}")
                input_size += len(current)
                if operation.op == "delete":
                    new_bytes = None
                else:
                    try:
                        text = current.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise fail(
                            "BINARY_OR_NON_UTF8_FILE",
                            f"Cannot patch non-UTF-8 file: {operation.path}",
                        ) from exc
                    for replacement in operation.replacements:
                        count = text.count(replacement.old_text)
                        if count != replacement.expected_occurrences:
                            raise fail(
                                "PATCH_CONFLICT",
                                f"{operation.path}: expected {replacement.expected_occurrences} "
                                f"matches, found {count}.",
                            )
                        text = text.replace(
                            replacement.old_text,
                            replacement.new_text,
                            replacement.expected_occurrences,
                        )
                    new_bytes = text.encode()
            output_size += len(new_bytes or b"")
            planned.append((target, new_bytes, mode))

        if input_size > self.max_bytes or output_size > self.max_bytes:
            raise fail("FILE_TOO_LARGE", "Patch input or output exceeds 2 MiB.")

        changed: list[dict[str, str | None]] = []
        try:
            for target, content, mode in planned:
                if content is None:
                    target.unlink()
                    changed.append({"path": self.guard.relative(target), "sha256": None})
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                self._atomic_write(target, content, mode)
                changed.append(
                    {"path": self.guard.relative(target), "sha256": sha256_bytes(content)}
                )
        except Exception:
            self._restore(originals)
            raise
        return {"changed": changed}

    @staticmethod
    def _atomic_write(target: Path, content: bytes, mode: int) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=".coding-agent-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, mode)
            os.replace(temp_name, target)
        finally:
            Path(temp_name).unlink(missing_ok=True)

    def _restore(self, originals: dict[Path, tuple[bytes, int] | None]) -> None:
        for target, original in originals.items():
            if original is None:
                target.unlink(missing_ok=True)
            else:
                content, mode = original
                target.parent.mkdir(parents=True, exist_ok=True)
                self._atomic_write(target, content, mode)
