import asyncio
import os
import platform
import subprocess
import sys
import time

_playwright = None
_browser = None
_page = None
_console_log: list[str] = []
_CDP_PORT = 9222
_WIN_CHROME = [
    "/mnt/c/Program Files/Google/Chrome/Application/chrome.exe",
    "/mnt/c/Program Files (x86)/Google/Chrome/Application/chrome.exe",
]


def _on_console(msg):
    level = msg.type  # log, warning, error, info, debug
    text = msg.text
    _console_log.append(f"[{level}] {text}")


async def _ensure_browser():
    global _playwright, _browser, _page
    if _page is not None and (_page.is_closed() or not _browser.is_connected()):
        # Window closed out-of-band: Playwright objects stay truthy, so tear down and relaunch.
        await close_browser()
    if not _page:
        from playwright.async_api import async_playwright
        _playwright = await async_playwright().start()
        _browser = await _attach_windows_chrome() if _is_wsl() else None
        if _browser:
            _page = await _browser.contexts[0].new_page()
            await _page.set_viewport_size({"width": 1280, "height": 720})
        else:
            _browser = await _launch_chromium()
            _page = await _browser.new_page(viewport={"width": 1280, "height": 720})
        _page.on("console", _on_console)
    return _page


def _is_wsl() -> bool:
    return "microsoft" in platform.release().lower()


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
    try:
        local = subprocess.run(["cmd.exe", "/c", "echo %LOCALAPPDATA%"], capture_output=True,
                               text=True, timeout=10, cwd="/mnt/c").stdout.strip()
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


async def browser(code: str, *, project_dir: str, model: str) -> str:
    """Run Playwright async Python code against a persistent single-page browser (created on first call).

    Every run auto-captures a screenshot and describes it via vision.

    Args:
        code: Playwright async Python code, `page` in scope; return a value to see it as output
    """
    try:
        page = await _ensure_browser()
    except Exception as e:
        await close_browser()
        hint = ""
        if "missing dependencies" in str(e).lower():
            hint = f"\nFix: sudo {sys.executable} -m playwright install-deps chromium"
        return f"[Browser unavailable: {type(e).__name__}: {str(e)[:800]}]{hint}"

    ss_dir = os.path.join(project_dir, ".browser_screenshots")
    os.makedirs(ss_dir, exist_ok=True)

    exec_output = ""
    try:
        local_ns = {"page": page}
        exec(
            f"async def __browser_exec__():\n"
            + "".join(f"    {line}\n" for line in code.strip().splitlines()),
            local_ns,
        )
        result = await local_ns["__browser_exec__"]()
        if result is not None:
            exec_output = str(result)
    except Exception as e:
        exec_output = f"Execution error: {type(e).__name__}: {e}"

    # Auto-screenshot after every execution
    screenshot_path = os.path.join(ss_dir, f"{int(time.time() * 1000)}.png")
    try:
        await page.screenshot(path=screenshot_path)
    except Exception as e:
        return f"{exec_output}\n[Screenshot failed: {e}]".strip()

    from micro_cc.utils.annotate_ import annotate_screenshot
    elements = await _get_browser_elements(page, screenshot_path)
    element_index = annotate_screenshot(screenshot_path, elements)

    from micro_cc.tools.vision_tools_ import vision
    prompt = "Describe this browser page. Numbered coloured boxes mark interactive elements."
    description = await vision(
        screenshot_path,
        prompt,
        project_dir=project_dir,
        model=model,
    )

    console_lines = _console_log.copy()
    _console_log.clear()

    parts = []
    if exec_output:
        parts.append(f"Output: {exec_output}")
    if console_lines:
        parts.append(f"Console ({len(console_lines)} messages):\n" + "\n".join(console_lines[-50:]))
    parts.append(f"Screenshot: {screenshot_path}")
    if element_index:
        parts.append(f"Interactive elements:\n{element_index}")
        parts.append("IMPORTANT: To click any element, use ONLY the coordinates from the list above. "
                      "Example: await page.mouse.click(cx, cy). NEVER estimate coordinates visually — "
                      "vision cannot determine screen coordinates. The element list is authoritative.")
    parts.append(f"Page: {description}")

    return "\n".join(parts)


# In-viewport, visible, unoccluded interactive elements; box clipped to viewport, click at its visible center.
_ELEMENTS_JS = """() => {
    const sels = 'a[href], button, input:not([type=hidden]), select, textarea, summary, '
        + '[contenteditable=""], [contenteditable=true], [tabindex]:not([tabindex="-1"]), [onclick], '
        + '[role=button], [role=link], [role=tab], [role=checkbox], [role=radio], [role=switch], [role=menuitem], '
        + '[role=option], [role=combobox], [role=textbox], [role=searchbox], [role=slider], [role=treeitem]';
    const vw = innerWidth, vh = innerHeight, out = [];
    for (const el of document.querySelectorAll(sels)) {
        const r = el.getBoundingClientRect();
        const x0 = Math.max(r.left, 0), y0 = Math.max(r.top, 0);
        const x1 = Math.min(r.right, vw), y1 = Math.min(r.bottom, vh);
        if (x1 - x0 < 4 || y1 - y0 < 4) continue;
        const st = getComputedStyle(el);
        if (st.visibility !== 'visible' || +st.opacity === 0 || st.pointerEvents === 'none') continue;
        const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
        const hit = document.elementFromPoint(cx, cy);
        if (!hit || !(hit === el || el.contains(hit) || hit.contains(el) || (el.labels && [...el.labels].includes(hit)))) continue;
        const label = (el.getAttribute('aria-label') || el.innerText || el.labels?.[0]?.innerText || el.getAttribute('placeholder')
            || el.getAttribute('title') || el.getAttribute('alt') || el.querySelector('img[alt]')?.alt
            || (el.type === 'password' ? '' : el.value) || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
        const role = el.getAttribute('role');
        const tag = el.tagName.toLowerCase() + (el.type && el.tagName !== 'BUTTON' ? '[' + el.type + ']' : '') + (role ? '[role=' + role + ']' : '');
        out.push({x: x0, y: y0, width: x1 - x0, height: y1 - y0, click_x: cx, click_y: cy, label, tag});
    }
    return out;
}"""


async def _get_browser_elements(page, screenshot_path: str) -> list[dict]:
    """Extract interactive elements with bounding rects, scaled to screenshot pixels."""
    try:
        from PIL import Image

        # Screenshot pixels per viewport pixel (Retina).
        img = Image.open(screenshot_path)
        img_w, _ = img.size
        viewport = page.viewport_size
        scale = img_w / viewport["width"] if viewport else 1.0

        raw = await page.evaluate(_ELEMENTS_JS)

        # Viewport-coord click targets stay as-is (for page.mouse.click); boxes scale to screenshot pixels.
        elements = []
        for el in raw:
            el["x"] = el["x"] * scale
            el["y"] = el["y"] * scale
            el["width"] = el["width"] * scale
            el["height"] = el["height"] * scale
            elements.append(el)
            if len(elements) >= 50:
                break

        for i, el in enumerate(elements, 1):
            el["index"] = i

        return elements
    except Exception:
        return []
