"""Loop event types mods may observe; claude_loop_ yields them, tests keep this in sync."""

# Coarse events only; per-token deltas excluded to keep streaming cheap.
AGENT_TYPES = ("tool_call", "tool_result", "final_text", "done", "error", "turn_boundary",
               "approval_request", "question_asked", "cache_invalidate")
