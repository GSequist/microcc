from typing import Literal, Optional

from micro_cc.utils.session_ipc_ import send_message, list_peers
from micro_cc.utils.inbox_store_ import write_mail


async def message_session_(
    action: Literal["list", "send"],
    target_project_dir: Optional[str] = None,
    message: Optional[str] = None,
    *,
    project_dir,
) -> str:
    """
    Talk to another micro-cc session scoped to a project_dir — a live TUI in
    another terminal, or a headless/batch run (past, present, or future)
    against that same project_dir. Use this to hand off findings, coordinate
    with a sibling subagent, or report back to whoever spawned you.

    action="list" — the other live micro-cc sessions (TUIs) on this machine, as
        `project_dir (pid N)` lines. Reads a small registry on disk; contacts
        nobody. Call it when you need a target you don't already have.

    action="send" — deliver `message` to `target_project_dir`. Two paths, tried
        in order:
          1. Live: if a TUI session is open right now on target_project_dir, the
             message lands as an incoming turn in its conversation immediately —
             acted on right away if it's idle, or as soon as its current turn
             finishes if busy.
          2. Durable fallback: if nothing is live there, the message is queued
             in that project's inbox instead. It is NOT lost — it gets folded in
             automatically the next time anyone runs headless/TUI against that
             project_dir (at startup, and continuously at each tool-result
             checkpoint while it's running), even if that run starts well after
             this call returns.
        A reply arrives as its own incoming turn — don't follow up with
        monitor_, which is for headless subagents and PIDs only.

    Args:
        action: "list" or "send"
        target_project_dir: For "send" — absolute path of the target project,
            whatever it was launched with, e.g. `python start_.py /path/to/project`
            or `microcc-headless /path/to/project "..."`
        message: For "send" — what to tell that session
    """
    if action == "list":
        peers = list_peers(project_dir)
        if not peers:
            return "No other live micro-cc sessions."
        return "\n".join(f"{p['project_dir']} (pid {p['pid']})" for p in peers)

    if action == "send":
        if not target_project_dir or not message:
            return "Error: action='send' requires target_project_dir and message."
        result = await send_message(target_project_dir, project_dir, message)
        if result.startswith("delivered to session"):
            return result
        write_mail(target_project_dir, project_dir, message)
        return (
            f"{result} — queued in {target_project_dir}'s inbox instead, will be "
            "picked up next time that session runs or checks in"
        )

    return f"Error: unknown action {action!r} — use 'list' or 'send'."
