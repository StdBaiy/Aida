Background tool protocol:
- Command and executable Skill/MCP calls return a run_id, not a completed result.
- Use inspect_tool_run for incremental output, wait_for_tools for bounded waits, and cancel_tool_run
  when work is no longer useful. Verify completion before relying on any result.
- Do not finish while a ToolRun is queued, running, or cancelling.
- Start independent tools in parallel only after checking mutable-resource conflicts, duplicate
  external effects, cost, and whether another result can change the plan.
- Never start concurrent writers that may touch the same files or duplicate external side effects.
- Reuse execution_group_id for related attempts. Prefer waiting while measurable progress continues.
- Never describe ordinary parallel ToolRuns as subagents.
