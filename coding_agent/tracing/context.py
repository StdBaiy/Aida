"""Current-turn trace context for low-level execution adapters."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

from coding_agent.tracing.recorder import TraceRecorder

_ACTIVE_RECORDER: ContextVar[TraceRecorder | None] = ContextVar(
    "coding_agent_trace_recorder",
    default=None,
)


@contextmanager
def activate_recorder(recorder: TraceRecorder | None) -> Generator[None, None, None]:
    """Expose a recorder to tools invoked in the current context."""
    token = _ACTIVE_RECORDER.set(recorder)
    try:
        yield
    finally:
        _ACTIVE_RECORDER.reset(token)


def active_recorder() -> TraceRecorder | None:
    """Return the current turn recorder, when tracing is active."""
    return _ACTIVE_RECORDER.get()
