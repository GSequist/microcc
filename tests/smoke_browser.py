"""CI smoke: computer refuses cleanly off-mac; browser launches and renders a page."""
import asyncio
import platform

from micro_cc.tools.browser_tool_ import _ensure_browser, close_browser
from micro_cc.tools.computer_tool_ import computer


async def main():
    if platform.system() != "Darwin":
        out = await computer("pass", project_dir=".", model="")
        assert "macOS-only" in out, out
        print("computer: refused cleanly")
    page = await _ensure_browser()
    await page.goto("data:text/html,<h1>ok</h1>")
    assert await page.inner_text("h1") == "ok"
    print("browser: launched and rendered")
    await close_browser()


asyncio.run(main())
