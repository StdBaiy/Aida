"""Content-addressed private trace artifact storage."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class LocalArtifactStore:
    """Persist immutable trace payloads below the private Git data directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def put(self, content: bytes) -> tuple[str, str]:
        """Atomically store bytes and return their ID and relative path."""
        digest = hashlib.sha256(content).hexdigest()
        relative = Path(digest[:2]) / digest
        target = self.root / relative
        target.parent.mkdir(mode=0o700, exist_ok=True)
        os.chmod(target.parent, 0o700)
        if not target.exists():
            fd, temporary_name = tempfile.mkstemp(prefix=".artifact-", dir=target.parent)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "wb") as file:
                    file.write(content)
                    file.flush()
                    os.fsync(file.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        os.chmod(target, 0o600)
        return digest, relative.as_posix()

    def path(self, relative_path: str) -> Path:
        """Resolve a persisted artifact path."""
        return self.root / relative_path
