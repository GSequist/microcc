from typing import Literal
from micro_cc.utils import memory_store_ as store


async def memory_(
    action: Literal["add", "edit", "get", "delete", "list"],
    key: str = "",
    description: str = "",
    content: str = "",
    scope: Literal["global", "project"] = "global",
    project_dir: str = "",
) -> str:
    """Manage long-term keyed memory. scope="global": all projects
    (~/.micro-cc/memory.json). scope="project": this project only.

    Args:
        action: add|edit|get|delete|list.
        key: short snake_case id, never shown alone. Required except list.
        description: full sentence — shown in the manifest every turn.
            Required for add.
        content: memory body. Required for add.
        scope: "global" (default) or "project".
    """
    pdir = project_dir if scope == "project" else None

    if action == "list":
        items = store.list_memories(project_dir=pdir)
        if not items:
            return f"Memory ({scope}) is empty."
        lines = [f"- {it['key']} — {it['description']}" for it in items]
        return f"Memory manifest ({scope}, {len(items)}/{store.MAX_KEYS}):\n" + "\n".join(lines)

    if action == "get":
        if not key:
            return "Error: 'key' required for action='get'."
        entry = store.get_memory(key, project_dir=pdir)
        if not entry:
            return f"No memory entry found for key='{key}' in scope='{scope}'."
        return f"[{entry['key']}] ({scope}) — {entry['description']}\n\n{entry['content']}"

    if action == "delete":
        if not key:
            return "Error: 'key' required for action='delete'."
        res = store.delete_memory(key, project_dir=pdir)
        if res["status"] == "missing":
            return f"No memory entry found for key='{key}' in scope='{scope}'."
        return f"Deleted memory entry '{key}' ({scope})."

    if action == "add":
        if not key or not description or not content:
            return "Error: action='add' requires key, description, and content."
        if len(description) < 30:
            return f"Description '{description}' is too short — write it as a full descriptive sentence, since that's what shows in the manifest."
        count = store.count_memories(project_dir=pdir)
        if count >= store.MAX_KEYS:
            return (
                f"Memory cap reached for scope='{scope}' ({count}/{store.MAX_KEYS}). "
                f"Consolidate or delete obsolete entries first. "
                f"Call memory_(action='list', scope='{scope}') to review."
            )
        res = store.add_memory(key, description, content, project_dir=pdir)
        if res["status"] == "exists":
            return f"Memory key '{key}' already exists in scope='{scope}'. Use action='edit' to update it."
        return f"Added memory entry '{key}' ({scope})."

    if action == "edit":
        if not key:
            return "Error: 'key' required for action='edit'."
        if not content and not description:
            return "Error: action='edit' requires at least one of content or description."
        res = store.edit_memory(key, new_content=content or None, new_description=description or None, project_dir=pdir)
        if res["status"] == "missing":
            return f"No memory entry for key='{key}' in scope='{scope}'. Use action='add' to create it."
        return f"Updated memory entry '{key}' ({scope})."

    return f"Error: unknown action '{action}'. Use add|edit|get|delete|list."
