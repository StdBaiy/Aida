"""Non-bypassable command deny rules for the unsandboxed MVP."""

from __future__ import annotations

from coding_agent.errors import fail
from coding_agent.models import CommandRequest

_DENIED_PROGRAMS = {
    "bash",
    "sh",
    "zsh",
    "fish",
    "curl",
    "wget",
    "ssh",
    "scp",
    "nc",
    "netcat",
    "sudo",
    "su",
    "docker",
    "kubectl",
    "terraform",
    "mysql",
    "psql",
    "redis-cli",
}
_DYNAMIC_FLAGS = {"-c", "-e", "--eval", "-m"}


class CommandPolicy:
    """Reject commands that are unsafe without process isolation."""

    def validate(self, request: CommandRequest) -> None:
        """Raise when a command cannot be approved in the host backend."""
        program = request.argv[0].rsplit("/", 1)[-1]
        if program in _DENIED_PROGRAMS:
            raise fail("COMMAND_DENIED", f"Command is forbidden without a sandbox: {program}")
        remote_git = program == "git" and any(
            arg in {"push", "fetch", "pull", "clone"} for arg in request.argv[1:]
        )
        if remote_git:
            raise fail("COMMAND_DENIED", "Remote Git operations are forbidden.")
        dynamic_execution = program in {"python", "python3", "node", "ruby", "perl"} and any(
            arg in _DYNAMIC_FLAGS for arg in request.argv[1:]
        )
        approved_test_module = program in {"python", "python3"} and request.argv[1:3] == [
            "-m",
            "pytest",
        ]
        if dynamic_execution and not approved_test_module:
            raise fail("COMMAND_DENIED", "Inline or module script execution is forbidden.")
        if any("\n" in arg or "\r" in arg or "\x00" in arg for arg in request.argv):
            raise fail("COMMAND_DENIED", "Command arguments contain control characters.")
