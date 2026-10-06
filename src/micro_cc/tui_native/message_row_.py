"""Message row components with caching, image support, and streaming text."""

import base64
import io
from math import ceil

from rich.console import Console, Group
from rich.text import Text
from PIL import Image as PILImage

from micro_cc import mods_
from micro_cc.tui_native.md_render_ import render_md, rich_theme
from micro_cc.utils import theme_store_
from micro_cc.tui_native.glyphs_ import glyph
from micro_cc.tui_native.renderers_ import render_msg
from micro_cc.tui_native.detect_images_ import (
    detect_capabilities, calculate_image_cell_size, encode_kitty,
    get_cell_dimensions, allocate_image_id, image_fallback, ImageDimensions,
    register_kitty_image_metadata,
)
_ANSI_PLAIN_CACHE_MISS = object()


def _render_to_lines(renderable, width: int) -> list[str]:
    """Flatten Rich renderable to ANSI lines; applies consistent theme at the choke point."""
    buf = io.StringIO()
    console = Console(
        file=buf,
        width=max(1, width),
        force_terminal=True,
        color_system="truecolor",
        highlight=False,
        soft_wrap=False,
        theme=rich_theme(),
    )
    console.print(renderable, end="")
    return buf.getvalue().split("\n")


class Image:
    """Re-encodes base64 image data as PNG and renders via Kitty protocol."""

    # Avoid transmitting unnecessary resolution; no terminal displays >500px.
    _MAX_DIMENSION_PX = 500
    # Cap display to 60 cells to prevent images from dominating the layout.
    _MAX_DISPLAY_WIDTH_CELLS = 60

    def __init__(self, base64_data: str):
        raw_bytes = base64.b64decode(base64_data)
        with PILImage.open(io.BytesIO(raw_bytes)) as img:
            img = img.convert("RGBA")
            if max(img.size) > self._MAX_DIMENSION_PX:
                scale = self._MAX_DIMENSION_PX / max(img.size)
                img = img.resize((round(img.width * scale), round(img.height * scale)), PILImage.LANCZOS)
            self.dims = ImageDimensions(*img.size)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            self.encoded_data = base64.b64encode(buf.getvalue()).decode("ascii")
        self.image_id = None

    def render(self, width: int) -> list[str]:
        caps = detect_capabilities()
        if not caps.images:
            return [image_fallback(self.dims)]  # "[Image: 800x600]"
        max_width = max(1, min(width - 2, self._MAX_DISPLAY_WIDTH_CELLS))
        cell_px = get_cell_dimensions()
        # Cap height to prevent tall images from balloons past viewport (cells taller than wide).
        max_height = max(1, ceil(max_width * cell_px.w / cell_px.h))
        size = calculate_image_cell_size(self.dims, max_width, max_height, cell_px)
        self.image_id = self.image_id or allocate_image_id()
        # Register pixel dimensions for partial-window cropping; not recoverable from escape sequence.
        register_kitty_image_metadata(self.image_id, self.dims.width, self.dims.height)
        seq = encode_kitty(self.encoded_data, size.columns, size.rows, self.image_id)
        return [seq] + [""] * (size.rows - 1)


class MessageRow:
    """One message = one component with per-width render caching."""

    def __init__(self, msg: dict):
        self._msg = msg
        self._cached_width: int | None = None
        self._cached_lines: list[str] | None = None
        self._cached_image: Image | None = None

    def _build(self):
        """Rich renderable for the msg via the renderer registry."""
        return render_msg(self._msg)

    def _render_image_result(self, width: int) -> list[str]:
        if self._cached_image is None:
            self._cached_image = Image(self._msg["result"]["data"])
        header = _render_to_lines(Text(f"{glyph('tool')} {self._msg['name']}", style="bold dim"), width)
        return header + self._cached_image.render(width)

    def render(self, width: int) -> list[str]:
        # Cache once per width; simple caching is the proven design for all row types.
        if self._cached_width == width and self._cached_lines is not None:
            return self._cached_lines
        result = self._msg.get("result")
        if self._msg["type"] == "tool_call" and isinstance(result, dict) and result.get("type") == "image":
            lines = self._render_image_result(width)
        else:
            lines = _render_to_lines(self._build(), width)
        self._cached_width = width
        self._cached_lines = lines
        return lines

    def invalidate(self) -> None:
        self._cached_width = None
        self._cached_lines = None

    def invalidate_cache(self) -> None:
        """Force re-render; called when message content changes (tool result, toggle)."""
        self.invalidate()

    def update_msg(self, msg: dict) -> None:
        self._msg = msg
        self.invalidate_cache()

    def toggle_expanded(self) -> bool:
        t = self._msg.get("type")
        expandable = t == "approval" or (t == "tool_call" and self._msg.get("result") is not None)
        if expandable:
            self._msg["expanded"] = not self._msg.get("expanded", False)
            self.invalidate_cache()

            return True
        return False


class StreamingRow:
    """Streaming text/thinking block; append() writes directly, no buffering."""

    def __init__(self, mode: str = "text"):
        self._mode = mode  # "text" or "thinking"
        self._content = ""
        self._stable_content = ""
        self._stable_rendered = None
        self._cached_width: int | None = None
        self._cached_lines: list[str] | None = None

    def append(self, chunk: str) -> None:
        self._content += chunk
        self.invalidate()

    def _build(self):
        if self._mode == "thinking":
            parts = [Text(glyph("thinking"), style="dim italic")]
            if self._content:
                parts.append(render_md(self._content, dim=True))
            return Group(*parts)

        boundary = self._content.rfind("\n\n")
        if boundary > len(self._stable_content):
            self._stable_content = self._content[:boundary]
            self._stable_rendered = render_md(self._stable_content)

        if self._stable_rendered and boundary > 0:
            suffix = self._content[boundary:]
            return Group(self._stable_rendered, render_md(suffix))
        return render_md(self._content) if self._content else Text("")

    def render(self, width: int) -> list[str]:
        if self._cached_width == width and self._cached_lines is not None:
            return self._cached_lines
        lines = _render_to_lines(self._build(), width)
        self._cached_width = width
        self._cached_lines = lines
        return lines

    def invalidate(self) -> None:
        self._cached_width = None
        self._cached_lines = None

    def get_content(self) -> str:
        return self._content


class RichStatic:
    """Cached Rich markup renderer for banner/statusbar/hint lines; named ones are mod-hookable."""

    def __init__(self, content: str = "", single_line: bool = False, max_lines: int | None = None,
                 name: str | None = None):
        """single_line=True enforces 1 row for status/hint lines; max_lines caps multi-line content."""
        self._content = content
        self._single_line = single_line
        self._max_lines = max_lines
        self._name = name
        self._cached_width: int | None = None
        self._cached_lines: list[str] | None = None
        self._cached_gen: int | None = None

    def update(self, content: str) -> None:
        if content == self._content:
            return
        self._content = content
        self.invalidate()

    def render(self, width: int) -> list[str]:
        hooked = self._name is not None and mods_.has("render", component=self._name)
        gen = mods_.generation() if hooked else None
        if self._cached_width == width and self._cached_lines is not None and self._cached_gen == gen:
            return self._cached_lines
        content = mods_.render_markup(self._name, self._content, width) if hooked else self._content
        lines = []
        if content:
            renderable = Text.from_markup(content)
            if self._single_line:
                renderable.no_wrap = True
                renderable.overflow = "ellipsis"
            lines = _render_to_lines(renderable, width)
            if self._single_line and len(lines) > 1:
                lines = lines[:1]  # belt-and-braces — Rich's own no_wrap should already guarantee this
            elif self._max_lines is not None and len(lines) > self._max_lines:
                lines = lines[: self._max_lines]
                lines[-1] = lines[-1] + " …"
        self._cached_width = width
        self._cached_lines = lines
        self._cached_gen = gen
        return lines

    def invalidate(self) -> None:
        self._cached_width = None
        self._cached_lines = None


class HRule:
    """Full-width horizontal divider; dim by default for quiet structure."""

    def __init__(self, char: str | None = None, dim: bool = True):
        self._char = char or glyph("rule")
        self._dim = dim

    def render(self, width: int) -> list[str]:
        line = self._char * max(0, width)
        return [f"\x1b[2m{line}\x1b[0m" if self._dim else line]

    def invalidate(self) -> None:
        pass
