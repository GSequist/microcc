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
    Talk to another micro-cc session on a project_dir: a live TUI, or a headless run against it
    (past, present or future). Use it to hand off findings or report back.

    action="list": live micro-cc TUI sessions on this machine, as `project_dir (pid N)` lines.
    Reads a disk registry and contacts nobody.
    action="send": deliver `message` to `target_project_dir`. Tries live delivery first: the
    message lands as an incoming turn now, or after the current turn if busy. If no TUI is open
    there, it is queued in that project's inbox and folded in at the next headless or TUI
    startup or tool-result checkpoint.
    A reply arrives as its own incoming turn. Do not follow up with monitor_; it is for
    headless subagents and PIDs only.

    Args:
        action: "list" or "send"
        target_project_dir: For "send": absolute path of the target project.
        message: For "send": what to tell that session.
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
