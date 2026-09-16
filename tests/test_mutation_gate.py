import threading

from coding_agent.errors import CodingAgentError
from coding_agent.workspace.mutation import WorkspaceMutationGate


def test_mutation_gate_is_reentrant_for_one_logical_turn() -> None:
    gate = WorkspaceMutationGate()

    gate.acquire("thread-a")
    gate.acquire("thread-a")
    gate.release("thread-a")

    gate.acquire("thread-b")
    gate.release("thread-b")


def test_mutation_gate_wait_can_be_cancelled() -> None:
    gate = WorkspaceMutationGate()
    cancelled = threading.Event()
    errors: list[CodingAgentError] = []
    gate.acquire("thread-a")

    def wait_for_gate() -> None:
        try:
            gate.acquire("thread-b", cancelled)
        except CodingAgentError as exc:
            errors.append(exc)

    worker = threading.Thread(target=wait_for_gate)
    worker.start()
    cancelled.set()
    worker.join(timeout=2)
    gate.release("thread-a")

    assert not worker.is_alive()
    assert [error.code for error in errors] == ["OPERATION_CANCELLED"]
