"""Process-level repository lock."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from types import TracebackType

from coding_agent.errors import fail


class RepositoryLock:
    """Hold an exclusive non-blocking lock for one repository."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> RepositoryLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self._fd)
            self._fd = None
            raise fail(
                "REPOSITORY_LOCKED",
                "Another coding-agent process owns this repository.",
            ) from exc
        os.ftruncate(self._fd, 0)
        os.write(self._fd, f"pid={os.getpid()}\n".encode())
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None
