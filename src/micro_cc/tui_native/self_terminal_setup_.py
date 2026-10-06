import asyncio
from micro_cc.utils.terminal_setup import ensure_terminal_setup

async def _background_terminal_setup_check(app):
    """Once per terminal (cached in ~/.micro-cc/terminal_setup.json): set up Shift+Enter or post a tip."""
    loop = asyncio.get_event_loop()
    try:
        message = await loop.run_in_executor(None, ensure_terminal_setup)
    except Exception:
        return
    if message:
        await app._mount_row({"type": "text", "content": message})
