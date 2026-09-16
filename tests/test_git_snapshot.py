import subprocess
from pathlib import Path

from coding_agent.workspace.git import GitSnapshotStore, resolve_workspace


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def test_snapshot_and_restore_preserve_head_index_and_ignored(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / ".gitignore").write_text("ignored.txt\n")
    (repo / "tracked.txt").write_text("baseline\n")
    git(repo, "add", ".")
    tree = git(repo, "write-tree")
    commit = subprocess.run(
        ["git", "-C", str(repo), "commit-tree", tree],
        input="initial\n",
        capture_output=True,
        check=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    ).stdout.strip()
    git(repo, "update-ref", "HEAD", commit)

    workspace = resolve_workspace(repo)
    snapshots = GitSnapshotStore(workspace)
    head = git(repo, "rev-parse", "HEAD")
    index = git(repo, "write-tree")
    baseline = snapshots.create(
        ref="refs/coding-agent/test/baseline",
        parent_oid=None,
        reason="baseline",
    )

    (repo / "tracked.txt").write_text("changed\n")
    (repo / "added.txt").write_text("added\n")
    changed = snapshots.create(
        ref="refs/coding-agent/test/changed",
        parent_oid=baseline,
        reason="changed",
    )
    (repo / "ignored.txt").write_text("keep\n")
    (repo / "tracked.txt").write_text("later\n")
    (repo / "added.txt").unlink()

    snapshots.restore(changed)

    assert (repo / "tracked.txt").read_text() == "changed\n"
    assert (repo / "added.txt").read_text() == "added\n"
    assert (repo / "ignored.txt").read_text() == "keep\n"
    assert (workspace.data_dir / "temp").is_dir()
    assert git(repo, "rev-parse", "HEAD") == head
    assert git(repo, "write-tree") == index
