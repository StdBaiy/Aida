"""Workspace safety and snapshot services."""

from coding_agent.workspace.git import GitSnapshotStore, resolve_workspace
from coding_agent.workspace.paths import PathGuard

__all__ = ["GitSnapshotStore", "PathGuard", "resolve_workspace"]
