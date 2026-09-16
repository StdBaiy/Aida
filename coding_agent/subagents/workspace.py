"""Git worktree isolation for subagent attempts."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import threading
from pathlib import Path

from coding_agent.errors import fail
from coding_agent.models import Workspace

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Coding Agent Subagent",
    "GIT_AUTHOR_EMAIL": "subagent@localhost",
    "GIT_COMMITTER_NAME": "Coding Agent Subagent",
    "GIT_COMMITTER_EMAIL": "subagent@localhost",
}


class SubagentWorkspaceManager:
    """Create isolated worktrees and publish accepted commits to private refs."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        repo_key = hashlib.sha256(str(workspace.repo_root).encode()).hexdigest()[:12]
        self.worktrees_root = (
            Path(tempfile.gettempdir()) / "coding-agent-subagents" / repo_key
        )
        self.worktrees_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._git_lock = threading.RLock()

    def head_commit(self) -> str:
        return self._git(self.workspace.repo_root, "rev-parse", "HEAD")

    def create_attempt(
        self,
        *,
        run_id: str,
        task_name: str,
        attempt_id: str,
        attempt_number: int,
        base_commit: str,
    ) -> tuple[Path, Path, str]:
        del task_name
        worktree = self.worktrees_root / attempt_id
        attempt_key = hashlib.sha256(attempt_id.encode()).hexdigest()[:12]
        branch = (
            f"coding-agent/demo/{run_id[:8]}/{attempt_key}"
            f"/attempt-{attempt_number}"
        )
        with self._git_lock:
            self._git(
                self.workspace.repo_root,
                "worktree",
                "add",
                "-b",
                branch,
                str(worktree),
                base_commit,
            )
        prefix = self.workspace.root.relative_to(self.workspace.repo_root)
        attempt_workspace = worktree if prefix == Path(".") else worktree / prefix
        attempt_workspace.mkdir(parents=True, exist_ok=True)
        return worktree, attempt_workspace, branch

    def commit(self, worktree: Path, *, message: str) -> str:
        with self._git_lock:
            self._git(worktree, "add", "-A")
            self._git(
                worktree,
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "-m",
                message,
                env=_GIT_IDENTITY,
            )
            return self._git(worktree, "rev-parse", "HEAD")

    def changed_paths(
        self,
        base_commit: str,
        result_commit: str,
        *,
        cwd: Path | None = None,
    ) -> list[str]:
        output = self._git(
            cwd or self.workspace.repo_root,
            "diff",
            "--name-only",
            base_commit,
            result_commit,
        )
        return [line for line in output.splitlines() if line]

    def file_at_commit(self, commit: str, workspace_relative_path: str) -> str:
        repo_path = (self.workspace.root / workspace_relative_path).relative_to(
            self.workspace.repo_root
        )
        return self._git(
            self.workspace.repo_root,
            "show",
            f"{commit}:{repo_path.as_posix()}",
        )

    def integrate(self, run_id: str, task_id: str, result_commit: str) -> str:
        """Publish an accepted result without modifying the user's checked-out branch."""
        ref = f"refs/coding-agent/subagent-integrations/{run_id}/{task_id}"
        with self._git_lock:
            self._git(self.workspace.repo_root, "update-ref", ref, result_commit)
            return self._git(self.workspace.repo_root, "rev-parse", ref)

    def check_apply_to_parent(self, base_commit: str, result_commit: str) -> None:
        """Verify that a child diff can be applied to the parent workspace."""
        with self._git_lock:
            patch = self._result_patch(base_commit, result_commit)
            self._git_bytes(
                self.workspace.repo_root,
                "apply",
                "--check",
                "--whitespace=nowarn",
                stdin=patch,
            )

    def apply_to_parent(
        self,
        base_commit: str,
        result_commit: str,
        *,
        prechecked: bool = False,
    ) -> str:
        """Apply an accepted child diff to the parent workspace without committing it."""
        with self._git_lock:
            patch = self._result_patch(base_commit, result_commit)
            if not prechecked:
                self._git_bytes(
                    self.workspace.repo_root,
                    "apply",
                    "--check",
                    "--whitespace=nowarn",
                    stdin=patch,
                )
            self._git_bytes(
                self.workspace.repo_root,
                "apply",
                "--whitespace=nowarn",
                stdin=patch,
            )
            return result_commit

    def patch_state(self, base_commit: str, result_commit: str) -> str:
        """Classify an interrupted integration without mutating the workspace."""
        with self._git_lock:
            patch = self._result_patch(base_commit, result_commit)
            if self._git_succeeds(
                self.workspace.repo_root,
                "apply",
                "--reverse",
                "--check",
                "--whitespace=nowarn",
                stdin=patch,
            ):
                return "applied"
            if self._git_succeeds(
                self.workspace.repo_root,
                "apply",
                "--check",
                "--whitespace=nowarn",
                stdin=patch,
            ):
                return "not_applied"
            return "conflict"

    def _result_patch(self, base_commit: str, result_commit: str) -> bytes:
        patch = self._git_bytes(
            self.workspace.repo_root,
            "diff",
            "--binary",
            base_commit,
            result_commit,
            "--",
            self.workspace.root.relative_to(self.workspace.repo_root).as_posix(),
        )
        if not patch:
            raise fail("SUBAGENT_EMPTY_RESULT", "The child result has no workspace changes.")
        return patch

    @staticmethod
    def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
        return SubagentWorkspaceManager._git_bytes(cwd, *args, env=env).decode(
            errors="replace"
        ).strip()

    @staticmethod
    def _git_bytes(
        cwd: Path,
        *args: str,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> bytes:
        process = subprocess.run(
            ["git", "-C", str(cwd), *args],
            input=stdin,
            capture_output=True,
            check=False,
            env={**os.environ, **(env or {})},
        )
        if process.returncode:
            message = process.stderr.decode(errors="replace").strip()
            raise fail("SUBAGENT_GIT_ERROR", message or f"git {' '.join(args)} failed")
        return process.stdout

    @staticmethod
    def _git_succeeds(
        cwd: Path,
        *args: str,
        stdin: bytes | None = None,
    ) -> bool:
        process = subprocess.run(
            ["git", "-C", str(cwd), *args],
            input=stdin,
            capture_output=True,
            check=False,
            env=os.environ,
        )
        return process.returncode == 0
