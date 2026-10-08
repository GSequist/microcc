from micro_cc.utils import self_reload_


def reload_harness_() -> str:
    """Restart micro-cc into your edits to the harness source or ~/.micro-cc/mods. Call it only after the user has allowed the restart. It happens when this turn ends; the conversation is kept, so finish your reply first."""
    if not self_reload_.is_live():
        return "Error: reload is only available in the interactive TUI."
    self_reload_.request_reload()
    return "Reload queued: micro-cc restarts into the changes when this turn ends. The conversation continues after it."
