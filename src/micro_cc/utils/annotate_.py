from PIL import Image, ImageDraw, ImageFont

# Bold sans first: mac, WSL (Windows fonts), Debian/Ubuntu, Fedora; Pillow's scalable default last.
_FONT_PATHS = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/mnt/c/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
]
# Dark, mutually distinct hues: white text stays legible and neighbours never share a colour.
_PALETTE = [
    (230, 25, 75), (0, 110, 200), (20, 140, 60), (220, 100, 0), (145, 30, 180),
    (0, 128, 128), (200, 0, 160), (128, 0, 0), (0, 0, 140), (120, 80, 20),
]
_MAX_DRAWN = 50  # max boxes on screenshot (readability)

# Element map: {number: {click_x, click_y, label, tag}}, reset on each call.
element_map: dict[int, dict] = {}
_fonts: dict[int, ImageFont.FreeTypeFont] = {}


def _load_font(size: int):
    if size not in _fonts:
        for path in _FONT_PATHS:
            try:
                _fonts[size] = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
        else:
            _fonts[size] = ImageFont.load_default(size=size)
    return _fonts[size]


def _overlap(a, b) -> float:
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))


def _place_badge(box, bw, bh, img_w, img_h, badges, boxes):
    """Badge slot around `box` covering the least of other badges (heavily) and other boxes' content."""
    x0, y0, x1, y1 = box
    slots = [(x0, y0 - bh), (x1 - bw, y0 - bh), (x0 - bw, y0), (x1, y0),
             (x0, y1), (x1 - bw, y1), (x0 - bw, y1 - bh), (x1, y1 - bh), (x0, y0)]
    best, best_cost = None, None
    for rank, (sx, sy) in enumerate(slots):
        sx = min(max(sx, 0), img_w - bw)
        sy = min(max(sy, 0), img_h - bh)
        rect = (sx, sy, sx + bw, sy + bh)
        cost = (sum(_overlap(rect, b) for b in badges) * 10
                + sum(_overlap(rect, b) for b in boxes if b is not box) + rank)
        if best_cost is None or cost < best_cost:
            best, best_cost = rect, cost
    return best


def annotate_screenshot(img_path: str, elements: list[dict]) -> str:
    """Draw numbered boxes on top 50 elements; store all in map, return text index."""
    element_map.clear()

    if not elements:
        return ""

    # Store all elements in map with native click coordinates.
    for i, el in enumerate(elements, 1):
        idx = el.get("index", i)
        cx = el.get("click_x", el["x"] + el["width"] / 2)
        cy = el.get("click_y", el["y"] + el["height"] / 2)
        element_map[idx] = {
            "click_x": cx, "click_y": cy,
            "label": el.get("label", ""), "tag": el.get("tag", ""),
        }

    img = Image.open(img_path).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    # Scale to screenshot resolution: ~13px at 1280 wide, ~27px on a 2560 Retina capture.
    font_size = max(12, round(img.width / 95))
    font = _load_font(font_size)
    line_w = max(2, font_size // 9)
    pad = max(2, font_size // 5)

    # Pass 1: boxes with a faint tint, so badges drawn later are never covered.
    drawn = []
    for i, el in enumerate(elements[:_MAX_DRAWN]):
        color = _PALETTE[i % len(_PALETTE)]
        x, y, w, h = el["x"], el["y"], el["width"], el["height"]
        draw.rectangle([x, y, x + w, y + h], fill=(*color, 28), outline=(*color, 255), width=line_w)
        drawn.append((el, color))

    # Pass 2: badges placed around their box, avoiding other badges and other boxes' content.
    boxes = [(el["x"], el["y"], el["x"] + el["width"], el["y"] + el["height"]) for el, _ in drawn]
    taken = []
    for (el, color), box in zip(drawn, boxes):
        text = str(el.get("index", 0))
        l, t, r, b = font.getbbox(text)
        bw, bh = r - l + 2 * pad, b - t + 2 * pad
        rect = _place_badge(box, bw, bh, img.width, img.height, taken, boxes)
        taken.append(rect)
        draw.rectangle(rect, fill=(*color, 240), outline=(255, 255, 255, 255), width=1)
        draw.text((rect[0] + pad - l, rect[1] + pad - t), text, fill=(255, 255, 255, 255), font=font)

    img = Image.alpha_composite(img, overlay)
    img.convert("RGB").save(img_path)

    # Return text index for ALL elements
    return _format_index(elements)


def _format_index(elements: list[dict]) -> str:
    """Format element index with native click coordinates."""
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
