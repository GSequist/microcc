import fcntl
import os
import re
import struct
import termios
from math import ceil
from typing import NamedTuple


class Capabilities(NamedTuple):
    images: str | None
    true_color: bool
    hyperlinks: bool


def detect_capabilities() -> Capabilities:
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    if os.environ.get("TMUX") or os.environ.get("TERM", "").startswith("tmux"):
        return Capabilities(images=None, true_color=False, hyperlinks=False)          # images unreliable under tmux
    if (os.environ.get("KITTY_WINDOW_ID")
            or os.environ.get("ITERM_SESSION_ID")       # iTerm2 3.5+ also speaks Kitty's graphics protocol
            or term_program in ("kitty", "ghostty", "wezterm", "warpterminal", "iterm.app")):
        return Capabilities(images="kitty", true_color=True, hyperlinks=True)
    return Capabilities(images=None, true_color=False, hyperlinks=False)


class ImageDimensions(NamedTuple):
    width: int
    height: int


class CellSize(NamedTuple):
    columns: int
    rows: int


class CellPixels(NamedTuple):
    w: float
    h: float


def get_cell_dimensions() -> CellPixels:
    """Terminal cell size in pixels, needed to convert an image's pixel
    dimensions into a column/row count. TIOCGWINSZ (same ioctl SIGWINCH
    resize handling already reads for rows/cols — see terminal_.py) also
    reports the window size in pixels on most terminals; a few report 0
    for the pixel fields, so fall back to a plausible default rather than
    dividing by zero."""
    try:
        rows, cols, xpixels, ypixels = struct.unpack(
            "HHHH", fcntl.ioctl(1, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
        )
        if xpixels and ypixels and cols and rows:
            return CellPixels(w=xpixels / cols, h=ypixels / rows)
    except OSError:
        pass
    return CellPixels(w=8, h=16)   # common default cell size when the terminal doesn't report pixels


def calculate_image_cell_size(
    dims: ImageDimensions, max_width_cells: int, max_height_cells: int | None, cell_px: CellPixels
) -> CellSize:
    scale = min(max_width_cells * cell_px.w / dims.width,
                (max_height_cells or 10**9) * cell_px.h / dims.height)
    return CellSize(columns=ceil(dims.width * scale / cell_px.w),
                     rows=ceil(dims.height * scale / cell_px.h))


_next_image_id = 0


def allocate_image_id() -> int:
    """Kitty needs a small unique integer per transmitted image (the `i=`
    param) to reference it in later placement/delete commands. A plain
    incrementing counter is enough for v1 — nothing here reuses ids
    across rows or re-transmits without a fresh id."""
    global _next_image_id
    _next_image_id += 1
    return _next_image_id


def encode_kitty(base64_data: str, columns: int, rows: int, image_id: int) -> str:
    """f=100 (real PNG file bytes) encodes the image in compressed format,
    which is more efficient than f=32 (raw RGBA) that would need s=/v=
    parameters and produce larger output. message_row_.Image PNG-encodes
    via Pillow to match this encoding.

    Delete-before-transmit is safe here regardless of exact `d=i` delete
    semantics (whether it frees the underlying image data or only the
    placement) because the full payload gets sent again immediately after.
    No placement-reuse tricks — simply cache once per width and re-transmit
    the entire image when resizing.

    C=1 sets Kitty's cursor-movement policy after placement. Default is to
    move the cursor itself once the image is drawn; we set moveCursor: false
    (C=1) because do_render owns cursor placement entirely via explicit
    positioning before every write. Two things independently moving the
    cursor after this sequence executes desyncs every write that follows —
    the image fills the whole screen with text scattered over it, banner
    gone."""
    delete = f"\x1b_Ga=d,d=i,i={image_id}\x1b\\"
    params = f"a=T,f=100,q=2,C=1,c={columns},r={rows},i={image_id}"
    if len(base64_data) <= 4096:
        return delete + f"\x1b_G{params};{base64_data}\x1b\\"
    return delete + _chunk_and_wrap(base64_data, params)


def delete_all_kitty_images() -> str:
    """Delete all image placements and underlying pixel data (d=A + q=2).
    /clear needs this: it wipes MessageRow/message_list (the text-grid data
    model) but the images those rows placed are a separate compositing layer
    Kitty keeps independently — without this they'd stay visually painted on
    screen, with nothing left in this app's state to reference them anymore."""
    return "\x1b_Ga=d,d=A,q=2\x1b\\"


def _chunk_and_wrap(base64_data: str, params: str) -> str:
    """Kitty caps a single escape sequence's payload at 4096 base64 bytes.
    Per the protocol spec: the first chunk carries the full param set plus
    m=1, every following chunk carries only m=1 (m=0 on the last one)."""
    chunks = [base64_data[i:i + 4096] for i in range(0, len(base64_data), 4096)]
    seq = ""
    for i, chunk in enumerate(chunks):
        more = 0 if i == len(chunks) - 1 else 1
        chunk_params = f"{params},m={more}" if i == 0 else f"m={more}"
        seq += f"\x1b_G{chunk_params};{chunk}\x1b\\"
    return seq


def image_fallback(dims: ImageDimensions) -> str:
    return f"[Image: {dims.width}x{dims.height}]"


# --- Cropping a partially-scrolled image line -----------
# Needed because a Kitty image's escape sequence lives on only the FIRST of
# its `r` reserved rows — the rest are blank padding so the row count lines
# up for layout. ScrollView.get_scrolled_lines slices full_lines[top:top+viewport_height]
# like any other text; if `top` falls strictly inside an image's row span,
# the escape-sequence line (the only one that actually carries the placement
# command) is excluded even though some of the image's trailing blank rows
# are still technically in range — nothing gets sent to Kitty, the image
# just doesn't render at all. This recomputes the placement to show only
# the visible source rows instead of all-or-nothing.

_kitty_image_metadata: dict[int, tuple[int, int]] = {}   # image_id -> (width_px, height_px)


def register_kitty_image_metadata(image_id: int, width_px: int, height_px: int) -> None:
    """Called from message_row_.Image.render() every time it builds a
    placement — crop_kitty_image_line needs the SOURCE pixel dimensions
    to compute y=/h=, and those aren't recoverable from the escape
    sequence text itself (only c=/r=, the target CELL size, are)."""
    _kitty_image_metadata[image_id] = (width_px, height_px)
    if len(_kitty_image_metadata) > 1000:
        del _kitty_image_metadata[next(iter(_kitty_image_metadata))]


_KITTY_PARAM_RE = re.compile(r"(?:^|(?<=,))([a-zA-Z])=(-?\d+)(?=,|$)")


def _parse_kitty_params(controls: str) -> dict[str, int]:
    return {m.group(1): int(m.group(2)) for m in _KITTY_PARAM_RE.finditer(controls)}


def _iter_kitty_segments(line: str):
    """Yield (start, end, controls_str) for each \\x1b_G...\\x1b\\\\ control
    block in `line`. `end` is the index right after the closing \\x1b\\\\.
    A payload-carrying segment (a=T's first chunk) has controls up to its
    first ';'; a control-only segment (the a=d delete prefix every line
    here starts with — see encode_kitty) has no ';' at all, so its
    controls run all the way to the terminator itself. Finding just the
    line's first ';' globally (the earlier, wrong approach) breaks on
    exactly this shape: the delete prefix has no ';' of its own, so a
    naive scan for the first ';' anywhere in the line lands inside the
    NEXT segment's payload, merging two segments' controls into one
    garbled string."""
    i = 0
    while True:
        start = line.find("\x1b_G", i)
        if start == -1:
            return
        term = line.find("\x1b\\", start + 3)
        if term == -1:
            return
        semi = line.find(";", start + 3, term)
        controls_end = semi if semi != -1 else term
        yield start, term + 2, line[start + 3:controls_end]
        i = term + 2


def find_kitty_image_spans(lines: list[str]) -> list[tuple[int, int]]:
    """[(start_index, row_count), ...] for each Kitty image block in
    `lines` — the placement segment's own r= is the row count, found by
    walking segments (see _iter_kitty_segments) past the delete prefix."""
    spans = []
    for idx, line in enumerate(lines):
        if "\x1b_G" not in line:
            continue
        for _, _, controls_str in _iter_kitty_segments(line):
            params = _parse_kitty_params(controls_str)
            if "r" in params and "i" in params:
                spans.append((idx, params["r"]))
                break
    return spans


def crop_kitty_image_line(line: str, hidden_rows: int, visible_rows: int) -> str:
    """Crop image placement to visible window. hidden_rows = how many of the
    image's own rows are above the current window's top; visible_rows =
    how many of its rows actually fall inside the window. Rewrites the
    placement segment's y=/h=/r= to show only that slice of source pixels,
    leaving the delete prefix and the payload itself untouched."""
    for start, end, controls_str in _iter_kitty_segments(line):
        params = _parse_kitty_params(controls_str)
        if "r" not in params or "i" not in params:
            continue
        image_id, total_rows = params["i"], params["r"]
        meta = _kitty_image_metadata.get(image_id)
        if meta is None or hidden_rows < 0 or hidden_rows >= total_rows or visible_rows <= 0:
            return line
        width_px, height_px = meta
        cropped_rows = min(visible_rows, total_rows - hidden_rows)
        if hidden_rows == 0 and cropped_rows == total_rows:
            return line
        source_y = (height_px * hidden_rows) // total_rows
        source_end = -(-(height_px * (hidden_rows + cropped_rows)) // total_rows)   # ceil div
        source_height = max(1, min(height_px, source_end) - source_y)
        kept = {k: v for k, v in params.items() if k not in ("y", "h", "r")}
        kept["y"], kept["h"], kept["r"] = source_y, source_height, cropped_rows
        new_controls = ",".join(f"{k}={v}" for k, v in kept.items())
        semi = line.find(";", start, end)
        if semi != -1:
            return f"{line[:start]}\x1b_G{new_controls}{line[semi:]}"
        return f"{line[:start]}\x1b_G{new_controls}\x1b\\{line[end:]}"
    return line
