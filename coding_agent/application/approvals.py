"""Bridge synchronous runtime approvals to REST decisions."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any

from coding_agent.application.events import EventJournal
from coding_agent.errors import fail
from coding_agent.models import utc_now
from coding_agent.repository import new_id


@dataclass
class _Pending:
    event: threading.Event
    decision: bool | None = None


class ApprovalBroker:
    """Persist exact action requests and block only the workspace worker."""

    def __init__(self, journal: EventJournal) -> None:
        self.journal = journal
        self._pending: dict[str, _Pending] = {}
        self._lock = threading.RLock()

    def request(
        self,
        operation_id: str,
        request: dict[str, Any],
        cancelled: threading.Event | None = None,
    ) -> bool:
        canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
        request_hash = hashlib.sha256(canonical.encode()).hexdigest()
        approval_id = new_id()
        pending = _Pending(threading.Event())
        with self._lock, self.journal._lock, self.journal.connection:
            self._pending[approval_id] = pending
            self.journal.connection.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, 'pending', NULL, ?, NULL)",
                (
                    approval_id,
                    operation_id,
                    request_hash,
                    canonical,
                    utc_now().isoformat(),
                ),
            )
        self.journal.set_status(operation_id, "waiting_approval")
        self.journal.append(
            operation_id,
            "approval.required",
            {
                "approval_id": approval_id,
                "request_hash": request_hash,
                "request": request,
            },
        )
        while not pending.event.wait(timeout=0.1):
            if cancelled is not None and cancelled.is_set():
                self._cancel_pending(approval_id, pending)
                raise fail("OPERATION_CANCELLED", "The Agent operation was cancelled.")
        if cancelled is not None and cancelled.is_set():
            raise fail("OPERATION_CANCELLED", "The Agent operation was cancelled.")
        with self._lock, self.journal._lock:
            self._pending.pop(approval_id, None)
        self.journal.set_status(operation_id, "running")
        return bool(pending.decision)

    def cancel_operation(self, operation_id: str) -> None:
        """Wake pending approvals owned by a cancelled operation."""
        with self._lock, self.journal._lock:
            rows = self.journal.connection.execute(
                "SELECT approval_id FROM approvals "
                "WHERE operation_id = ? AND status = 'pending'",
                (operation_id,),
            ).fetchall()
            for row in rows:
                approval_id = str(row["approval_id"])
                pending = self._pending.get(approval_id)
                if pending is not None:
                    self._cancel_pending(approval_id, pending)

    def _cancel_pending(self, approval_id: str, pending: _Pending) -> None:
        with self._lock, self.journal.connection:
            self.journal.connection.execute(
                """
                UPDATE approvals
                SET status = 'cancelled', decision = 'cancel', decided_at = ?
                WHERE approval_id = ? AND status = 'pending'
                """,
                (utc_now().isoformat(), approval_id),
            )
            self._pending.pop(approval_id, None)
            pending.event.set()

    def resolve(
        self,
        approval_id: str,
        *,
        operation_id: str,
        request_hash: str,
        decision: str,
    ) -> dict[str, Any]:
        with self._lock:
            row = self.journal.connection.execute(
                "SELECT * FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise fail("APPROVAL_NOT_FOUND", "Approval request no longer exists.")
            if row["operation_id"] != operation_id or row["request_hash"] != request_hash:
                raise fail("APPROVAL_MISMATCH", "Approval does not match this exact action.")
            if row["status"] == "resolved":
                return {"decision": row["decision"], "replayed": True}
            pending = self._pending.get(approval_id)
            if pending is None:
                raise fail("APPROVAL_EXPIRED", "The operation is no longer waiting.")
            approved = decision == "approve"
            pending.decision = approved
            with self.journal.connection:
                self.journal.connection.execute(
                    """
                    UPDATE approvals
                    SET status = 'resolved', decision = ?, decided_at = ?
                    WHERE approval_id = ?
                    """,
                    (decision, utc_now().isoformat(), approval_id),
                )
            pending.event.set()
        self.journal.append(
            operation_id,
            "approval.resolved",
            {"approval_id": approval_id, "decision": decision},
        )
        return {"decision": decision, "replayed": False}
