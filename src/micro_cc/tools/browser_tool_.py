import asyncio
import base64
import functools
import os
import platform
import subprocess
import sys

_playwright = None
_browser = None
_page = None
_mode = ""  # which browser we're driving, shown to the model
_console_log: list[str] = []
_CDP_PORT = 9222
_SNAPSHOT_CAP = 40_000
_WIN_CHROME = [
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
]


def _on_console(msg):
    _console_log.append(f"[{msg.type}] {msg.text}")


def _adopt(page):
    """Make `page` the active tab; popups it opens (target=_blank) become active in turn."""
    global _page
    _page = page
    page.set_default_timeout(10_000)  # stale refs fail fast instead of hanging 30s
    page.on("console", _on_console)
    page.on("popup", _adopt)


async def _ensure_browser():
    global _playwright, _browser, _page, _mode
    if _page is not None and (_page.is_closed() or not _browser.is_connected()):
        # Window closed out-of-band: Playwright objects stay truthy, so tear down and relaunch.
        await close_browser()
    if not _page:
        from playwright.async_api import async_playwright
        _playwright = await async_playwright().start()
        _browser = await _attach_live_chrome()
        _mode = "the user's own Chrome (their logged-in profile)"
        if not _browser and _is_wsl():
            _browser = await _attach_windows_chrome()
            _mode = "Windows Chrome (micro-cc profile; logins persist)"
        if _browser:
            _adopt(await _browser.contexts[0].new_page())
        else:
            _browser = await _launch_chromium()
            _mode = "Playwright Chromium (fresh profile, not logged in)"
            _adopt(await _browser.new_page(viewport={"width": 1280, "height": 720}))
    return _page


def _is_wsl() -> bool:
    return "microsoft" in platform.release().lower()


@functools.cache
def _win_localappdata() -> str:
    """WSL: Windows %LOCALAPPDATA% (e.g. C:\\Users\\x\\AppData\\Local), '' if cmd.exe fails."""
    try:
        return subprocess.run(["cmd.exe", "/c", "echo %LOCALAPPDATA%"], capture_output=True,
                              text=True, timeout=10, cwd="/mnt/c").stdout.strip()
    except Exception:
        return ""


def _chrome_user_data_dir() -> str:
    """Default Chrome profile root for this platform, as a local path."""
    if platform.system() == "Darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    if _is_wsl():
        local = _win_localappdata()
        if not local:
            return ""
        try:
            return subprocess.run(["wslpath", "-u", f"{local}\\Google\\Chrome\\User Data"],
                                  capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return ""
    return os.path.expanduser("~/.config/google-chrome")


async def _attach_live_chrome():
    """Attach to the user's running Chrome if they enabled remote debugging (chrome://inspect/#remote-debugging)."""
    try:
        with open(os.path.join(_chrome_user_data_dir(), "DevToolsActivePort")) as f:
            port, path = [line.strip() for line in f.read().splitlines()[:2]]
    except (OSError, ValueError):
        return None
    try:
        # Generous timeout: Chrome may ask the user to allow the connection.
        return await _playwright.chromium.connect_over_cdp(f"ws://127.0.0.1:{port}{path}", timeout=30_000)
    except Exception:
        return None  # stale file (Chrome closed), denied, or WSL NAT can't reach Windows localhost


async def _attach_windows_chrome():
    """WSL: start Windows Chrome with a persistent micro-cc profile and attach over CDP; None if unreachable."""
    exe = next((p for p in _WIN_CHROME if os.path.exists(p)), None)
    if not exe:
        return None
    url = f"http://localhost:{_CDP_PORT}"
    try:
        return await _playwright.chromium.connect_over_cdp(url, timeout=1500)
    except Exception:
        pass  # not running yet
    local = _win_localappdata()
    if not local:
        return None
    try:
        subprocess.Popen([exe, f"--remote-debugging-port={_CDP_PORT}", f"--user-data-dir={local}\\micro-cc-chrome",
                          "--no-first-run", "--no-default-browser-check", "about:blank"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        return None
    for _ in range(20):
        await asyncio.sleep(0.5)
        try:
            return await _playwright.chromium.connect_over_cdp(url, timeout=1500)
        except Exception:
            continue
    return None  # NAT-mode WSL can't reach Windows localhost; fall back to Linux Chromium


async def _launch_chromium():
    """Launch Playwright Chromium, downloading it on first use; headless when Linux has no display."""
    headless = platform.system() == "Linux" and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    try:
        return await _playwright.chromium.launch(headless=headless)
    except Exception as e:
        if "Executable doesn't exist" not in str(e):
            raise
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "playwright", "install", "chromium",
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"playwright install chromium failed: {err.decode()[-500:]}")
    return await _playwright.chromium.launch(headless=headless)


async def close_browser():
    """Tear down the browser session; errors from an already-dead browser are swallowed."""
    global _playwright, _browser, _page
    if _page:
        try:
            await _page.close()  # CDP-attached Chrome keeps running; drop only our tab
        except Exception:
            pass
    if _browser:
        try:
            await _browser.close()
        except Exception:
            pass
    if _playwright:
        try:
            await _playwright.stop()
        except Exception:
            pass
    _playwright = _browser = _page = None


async def browser(code: str, description: str, *, project_dir: str, model: str, screenshot: bool = False) -> str | dict:
    """Run Playwright async Python in a persistent browser tab, then return the page as an accessibility snapshot.

    The snapshot lists elements like `- button "Save" [ref=e12]`. Act by ref with `ref("e12")`, a Playwright
    Locator that auto-waits and scrolls: `await ref("e12").click()`, `await ref("e7").fill("text")`.
    Refs are valid only from the latest snapshot. Use empty code to just look. Read exact data with
    page.evaluate / inner_text. Never click by pixel coordinates.

    Args:
        code: Playwright async Python with `page` and `ref` in scope; return a value to see it as output
        description: What this does, in plain words for a non-technical user who may have to approve it (5-12 words, e.g. "Open the bank's login page")
        screenshot: also attach a viewport image; only when layout, images or canvas matter
    """
    try:
        page = await _ensure_browser()
    except Exception as e:
        await close_browser()
        hint = ""
        if "missing dependencies" in str(e).lower():
            hint = f"\nFix: sudo {sys.executable} -m playwright install-deps chromium"
        return f"[Browser unavailable: {type(e).__name__}: {str(e)[:800]}]{hint}"

    exec_output = ""
    if code.strip():
        try:
            local_ns = {"page": page, "ref": lambda r: page.locator(f"aria-ref={r}")}
            exec(
                "async def __browser_exec__():\n"
                + "".join(f"    {line}\n" for line in code.strip().splitlines()),
                local_ns,
            )
            result = await local_ns["__browser_exec__"]()
            if result is not None:
                exec_output = str(result)
        except Exception as e:
            exec_output = f"Execution error: {type(e).__name__}: {e}"

    page = _page  # a popup may have taken over
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    try:
        snap = await page.aria_snapshot(mode="ai", timeout=10_000)
    except Exception as e:
        snap = f"[Snapshot failed: {type(e).__name__}: {str(e)[:300]}]"
    if len(snap) > _SNAPSHOT_CAP:
        snap = (snap[:_SNAPSHOT_CAP] + "\n[... snapshot truncated: for one region, "
                "`return await page.locator(selector).aria_snapshot(mode='ai')` (its refs replace these)]")

    console_lines = _console_log.copy()
    _console_log.clear()

    parts = []
    if exec_output:
        parts.append(f"Output: {exec_output}")
    if console_lines:
        parts.append(f"Console ({len(console_lines)} messages):\n" + "\n".join(console_lines[-50:]))
    try:
        title = await page.title()
    except Exception:
        title = ""
    parts.append(f"Tab: {title} — {page.url}\nBrowser: {_mode}")
    parts.append(f"Snapshot:\n{snap}")
    text = "\n".join(parts)

    if not screenshot:
        return text
    try:
        img = await page.screenshot(type="jpeg", quality=70, scale="css")
    except Exception as e:
        return f"{text}\n[Screenshot failed: {e}]"
    return {"type": "image", "data": base64.b64encode(img).decode(), "text": text}
