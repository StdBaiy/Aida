"""Git-backed snapshots that do not touch HEAD or the real index."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path

from coding_agent.errors import fail
from coding_agent.models import Workspace
from coding_agent.workspace.paths import PathGuard


def _git(
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
        raise fail("GIT_ERROR", message or f"git {' '.join(args)} failed")
    return process.stdout


def resolve_workspace(path: Path) -> Workspace:
    """Resolve a worktree directory and its private Git data directory."""
    try:
        root = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise fail("INVALID_WORKSPACE", f"Workspace does not exist: {path}") from exc
    if not root.is_dir():
        raise fail("INVALID_WORKSPACE", f"Workspace is not a directory: {root}")
    try:
        repo_root = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir").decode().strip()).resolve()
    except Exception as exc:
        raise fail("NOT_A_GIT_REPOSITORY", f"Not a Git repository: {root}") from exc
    if not root.is_relative_to(repo_root):
        raise fail("INVALID_WORKSPACE", "Workspace must be inside the Git worktree.")
    data_dir = git_dir / "coding-agent"
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)
    for name in ("temp", "locks"):
        child = data_dir / name
        child.mkdir(mode=0o700, exist_ok=True)
        os.chmod(child, 0o700)
    return Workspace(repo_root=repo_root, root=root, git_dir=git_dir, data_dir=data_dir)


class GitSnapshotStore:
    """Store worktree states as private commit objects."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.guard = PathGuard(workspace.root)

    def create(self, *, ref: str, parent_oid: str | None, reason: str) -> str:
        """Create a snapshot through a temporary index."""
        temp_dir = self.workspace.data_dir / "temp"
        temp_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(temp_dir, 0o700)
        fd, index_name = tempfile.mkstemp(prefix="index-", dir=temp_dir)
        os.close(fd)
        index = Path(index_name)
        index.unlink()
        env = {
            "GIT_INDEX_FILE": str(index),
            "GIT_AUTHOR_NAME": "Coding Agent",
            "GIT_AUTHOR_EMAIL": "coding-agent@localhost",
            "GIT_COMMITTER_NAME": "Coding Agent",
            "GIT_COMMITTER_EMAIL": "coding-agent@localhost",
        }
        try:
            head = subprocess.run(
                ["git", "-C", str(self.workspace.repo_root), "rev-parse", "--verify", "HEAD"],
                capture_output=True,
                check=False,
            )
            if head.returncode == 0:
                _git(self.workspace.repo_root, "read-tree", "HEAD", env=env)
            else:
                _git(self.workspace.repo_root, "read-tree", "--empty", env=env)
            _git(self.workspace.repo_root, "add", "-A", "--", ".", env=env)
            tree_oid = _git(self.workspace.repo_root, "write-tree", env=env).decode().strip()
            args = ["commit-tree", tree_oid]
            if parent_oid:
                args.extend(["-p", parent_oid])
            oid = (
                _git(
                    self.workspace.repo_root,
                    *args,
                    env=env,
                    stdin=f"{reason}\n".encode(),
                )
                .decode()
                .strip()
            )
            _git(self.workspace.repo_root, "update-ref", ref, oid)
            return oid
        finally:
            index.unlink(missing_ok=True)

    def point_ref(self, ref: str, oid: str) -> None:
        """Point a private timeline ref at an existing snapshot."""
        _git(self.workspace.repo_root, "update-ref", ref, oid)

    def diff_stats(self, old_oid: str, new_oid: str) -> dict[str, int]:
        """Return path-free line and file counts between two snapshots."""
        prefix = self.workspace.root.relative_to(self.workspace.repo_root).as_posix()
        args = ["diff", "--numstat", "--no-renames", old_oid, new_oid]
        if prefix != ".":
            args.extend(["--", prefix])
        output = _git(self.workspace.repo_root, *args).decode(errors="replace")
        changed_file_count = added_lines = deleted_lines = 0
        for line in output.splitlines():
            fields = line.split("\t", 2)
            if len(fields) != 3:
                continue
            changed_file_count += 1
            if fields[0].isdigit():
                added_lines += int(fields[0])
            if fields[1].isdigit():
                deleted_lines += int(fields[1])
        return {
            "changed_file_count": changed_file_count,
            "added_lines": added_lines,
            "deleted_lines": deleted_lines,
        }

    def restore(self, target_oid: str) -> None:
        """Restore workspace files from a tree while retaining ignored files."""
        prefix = self.workspace.root.relative_to(self.workspace.repo_root).as_posix()
        target = self._manifest(target_oid, prefix)
        current_oid = self.create(
            ref="refs/coding-agent/recovery/latest",
            parent_oid=None,
            reason="pre-restore safety snapshot",
        )
        current = self._manifest(current_oid, prefix)

        removed = sorted(
            current.keys() - target.keys(),
            key=lambda item: len(item.parts),
            reverse=True,
        )
        for relative in removed:
            path = self.guard.resolve_for_write(relative.as_posix())
            if path.is_dir() and not path.is_symlink():
                continue
            path.unlink(missing_ok=True)

        try:
            for relative, (mode, blob_oid) in target.items():
                path = self.guard.resolve_for_write(relative.as_posix())
                if path.exists() and path.is_dir() and not path.is_symlink():
                    for child in sorted(path.rglob("*"), reverse=True):
                        if child.is_file() or child.is_symlink():
                            child.unlink()
                        elif child.is_dir():
                            child.rmdir()
                    path.rmdir()
                content = _git(self.workspace.repo_root, "cat-file", "blob", blob_oid)
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() or path.is_symlink():
                    path.unlink()
                if mode == "120000":
                    os.symlink(content.decode(), path)
                elif mode in {"100644", "100755"}:
                    path.write_bytes(content)
                    os.chmod(
                        path,
                        stat.S_IRUSR
                        | stat.S_IWUSR
                        | stat.S_IRGRP
                        | stat.S_IROTH
                        | (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH if mode == "100755" else 0),
                    )
                else:
                    raise fail("UNSUPPORTED_GIT_MODE", f"Cannot restore mode {mode}: {relative}")
            self._remove_empty_directories()
        except Exception:
            if current_oid != target_oid:
                self._restore_without_safety(current_oid, prefix)
            raise

    def _restore_without_safety(self, oid: str, prefix: str) -> None:
        target = self._manifest(oid, prefix)
        current = self._manifest(
            self.create(
                ref="refs/coding-agent/recovery/rollback",
                parent_oid=None,
                reason="failed restore state",
            ),
            prefix,
        )
        removed = sorted(
            current.keys() - target.keys(),
            key=lambda item: len(item.parts),
            reverse=True,
        )
        for relative in removed:
            path = self.guard.resolve_for_write(relative.as_posix())
            if path.is_file() or path.is_symlink():
                path.unlink()
        for relative, (mode, blob_oid) in target.items():
            path = self.guard.resolve_for_write(relative.as_posix())
            content = _git(self.workspace.repo_root, "cat-file", "blob", blob_oid)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.is_dir() and not path.is_symlink():
                for child in sorted(path.rglob("*"), reverse=True):
                    if child.is_file() or child.is_symlink():
                        child.unlink()
                    elif child.is_dir():
                        child.rmdir()
                path.rmdir()
            if path.exists() or path.is_symlink():
                path.unlink()
            if mode == "120000":
                os.symlink(content.decode(), path)
            else:
                path.write_bytes(content)
                os.chmod(path, 0o755 if mode == "100755" else 0o644)

    def _manifest(self, oid: str, prefix: str) -> dict[Path, tuple[str, str]]:
        args = ["ls-tree", "-r", "-z", oid]
        if prefix != ".":
            args.extend(["--", prefix])
        output = _git(self.workspace.repo_root, *args)
        result: dict[Path, tuple[str, str]] = {}
        for entry in output.split(b"\0"):
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            mode, _, blob_oid = metadata.decode().split()
            repo_path = Path(raw_path.decode())
            relative = repo_path if prefix == "." else repo_path.relative_to(prefix)
            result[relative] = (mode, blob_oid)
        return result

    def _remove_empty_directories(self) -> None:
        paths = sorted(
            self.workspace.root.rglob("*"),
            key=lambda item: len(item.parts),
            reverse=True,
        )
        for path in paths:
            relative = path.relative_to(self.workspace.root)
            if relative.parts and relative.parts[0] == ".git":
                continue
            if path.is_dir() and not path.is_symlink():
                with suppress(OSError):
                    path.rmdir()
