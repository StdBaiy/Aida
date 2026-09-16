"""Workspace mutation lease shared by concurrent session runners."""

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from coding_agent.errors import fail


class WorkspaceMutationGate:
    """Allow one logical Agent thread to mutate and snapshot the workspace."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._owner: str | None = None

    def acquire(
        self,
        owner: str,
        cancelled: threading.Event | None = None,
    ) -> None:
        with self._condition:
            while self._owner not in {None, owner}:
                if cancelled is not None and cancelled.is_set():
                    raise fail("OPERATION_CANCELLED", "Workspace lease wait was cancelled.")
                self._condition.wait(timeout=0.1)
            self._owner = owner

    def release(self, owner: str) -> None:
        with self._condition:
            if self._owner != owner:
                return
            self._owner = None
            self._condition.notify_all()

    def owns(self, owner: str) -> bool:
        """Return whether this logical operation currently owns the lease."""
        with self._condition:
            return self._owner == owner


@dataclass(frozen=True)
class _MutationContext:
    gate: WorkspaceMutationGate
    owner: str
    cancelled: threading.Event | None


_ACTIVE_MUTATION: ContextVar[_MutationContext | None] = ContextVar(
    "active_workspace_mutation",
    default=None,
)


@contextmanager
def activate_workspace_mutation(
    gate: WorkspaceMutationGate | None,
    owner: str,
    cancelled: threading.Event | None,
) -> Generator[None]:
    """Expose the current turn's mutation lease to workspace-writing tools."""
    if gate is None:
        yield
        return
    token = _ACTIVE_MUTATION.set(_MutationContext(gate, owner, cancelled))
    try:
        yield
    finally:
        _ACTIVE_MUTATION.reset(token)


def acquire_workspace_mutation() -> None:
    """Acquire the active turn's lease before the first workspace side effect."""
    context = _ACTIVE_MUTATION.get()
    if context is not None:
        context.gate.acquire(context.owner, context.cancelled)
