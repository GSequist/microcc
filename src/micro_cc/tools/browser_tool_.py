import os
import time

_playwright = None
_browser = None
_page = None
_console_log: list[str] = []


def _on_console(msg):
    level = msg.type  # log, warning, error, info, debug
    text = msg.text
    _console_log.append(f"[{level}] {text}")


async def _ensure_browser():
    global _playwright, _browser, _page
    if _page is not None and (_page.is_closed() or not _browser.is_connected()):
        # The user closed the browser window/tab out-of-band — Playwright's
        # own objects don't reset themselves when that happens, so `_page`
        # stays a truthy (but dead) reference forever and the `if not
        # _page:` check below never re-fires. Every subsequent call was
        # hitting that same dead target: TargetClosedError on every call,
        # with no way back short of /clear (the only thing that ever called
        # close_browser()). Tear down and fall through to relaunch fresh,
        # same as the very first call. Best-effort — close_browser() already
        # swallows errors from an already-dead process.
        await close_browser()
    if not _page:
        from playwright.async_api import async_playwright
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(headless=False)
        _page = await _browser.new_page(viewport={"width": 1280, "height": 720})
        _page.on("console", _on_console)
    return _page


async def close_browser():
    """Tear down the persistent browser session.

    Best-effort: if the browser process already died (crashed, killed,
    its pipe to Playwright's driver closed from the other end), close()/
    stop() raise instead of no-op'ing, which would otherwise take down
    whatever awaited us (e.g. /clear) with an unhandled exception."""
    global _playwright, _browser, _page
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
    """Execute Playwright Python code against a persistent browser session.

    A browser with a single page is lazily initialized on first call and persists
    across calls. `page` is pre-initialized in scope — just write Playwright code.

    Every execution auto-captures a screenshot which is analyzed via vision.

    Args:
        code: Playwright Python async code. `page` is available.
              Examples:
                await page.goto("https://example.com")
                await page.click("button#submit")
                await page.fill("input[name=q]", "hello")
    """
    page = await _ensure_browser()

    # Screenshot dir
    ss_dir = os.path.join(project_dir, ".browser_screenshots")
    os.makedirs(ss_dir, exist_ok=True)

    # Execute the code with page in scope
    exec_output = ""
    try:
        # Capture any return value from last expression
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

    # Annotate screenshot with interactive element bounding boxes
    from micro_cc.utils.annotate_ import annotate_screenshot
    elements = await _get_browser_elements(page, screenshot_path)
    element_index = annotate_screenshot(screenshot_path, elements)

    # Vision the annotated screenshot
    from micro_cc.tools.vision_tools_ import vision
    prompt = "Describe this browser page. Numbered red boxes mark interactive elements."
    description = await vision(
        screenshot_path,
        prompt,
        project_dir=project_dir,
        model=model,
    )

    # Drain console log
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


async def _get_browser_elements(page, screenshot_path: str) -> list[dict]:
    """Extract interactive elements from the page with bounding rects, scaled for Retina."""
    try:
        from PIL import Image

        # Determine scale factor: screenshot pixels vs viewport
        img = Image.open(screenshot_path)
        img_w, _ = img.size
        viewport = page.viewport_size
        scale = img_w / viewport["width"] if viewport else 1.0

        raw = await page.evaluate("""() => {
            const sels = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="tab"], [role="checkbox"], [role="menuitem"], [onclick]';
            const els = [...document.querySelectorAll(sels)];
            return els.map(el => {
                const r = el.getBoundingClientRect();
                const label = el.textContent?.trim()?.slice(0, 80)
                    || el.getAttribute('aria-label')
                    || el.getAttribute('placeholder')
                    || el.getAttribute('title')
                    || '';
                const tag = el.tagName.toLowerCase()
                    + (el.type ? '[' + el.type + ']' : '')
                    + (el.getAttribute('role') ? '[role=' + el.getAttribute('role') + ']' : '');
                return { x: r.x, y: r.y, width: r.width, height: r.height, label, tag };
            });
        }""")

        # Filter zero-size / offscreen, cap at 50
        elements = []
        for el in raw:
            if el["width"] < 2 or el["height"] < 2:
                continue
            if el["x"] + el["width"] < 0 or el["y"] + el["height"] < 0:
                continue
            # Store viewport-coord click target (for page.mouse.click)
            el["click_x"] = el["x"] + el["width"] / 2
            el["click_y"] = el["y"] + el["height"] / 2
            # Scale to screenshot pixels for box drawing
            el["x"] = el["x"] * scale
            el["y"] = el["y"] * scale
            el["width"] = el["width"] * scale
            el["height"] = el["height"] * scale
            elements.append(el)
            if len(elements) >= 50:
                break

        # Assign indices
        for i, el in enumerate(elements, 1):
            el["index"] = i

        return elements
    except Exception:
        return []
