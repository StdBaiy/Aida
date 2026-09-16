from pathlib import Path

import pytest

from coding_agent.errors import CodingAgentError
from coding_agent.workspace.paths import PathGuard


def test_path_guard_rejects_escape_and_protected_paths(tmp_path: Path) -> None:
    guard = PathGuard(tmp_path)

    for value in ("../outside", "/tmp/outside", ".git/config", ".env"):
        with pytest.raises(CodingAgentError):
            guard.resolve_for_read(value)


def test_path_guard_rejects_external_symlink(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    (tmp_path / "link").symlink_to(outside)

    with pytest.raises(CodingAgentError, match="Symlink escapes"):
        PathGuard(tmp_path).resolve_for_read("link")


def test_path_guard_allows_new_nested_file(tmp_path: Path) -> None:
    target = PathGuard(tmp_path).resolve_for_write("new/module.py")

    assert target == tmp_path / "new" / "module.py"


def test_path_guard_rejects_writing_sensitive_file(tmp_path: Path) -> None:
    with pytest.raises(CodingAgentError):
        PathGuard(tmp_path).resolve_for_write("config/credentials.json")
