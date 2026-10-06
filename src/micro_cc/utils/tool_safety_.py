"""Read-only tool classification for safe parallel/idempotent execution."""

READ_ONLY_TOOLS = {
    "read_", "glob_", "grep_",
    "read_skill", "list_skills", "list_mcps",
}


def read_only_by_input(name: str, tool_input: dict) -> bool:
    """Check if tool is read-only based on specific action parameter."""
    if name == "memory_":
        return tool_input.get("action") in ("get", "list")
    if name == "search_tools":
        return tool_input.get("action") == "discover"
    return False


def is_read_only(name: str, tool_input: dict) -> bool:
    return name in READ_ONLY_TOOLS or read_only_by_input(name, tool_input)
