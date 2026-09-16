"""Workspace path confinement."""

from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath

from coding_agent.errors import fail

_SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa",
    "id_ed25519",
    "*credentials*",
)


class PathGuard:
    """Resolve user paths while preventing workspace and symlink escapes."""

    def __init__(self, workspace_root: Path) -> None:
        self.root = workspace_root.resolve(strict=True)

    def _validate_relative(self, value: str) -> PurePosixPath:
        if not value or "\x00" in value:
            raise fail("PATH_OUTSIDE_WORKSPACE", "Path must be a non-empty relative path.")
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise fail("PATH_OUTSIDE_WORKSPACE", f"Path escapes workspace: {value}")
        if ".git" in path.parts:
            raise fail("PROTECTED_PATH", "Access to .git is forbidden.")
        return path

    def _check_secret(self, path: PurePosixPath) -> None:
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in _SECRET_PATTERNS):
            raise fail("PROTECTED_PATH", f"Sensitive path is not readable: {path}")

    def resolve_for_read(self, value: str) -> Path:
        """Resolve an existing readable path."""
        relative = self._validate_relative(value)
        self._check_secret(relative)
        try:
            resolved = (self.root / relative).resolve(strict=True)
        except FileNotFoundError as exc:
            raise fail("FILE_NOT_FOUND", f"Path does not exist: {value}") from exc
        if not resolved.is_relative_to(self.root):
            raise fail("PATH_OUTSIDE_WORKSPACE", f"Symlink escapes workspace: {value}")
        return resolved

    def resolve_for_write(self, value: str) -> Path:
        """Resolve a writable target, including a new leaf."""
        relative = self._validate_relative(value)
        self._check_secret(relative)
        target = self.root / relative
        ancestor = target
        while not ancestor.exists():
            if ancestor == self.root:
                break
            ancestor = ancestor.parent
        resolved_ancestor = ancestor.resolve(strict=True)
        if not resolved_ancestor.is_relative_to(self.root):
            raise fail("PATH_OUTSIDE_WORKSPACE", f"Parent symlink escapes workspace: {value}")
        if target.exists() and not target.resolve(strict=True).is_relative_to(self.root):
            raise fail("PATH_OUTSIDE_WORKSPACE", f"Symlink escapes workspace: {value}")
        return target

    def validate_cwd(self, value: str) -> Path:
        """Resolve a command working directory."""
        path = self.resolve_for_read(value)
        if not path.is_dir():
            raise fail("INVALID_CWD", f"Command cwd is not a directory: {value}")
        return path

    def relative(self, path: Path) -> str:
        """Return a stable POSIX path relative to the workspace."""
        return path.relative_to(self.root).as_posix() or "."
