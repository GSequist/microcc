from PIL import Image, ImageDraw, ImageFont

_FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
_FONT_SIZE = 28
_BOX_COLOR = (255, 0, 0)
_BOX_WIDTH = 3
_BADGE_BG = (255, 0, 0, 230)
_BADGE_TEXT_COLOR = (255, 255, 255)
_MAX_DRAWN = 50  # max boxes on screenshot (readability)

# Stored element map — overwritten on each annotate_screenshot() call.
# Maps element number → {click_x, click_y, label, tag}
element_map: dict[int, dict] = {}


def _load_font(size=_FONT_SIZE):
    try:
        return ImageFont.truetype(_FONT_PATH, size)
    except Exception:
        return ImageFont.load_default()


def annotate_screenshot(img_path: str, elements: list[dict]) -> str:
    """Draw numbered bounding boxes on a screenshot and store ALL elements in element_map.

    - Top 50 elements get red boxes drawn on the screenshot
    - ALL elements stored in element_map with native click coordinates
    - ALL elements included in the returned text index

    Each element dict must have: x, y, width, height, label, tag
    Optional: click_x, click_y (native coords — if omitted, center of box used)
    """
    element_map.clear()

    if not elements:
        return ""

    # Store ALL elements in map
    for i, el in enumerate(elements, 1):
        idx = el.get("index", i)
        cx = el.get("click_x", el["x"] + el["width"] / 2)
        cy = el.get("click_y", el["y"] + el["height"] / 2)
        element_map[idx] = {
            "click_x": cx, "click_y": cy,
            "label": el.get("label", ""), "tag": el.get("tag", ""),
        }

    # Draw boxes on screenshot for top N only
    draw_elements = elements[:_MAX_DRAWN]

    img = Image.open(img_path).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _load_font()

    for el in draw_elements:
        idx = el.get("index", 0)
        x, y, w, h = el["x"], el["y"], el["width"], el["height"]

        draw.rectangle([x, y, x + w, y + h], outline=_BOX_COLOR, width=_BOX_WIDTH)

        badge_text = str(idx)
        bbox = font.getbbox(badge_text)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pad = 4
        bx, by = x, y - th - 2 * pad
        if by < 0:
            by = y
        draw.rectangle([bx, by, bx + tw + 2 * pad, by + th + 2 * pad], fill=_BADGE_BG)
        draw.text((bx + pad, by + pad - bbox[1]), badge_text, fill=_BADGE_TEXT_COLOR, font=font)

    img = Image.alpha_composite(img, overlay)
    img.convert("RGB").save(img_path)

    # Return text index for ALL elements
    return _format_index(elements)


def _format_index(elements: list[dict]) -> str:
    """Element index with native click coordinates ready to use."""
    lines = []
    for i, el in enumerate(elements, 1):
        idx = el.get("index", i)
        tag = el.get("tag", "element")
        label = el.get("label", "")
        cx = int(el.get("click_x", el["x"] + el["width"] / 2))
        cy = int(el.get("click_y", el["y"] + el["height"] / 2))
        label_part = f' "{label}"' if label else ""
        lines.append(f"[{idx}] {tag}{label_part} → click({cx}, {cy})")
    return "\n".join(lines)
