import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coding_agent.application.service import CodingAgentHost
from coding_agent.workspace import resolve_workspace


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_diff_preserves_porcelain_status_spacing_and_full_filename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.com")
    hooks = tmp_path / "test-hooks"
    hooks.mkdir()
    git(tmp_path, "config", "core.hooksPath", str(hooks))
    calculator = tmp_path / "calculator.py"
    calculator.write_text("def add(a, b):\n    return a + b\n")
    git(tmp_path, "add", "calculator.py")
    git(tmp_path, "commit", "-m", "baseline")
    calculator.write_text(
        "def add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\n"
    )

    host = CodingAgentHost.__new__(CodingAgentHost)
    host.workspace = resolve_workspace(tmp_path)
    host.config = SimpleNamespace(max_read_bytes=1_048_576)
    original_file_diff = host._file_diff
    requested_patches: list[str] = []

    def tracked_file_diff(path: str, status: str) -> str:
        requested_patches.append(path)
        return original_file_diff(path, status)

    monkeypatch.setattr(host, "_file_diff", tracked_file_diff)

    result = host.diff()
    assert len(result["files"]) == 1
    changed = result["files"][0]
    assert changed["path"] == "calculator.py"
    assert changed["status"] == "M"
    assert changed["additions"] == 4
    assert changed["patch"] == ""
    assert requested_patches == []

    detail = host.diff("calculator.py")["files"][0]
    assert requested_patches == ["calculator.py"]
    assert "+++ b/calculator.py" in detail["patch"]
    assert "+def subtract(a, b):" in detail["patch"]

    files = host.files()
    assert {"path": "calculator.py", "size": calculator.stat().st_size} in files
    content = host.file_content("calculator.py")
    assert content["content"].startswith("def add")
    assert not content["binary"]
