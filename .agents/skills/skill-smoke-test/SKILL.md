---
name: skill-smoke-test
description: Verifies local Skill script and MCP execution. Invoke when testing whether the coding agent can activate a Skill, run its bundled script, or call its local MCP tools.
---

# Skill Smoke Test

Use this Skill only for validating the Skill execution pipeline.

## Script Check

1. Call `activate_skill` with `name="skill-smoke-test"` and wait for approval.
2. Call `run_skill_command` with:
   - `skill_name`: `skill-smoke-test`
   - `command_name`: `echo`
   - `extra_args`: a short test message
3. Confirm the returned JSON has `ok: true`, `kind: "skill-script"`, and the same arguments.

## MCP Check

The local smoke MCP server must already be running.

1. Call `activate_skill` with `name="skill-smoke-test"` and wait for approval.
2. Find `smoke_echo` and `smoke_add` in the returned `mcp_tools`.
3. Call `call_skill_mcp` with the exact returned tool name and arguments.
4. Report the script and MCP results separately.

Do not replace these checks with `run_command`; the purpose is to exercise the Skill-specific
execution tools.
