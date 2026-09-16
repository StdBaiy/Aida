Executable Skills:
- If load_skill reports requires_activation=true and execution is needed, call activate_skill and
  wait for host approval. Then use run_skill_command with a declared command name or call_skill_mcp
  with an exact returned tool name. Inspect the background result before relying on it.
- Report external side effects; restoring workspace files cannot undo them.
