"""Best-effort cleanup of provider resources left by crashed Host processes."""

from __future__ import annotations

from coding_agent.sandbox.provider import SandboxExecutionProvider
from coding_agent.sandbox.repository import SandboxExecutionRepository


class SandboxReconciler:
    """Delete managed resources whose creating Host instance is gone."""

    def __init__(
        self,
        provider: SandboxExecutionProvider,
        repository: SandboxExecutionRepository,
        *,
        host_instance_id: str,
    ) -> None:
        self.provider = provider
        self.repository = repository
        self.host_instance_id = host_instance_id

    def reconcile(self) -> int:
        health = self.provider.health()
        if not health.available:
            return 0
        removed = 0
        active = {
            str(row["resource_id"] or row["container_id"]): row
            for row in self.repository.active()
            if row.get("resource_id") or row.get("container_id")
        }
        for handle in self.provider.list_managed():
            if handle.host_instance_id == self.host_instance_id:
                continue
            try:
                self.provider.cancel(handle, 1)
                self.provider.remove(handle)
            except Exception:
                row = active.get(handle.resource_id)
                if row is not None:
                    self.repository.update(
                        str(row["execution_id"]),
                        "cleanup_pending",
                        cleanup_pending=True,
                    )
                continue
            row = active.get(handle.resource_id)
            if row is not None:
                self.repository.update(
                    str(row["execution_id"]),
                    "failed",
                    termination_reason="lost",
                    cleanup_pending=False,
                )
            removed += 1
        return removed
