import pytest

from coding_agent.errors import CodingAgentError
from coding_agent.execution.policy import CommandPolicy
from coding_agent.models import CommandRequest


@pytest.mark.parametrize(
    "argv",
    [
        ["bash", "-c", "echo unsafe"],
        ["curl", "https://example.com"],
        ["git", "push"],
        ["python", "-c", "print(1)"],
    ],
)
def test_policy_denies_high_risk_commands(argv: list[str]) -> None:
    with pytest.raises(CodingAgentError):
        CommandPolicy().validate(CommandRequest(argv=argv))


def test_policy_allows_structured_test_command() -> None:
    CommandPolicy().validate(CommandRequest(argv=["pytest", "-q"]))
    CommandPolicy().validate(CommandRequest(argv=["python3", "-m", "pytest", "-q"]))
