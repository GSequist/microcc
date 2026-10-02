from typing import List, Dict, Optional
from micro_cc.cache import state_store

VALID_STATUS = {"pending", "in_progress", "completed", "failed"}
_TODO_ICON = {"pending": "⏳", "in_progress": "🔄", "completed": "✅", "failed": "❌"}


def _format_todo_list(store: Dict[str, Dict], preview: int = 200) -> str:
    """Full task-list reminder, wrapped for the model's benefit. Baked directly
    into this tool's own return string so it rides inside its own tool_result
    — no separate message/block, so it can't create the stray-text-after-
    tool_result shape that confuses the model into premature stops."""
    if not store:
        return ""
    done = sum(1 for t in store.values() if t.get("status") == "completed")
    lines = []
    for tid, t in store.items():
        status = t.get("status", "pending")
        tag = f" [{t['tag']}]" if t.get("tag") else ""
        head = f"{_TODO_ICON.get(status, '?')} ({tid}) {t.get('content', '')}{tag}"
        notes = t.get("notes") or ""
        if notes:
            if status == "in_progress":
                head += f"\n    notes: {notes}"  # full, active item
            else:
                clip = notes[:preview] + ("…" if len(notes) > preview else "")
                head += f"\n    notes: {clip}"  # preview only
        lines.append(head)
    return (
        "<system-reminder>\n"
        f"Your task list ({done}/{len(store)} done). Keep exactly one item "
        "in_progress; mark completed/failed when done. Notes below are "
        'previewed — call todo_tool_ with get=["<id>"] (or get=["*"]) to read '
        "any item's full notes.\n"
        + "\n".join(lines)
        + "\n</system-reminder>"
    )


async def todo_tool_(
    todos: Optional[Dict[str, Dict]] = None,
    remove: Optional[List[str]] = None,
    get: Optional[List[str]] = None,
    *,
    project_dir,
    model,
) -> str:
    """
    Track multi-step work. Keyed by id — you only send the ids/fields you CHANGE;
    everything else is preserved. This is a MERGE, not a replace.

    Use `notes` as your per-task scratchpad — accumulate findings, SKUs, prices,
    rejected options. It persists across steps so you don't lose your own research.

    WRITE/EDIT — todos: {
      "1": {"content": "Search Greek cherry tomatoes < 2 EUR",
            "status": "in_progress", "activeForm": "Searching cherry tomatoes",
            "tag": "produce", "notes": "Found 3 SKUs: ...; rejected X because ..."},
      "2": {"content": "Review basket for gaps", "status": "pending"}
    }
      content     (str)  the task, imperative — set once
      status      (str)  pending | in_progress | completed | failed
      activeForm  (str)  present-continuous label shown in the UI while active
      notes       (str)  long-lived scratchpad, appended/edited over time
      tag         (str)  optional grouping label

    Flip one status:  todos={"1": {"status": "completed"}}   ← notes untouched.
    Edit notes:       todos={"3": {"notes": "updated text"}}  ← other fields kept.
    Every value MUST be an object, even for a single-field change.
      WRONG: todos={"3": "completed"}
      RIGHT: todos={"3": {"status": "completed"}}
    Delete:           remove=["1"].
    INSPECT in full:  get=["3"]  → returns id 3's full content+notes.
                      get=["*"]  → returns every todo in full.

    Keep exactly ONE item in_progress.
    """
    try:
        store: Dict[str, Dict] = state_store.get_todos(project_dir) or {}

        # ---- inspect path: read-only, return full detail to the model ----
        if get:
            ids = list(store.keys()) if get == ["*"] else [str(i) for i in get]
            blocks = []
            for tid in ids:
                t = store.get(tid)
                if not t:
                    blocks.append(f"({tid}) — not found")
                    continue
                blocks.append(
                    f"({tid}) [{t.get('status','?')}] {t.get('content','')}\n"
                    f"  notes: {t.get('notes','') or '(none)'}"
                )
            return "\n".join(blocks) or "No todos."

        # ---- write/merge path ----
        errors = []
        for tid, patch in (todos or {}).items():
            if not isinstance(patch, dict):
                errors.append(f"{tid}: value must be an object, got {type(patch).__name__} ({patch!r})")
                continue
            cur = store.get(str(tid), {})
            if "status" in patch and patch["status"] not in VALID_STATUS:
                errors.append(f"{tid}: bad status '{patch['status']}'")
                patch = {k: v for k, v in patch.items() if k != "status"}
            cur.update({k: v for k, v in patch.items() if v is not None})
            store[str(tid)] = cur

        for tid in (remove or []):
            store.pop(str(tid), None)

        in_prog = [k for k, t in store.items() if t.get("status") == "in_progress"]
        if len(in_prog) > 1:
            errors.append(f"{len(in_prog)} in_progress — keep one")

        state_store.set_todos(project_dir, store)

        done = sum(1 for t in store.values() if t.get("status") == "completed")
        msg = f"Todos: {done}/{len(store)} done"
        if errors:
            msg += "\nWARN: " + "; ".join(errors)
        reminder = _format_todo_list(store)
        if reminder:
            msg += "\n\n" + reminder
        # Single string back to the model (same contract as vision/file tools).
        # The store is persisted to state_store above; the UI (start_live
        # _tick_working) reads it straight from there — nothing downstream
        # needs the dict.
        return msg

    except Exception as e:
        # Returned string is already the tool_result the UI renders in the
        # tool_call row — no separate print() needed (and none wanted: a
        # bare print() inside a TUI's alt screen lands wherever the cursor
        # happens to be, not anywhere the user would see it as an error).
        return f"Error in todo_tool_: {e}"
