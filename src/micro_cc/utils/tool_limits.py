"""Per-tool result-size caps; over-cap results persist to /tmp."""

DEFAULT_TOOL_RESULT_CAP = 50_000  # Global ceiling for tools
TOOL_RESULT_CAPS = {  # Per-tool overrides for noisy tools
    "bash_": 30_000,
    "grep_": 20_000,
}
UNCAPPED_TOOLS = {"read_"}  # Self-bounded; never persist
MAX_TOOL_RESULTS_PER_MESSAGE = 120_000  # Aggregate cap per turn

PREVIEW_CHARS = 2000


def cap_for(tool_name: str) -> float:
    if tool_name in UNCAPPED_TOOLS:
        return float("inf")
    return min(TOOL_RESULT_CAPS.get(tool_name, DEFAULT_TOOL_RESULT_CAP), DEFAULT_TOOL_RESULT_CAP)
