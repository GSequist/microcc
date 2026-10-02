import asyncio
from micro_cc.utils.terminal_setup import ensure_terminal_setup

async def _background_terminal_setup_check(app):
    """Runs once per terminal (state cached in ~/.micro-cc/terminal_setup.json)
    — installs Shift+Enter keybindings on terminals that need them, or
    surfaces a Ctrl+J/Alt+Enter tip where that's not possible. See
    micro_cc.utils.terminal_setup. Posted as a chat row rather than the
    statusbar — the statusbar is too narrow to show the full tip."""
    loop = asyncio.get_event_loop()
    try:
        message = await loop.run_in_executor(None, ensure_terminal_setup)
    except Exception:
        return
    if message:
        await app._mount_row({"type": "text", "content": message})
