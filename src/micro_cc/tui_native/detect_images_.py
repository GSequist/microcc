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
    """Get terminal cell size in pixels; fall back to common default if unavailable."""
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
    """Allocate unique image ID for Kitty graphics protocol."""
    global _next_image_id
    _next_image_id += 1
    return _next_image_id


def encode_kitty(base64_data: str, columns: int, rows: int, image_id: int) -> str:
    """Encode image in Kitty graphics protocol (f=100 PNG, C=1 no cursor movement)."""
    delete = f"\x1b_Ga=d,d=i,i={image_id}\x1b\\"
    params = f"a=T,f=100,q=2,C=1,c={columns},r={rows},i={image_id}"
    if len(base64_data) <= 4096:
        return delete + f"\x1b_G{params};{base64_data}\x1b\\"
    return delete + _chunk_and_wrap(base64_data, params)


def delete_all_kitty_images() -> str:
    """Delete all image placements and pixel data (d=A + q=2)."""
    return "\x1b_Ga=d,d=A,q=2\x1b\\"


def _chunk_and_wrap(base64_data: str, params: str) -> str:
    """Split large payload into 4096-byte chunks with Kitty protocol framing."""
    chunks = [base64_data[i:i + 4096] for i in range(0, len(base64_data), 4096)]
    seq = ""
    for i, chunk in enumerate(chunks):
        more = 0 if i == len(chunks) - 1 else 1
        chunk_params = f"{params},m={more}" if i == 0 else f"m={more}"
        seq += f"\x1b_G{chunk_params};{chunk}\x1b\\"
    return seq


def image_fallback(dims: ImageDimensions) -> str:
    return f"[Image: {dims.width}x{dims.height}]"


# --- Cropping a partially-scrolled image line ----
# When scroll top falls inside an image's row span, recompute placement to show visible slice only.

_kitty_image_metadata: dict[int, tuple[int, int]] = {}   # image_id -> (width_px, height_px)


def register_kitty_image_metadata(image_id: int, width_px: int, height_px: int) -> None:
    """Register image pixel dimensions for crop_kitty_image_line; not in escape sequence."""
    _kitty_image_metadata[image_id] = (width_px, height_px)
    if len(_kitty_image_metadata) > 1000:
        del _kitty_image_metadata[next(iter(_kitty_image_metadata))]


_KITTY_PARAM_RE = re.compile(r"(?:^|(?<=,))([a-zA-Z])=(-?\d+)(?=,|$)")


def _parse_kitty_params(controls: str) -> dict[str, int]:
    return {m.group(1): int(m.group(2)) for m in _KITTY_PARAM_RE.finditer(controls)}


def _iter_kitty_segments(line: str):
    """Yield (start, end, controls_str) for each Kitty graphic protocol segment."""
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
    """Find image placements: [(start_index, row_count), ...] for each block."""
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
    """Rewrite image placement y=/h=/r= to show visible slice of source pixels."""
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
