"""Resource admission and sandbox usage helpers."""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager, suppress
from pathlib import Path

from coding_agent.errors import fail
from coding_agent.sandbox.models import ResourceUsage

_UNITS = {
    "B": 1,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KIB": 1024,
    "MIB": 1024**2,
    "GIB": 1024**3,
    "TIB": 1024**4,
}


def parse_size(value: str) -> int:
    """Parse Docker's human-readable byte values."""
    match = re.fullmatch(r"\s*([0-9.]+)\s*([A-Za-z]+)\s*", value)
    if match is None:
        return 0
    return int(float(match.group(1)) * _UNITS.get(match.group(2).upper(), 0))


class ResourceUsageAccumulator:
    """Merge periodic Docker statistics into a bounded summary."""

    def __init__(self, *, workspace_bytes_before: int) -> None:
        self.usage = ResourceUsage(workspace_bytes_before=workspace_bytes_before)

    def observe(self, sample: dict[str, str]) -> ResourceUsage:
        memory = parse_size(sample.get("MemUsage", "").partition("/")[0])
        pids = _integer(sample.get("PIDs", "0"))
        cpu = _float(sample.get("CPUPerc", "0").rstrip("%"))
        block_in, _, block_out = sample.get("BlockIO", "").partition("/")
        self.usage.peak_memory_bytes = max(self.usage.peak_memory_bytes, memory)
        self.usage.peak_pids = max(self.usage.peak_pids, pids)
        self.usage.cpu_percent_peak = max(self.usage.cpu_percent_peak, cpu)
        self.usage.block_read_bytes = max(self.usage.block_read_bytes, parse_size(block_in))
        self.usage.block_write_bytes = max(
            self.usage.block_write_bytes, parse_size(block_out)
        )
        self.usage.samples += 1
        return self.usage.model_copy(deep=True)

    def finish(self, *, workspace_bytes_after: int, oom_killed: bool) -> ResourceUsage:
        self.usage.workspace_bytes_after = workspace_bytes_after
        self.usage.workspace_growth_bytes = max(
            0, workspace_bytes_after - self.usage.workspace_bytes_before
        )
        self.usage.oom_killed = oom_killed
        return self.usage.model_copy(deep=True)


class ResourceAdmission:
    """Cancellation-aware local/global execution slots."""

    _global_lock = threading.Condition(threading.RLock())
    _global_active = 0

    def __init__(self, *, session_limit: int, global_limit: int) -> None:
        self.session_limit = session_limit
        self.global_limit = global_limit
        self._session_lock = threading.Condition(threading.RLock())
        self._session_active = 0

    @contextmanager
    def acquire(self, cancel_event: threading.Event) -> Generator[None]:
        self._acquire_session(cancel_event)
        try:
            self._acquire_global(cancel_event)
        except BaseException:
            self._release_session()
            raise
        try:
            yield
        finally:
            with self._global_lock:
                type(self)._global_active = max(0, type(self)._global_active - 1)
                self._global_lock.notify_all()
            self._release_session()

    def _acquire_session(self, cancel_event: threading.Event) -> None:
        with self._session_lock:
            while self._session_active >= self.session_limit:
                if cancel_event.is_set():
                    raise fail("SANDBOX_CANCELLED", "Sandbox execution was cancelled.")
                self._session_lock.wait(timeout=0.1)
            self._session_active += 1

    def _release_session(self) -> None:
        with self._session_lock:
            self._session_active = max(0, self._session_active - 1)
            self._session_lock.notify_all()

    def _acquire_global(self, cancel_event: threading.Event) -> None:
        deadline = time.monotonic() + 1800
        with self._global_lock:
            while type(self)._global_active >= self.global_limit:
                if cancel_event.is_set():
                    raise fail("SANDBOX_CANCELLED", "Sandbox execution was cancelled.")
                if time.monotonic() >= deadline:
                    raise fail(
                        "SANDBOX_RESOURCE_CAPACITY",
                        "Timed out waiting for sandbox execution capacity.",
                    )
                self._global_lock.wait(timeout=0.1)
            type(self)._global_active += 1


def workspace_size(root: Path) -> int:
    """Measure workspace files without following links or accounting for Git metadata."""
    total = 0
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name != ".git"]
        for name in files:
            path = Path(current) / name
            with suppress(OSError):
                if not path.is_symlink():
                    total += path.stat().st_size
    return total


def _integer(value: str) -> int:
    try:
        return int(value.strip())
    except ValueError:
        return 0


def _float(value: str) -> float:
    try:
        return float(value.strip())
    except ValueError:
        return 0
