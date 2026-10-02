import os
import platform
import subprocess
import time


async def computer(code: str, *, project_dir: str, model: str) -> str:
    """Execute PyAutoGUI code to control the Mac desktop.

    `pyautogui` and `time` are pre-imported. Use for mouse, keyboard, and screen control.

    Every execution auto-captures a screenshot which is analyzed via vision.

    Args:
        code: Python code using pyautogui. Examples:
              pyautogui.click(500, 300)
              pyautogui.hotkey('cmd', 'space')
              pyautogui.write('hello', interval=0.05)
              pyautogui.moveTo(100, 200)
              pyautogui.scroll(-3)
    """
    # Element scoping (_get_ax_elements) is macOS Accessibility API, and the
    # screenshot step below shells out to `screencapture` — both mac-only.
    # search_tool_.py already keeps this tool out of the catalog on other
    # platforms; this is the defense-in-depth check for any other call path.
    if platform.system() != "Darwin":
        return "[Computer tool is macOS-only — not available on this platform]"

    # pyautogui pulls in AppKit/Quartz/objc — kept out of the eager import
    # chain (this module loads at app startup) and only paid the first time
    # computer_use actually runs.
    import pyautogui
    pyautogui.PAUSE = 0.3

    # Screenshot dir
    ss_dir = os.path.join(project_dir, ".computer_screenshots")
    os.makedirs(ss_dir, exist_ok=True)

    # Execute the code with pyautogui in scope
    exec_output = ""
    try:
        local_ns = {"pyautogui": pyautogui, "time": time}
        exec(
            f"async def __computer_exec__():\n"
            + "".join(f"    {line}\n" for line in code.strip().splitlines()),
            local_ns,
        )
        result = await local_ns["__computer_exec__"]()
        if result is not None:
            exec_output = str(result)
    except Exception as e:
        exec_output = f"Execution error: {type(e).__name__}: {e}"

    # Auto-screenshot via native screencapture (faster + better quality than pyautogui)
    screenshot_path = os.path.join(ss_dir, f"{int(time.time() * 1000)}.png")
    try:
        subprocess.run(
            ["screencapture", "-x", screenshot_path],
            timeout=5,
        )
    except Exception as e:
        return f"{exec_output}\n[Screenshot failed: {e}]".strip()

    # Annotate screenshot with interactive element bounding boxes
    from micro_cc.utils.annotate_ import annotate_screenshot
    elements = _get_ax_elements()
    element_index = annotate_screenshot(screenshot_path, elements)

    # Vision the annotated screenshot
    from micro_cc.tools.vision_tools_ import vision
    prompt = "Describe this Mac screen. Numbered red boxes mark interactive elements."
    description = await vision(
        screenshot_path,
        prompt,
        project_dir=project_dir,
        model=model,
    )

    parts = []
    if exec_output:
        parts.append(f"Output: {exec_output}")
    parts.append(f"Screenshot: {screenshot_path}")
    if element_index:
        parts.append(f"Interactive elements:\n{element_index}")
        parts.append("IMPORTANT: To click any element, use ONLY the coordinates from the list above. "
                      "Example: pyautogui.click(cx, cy). NEVER estimate coordinates visually — "
                      "vision cannot determine screen coordinates. The element list is authoritative.")
    parts.append(f"Screen: {description}")

    return "\n".join(parts)


# Pure structural containers — have actions but clicking them is meaningless
_SKIP_ROLES = {"AXWindow", "AXApplication", "AXScrollArea", "AXSplitGroup", "AXSplitter"}
# Window chrome subroles — always skip
_CHROME_SUBROLES = {"AXCloseButton", "AXMinimizeButton", "AXZoomButton", "AXFullScreenButton"}
# Text inputs don't have AXPress but are interactive
_INPUT_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}


def _get_ax_elements() -> list[dict]:
    """Extract ALL interactive elements from all visible apps via macOS Accessibility API.

    Traverses to depth 30 to reach web content inside Chrome/Electron (Outlook lives at depth 20+).
    Collects everything with any action, then ranks for the annotated screenshot (top 50 drawn).
    All elements stored in element_map for Claude to reference.
    """
    try:
        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue,
            AXUIElementCopyActionNames,
            AXValueGetValue,
            kAXValueCGPointType,
            kAXValueCGSizeType,
        )
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
        )
        from AppKit import NSScreen

        # Retina scale factor
        scale = NSScreen.mainScreen().backingScaleFactor()

        # Get PIDs of all apps with on-screen windows (deduped, ordered)
        win_list = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
        seen_pids = set()
        pids = []
        for w in win_list:
            pid = w.get("kCGWindowOwnerPID")
            if pid and pid not in seen_pids and w.get("kCGWindowLayer", 0) == 0:
                seen_pids.add(pid)
                pids.append(pid)

        candidates = []

        def _traverse(el, depth=0):
            if depth > 30 or len(candidates) >= 1000:
                return

            err, role = AXUIElementCopyAttributeValue(el, "AXRole", None)
            role = role if err == 0 else ""

            # Skip window chrome buttons
            err_sr, subrole = AXUIElementCopyAttributeValue(el, "AXSubrole", None)
            if err_sr == 0 and subrole in _CHROME_SUBROLES:
                return

            # Any element with any action, or text inputs
            err_a, actions = AXUIElementCopyActionNames(el, None)
            has_action = err_a == 0 and actions and len(actions) > 0
            is_input = role in _INPUT_ROLES

            if (has_action or is_input) and role not in _SKIP_ROLES:
                err_pos, pos_ref = AXUIElementCopyAttributeValue(el, "AXPosition", None)
                err_size, size_ref = AXUIElementCopyAttributeValue(el, "AXSize", None)
                if err_pos == 0 and err_size == 0:
                    ok_p, point = AXValueGetValue(pos_ref, kAXValueCGPointType, None)
                    ok_s, size = AXValueGetValue(size_ref, kAXValueCGSizeType, None)
                    if ok_p and ok_s:
                        x, y = point.x * scale, point.y * scale
                        w, h = size.width * scale, size.height * scale
                        aspect = max(w, h) / min(w, h) if min(w, h) > 0 else 999
                        if (w >= 4 and h >= 4 and x >= 0 and y >= 0
                                and h < 10000 and aspect < 30):
                            label = ""
                            for attr in ("AXTitle", "AXDescription", "AXValue", "AXHelp"):
                                err_l, val = AXUIElementCopyAttributeValue(el, attr, None)
                                if err_l == 0 and val and isinstance(val, str) and val.strip():
                                    label = val.strip()[:80]
                                    break
                            candidates.append({
                                "x": x, "y": y, "width": w, "height": h,
                                "click_x": point.x + size.width / 2,
                                "click_y": point.y + size.height / 2,
                                "tag": role, "label": label,
                                "_depth": depth,
                            })

            # Recurse into children
            err_c, children = AXUIElementCopyAttributeValue(el, "AXChildren", None)
            if err_c == 0 and children:
                for child in children:
                    if len(candidates) >= 1000:
                        break
                    _traverse(child, depth + 1)

        for pid in pids:
            if len(candidates) >= 1000:
                break
            try:
                app_ref = AXUIElementCreateApplication(pid)
                _traverse(app_ref)
            except Exception:
                continue

        # Rank: labeled > unlabeled, inputs high, deeper = content not chrome,
        #        "Close"/"Close Tab" deprioritized
        def _score(el):
            s = 0
            if el["label"]:
                s += 10
            if el["label"] in ("Close", "Close Tab"):
                s -= 20
            if el["tag"] in _INPUT_ROLES:
                s += 15
            s += min(el["_depth"], 10)
            return s

        candidates.sort(key=_score, reverse=True)

        # Dedup: if two elements have click centers within 8px, keep the higher-scored one
        deduped = []
        seen_coords = []  # list of (cx, cy)
        for el in candidates:
            cx = el.get("click_x", el["x"] + el["width"] / 2)
            cy = el.get("click_y", el["y"] + el["height"] / 2)
            too_close = False
            for sx, sy in seen_coords:
                if abs(cx - sx) < 8 and abs(cy - sy) < 8:
                    too_close = True
                    break
            if not too_close:
                seen_coords.append((cx, cy))
                deduped.append(el)

        for i, el in enumerate(deduped, 1):
            el.pop("_depth", None)
            el["index"] = i

        return deduped
    except Exception:
        return []
