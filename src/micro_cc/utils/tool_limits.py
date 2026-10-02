"""Per-tool result-size caps — independent of DEFAULT_TRIM_BUDGET (the
transcript trim budget in models/registry.py). A tool result over its cap
gets persisted to /tmp and the model receives a preview + path instead of
the full text, rather than being silently truncated.

Implements a three-layer scheme: a per-tool cap, a global clamp regardless
of what a tool declares, and a per-message aggregate cap for parallel tool
calls (asyncio.gather in claude_loop_.py can return several large results
in one turn — each may pass its own cap individually while still collectively
blowing the budget).
"""

# Global ceiling regardless of what a tool below declares — a new or
# misconfigured tool can't claim the whole trim budget.
DEFAULT_TOOL_RESULT_CAP = 50_000

# Per-tool overrides (chars) for known-noisy tools. Tools absent from this
# map fall back to DEFAULT_TOOL_RESULT_CAP.
TOOL_RESULT_CAPS = {
    "bash_": 30_000,
    "grep_": 20_000,
}

# Self-bounded tools — never persist. read_ already caps itself via its own
# line/byte params; persisting its output would create a circular
# read_ -> file -> read_ loop.
UNCAPPED_TOOLS = {"read_"}

# Aggregate cap across all tool_result blocks in a single turn.
MAX_TOOL_RESULTS_PER_MESSAGE = 120_000

PREVIEW_CHARS = 2000


def cap_for(tool_name: str) -> float:
    if tool_name in UNCAPPED_TOOLS:
        return float("inf")
    return min(TOOL_RESULT_CAPS.get(tool_name, DEFAULT_TOOL_RESULT_CAP), DEFAULT_TOOL_RESULT_CAP)
