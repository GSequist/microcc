from micro_cc.utils.msg_store_ import search_msgs


async def search_history_(query: str, limit: int = 10, project_dir: str = "") -> str:
    """Full-text search over this project's ENTIRE conversation history —
    every message ever exchanged here, including turns folded out of the
    live context by checkpoint compaction (see the recovery note attached
    to a compacted <conversation-summary>). The compaction summary is a
    paraphrase; this is the real thing — use it whenever you need an exact
    quote, error string, file path, code snippet, or tool output that
    might have been summarized away.

    Case-insensitive substring match over every message's flattened text
    (thinking/text/tool_use input/tool_result content — raw image bytes are
    skipped). Not a semantic search — search for words you expect appeared
    verbatim, not a paraphrase of the idea.

    Args:
        query: Substring to search for, case-insensitive.
        limit: Max matches to return (default 10, most recent first).
    """
    matches = search_msgs(project_dir, query, limit)
    if not matches:
        return f"No matches for '{query}' in this project's history."
    lines = [f"[#{m['index']}, {m['role']}] {m['snippet']}" for m in matches]
    return f"{len(matches)} match(es) for '{query}':\n" + "\n".join(lines)
