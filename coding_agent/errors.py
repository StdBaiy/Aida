"""Stable application errors."""


class CodingAgentError(Exception):
    """An error safe to render in the CLI."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.user_message = message
        self.retryable = retryable


def fail(code: str, message: str) -> CodingAgentError:
    """Build a non-retryable application error."""
    return CodingAgentError(code, message)
