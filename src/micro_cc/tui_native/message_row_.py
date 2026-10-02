"""Adapts MessageRow/StreamingRow (Textual Static subclasses) to the
Component protocol (render(width) -> list[str], invalidate() -> None).
The per-width render cache below isn't optional — without it,
VStack.render() walking the full transcript every frame costs a full Rich
layout pass per row instead of a cache hit.

_build() is untouched from window_overlay_.py — it returns the same Rich
renderable, unchanged, for the same msg dict shape. Only the render() path
below is new: Rich renderables get flattened to plain ANSI-coded strings
via a real Console print into a StringIO (not Console.render_lines(), which
hands back Segment lists rather than the list[str] the Component protocol
needs), so composite_tui_line/apply_selection/etc. in alt_screen_.py can
treat this exactly like any other component's output.

SelectableRich (selectable_rich_.py) is dropped, not replaced 1:1 — drag-
select/copy now happens at the TuiAltScreen level (alt_screen_.py's
SelectionPoint/apply_selection/copy_selection_to_clipboard), which reads
directly off the composited screen rather than needing each row to opt
into a selectable wrapper. 
"""

import base64
import io
from math import ceil

from rich.console import Console, Group
from rich.text import Text
from PIL import Image as PILImage

from micro_cc.tui_native.md_render_ import render_md, rich_theme
from micro_cc.utils import theme_store_
from micro_cc.tui_native.diff_viewer_ import build_diff_lines
from micro_cc.tui_native.detect_images_ import (
    detect_capabilities, calculate_image_cell_size, encode_kitty,
    get_cell_dimensions, allocate_image_id, image_fallback, ImageDimensions,
    register_kitty_image_metadata,
)
_ANSI_PLAIN_CACHE_MISS = object()

# Expanded tool rows (bash_ results, diffs, previews) were only ever capped
# by character count (_cap=4000 below) — a result with many short lines
# stays well under that cap while still rendering hundreds of on-screen
# rows. This bounds every expanded section by line count too.
_MAX_EXPANDED_LINES = 40


def _ansi_plain(raw: str) -> str:
    return Text.from_ansi(raw).plain


def _cap_lines(text: str, max_lines: int = _MAX_EXPANDED_LINES) -> str:
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n… truncated, {len(lines)} lines total"


def _cap_diff_lines(diff_lines: list, max_lines: int = _MAX_EXPANDED_LINES) -> list:
    if len(diff_lines) <= max_lines:
        return diff_lines
    note = Text(f"… truncated, {len(diff_lines)} lines total", style="dim italic")
    return diff_lines[:max_lines] + [note]


def _render_to_lines(renderable, width: int) -> list[str]:
    """Flatten a Rich renderable into plain ANSI-coded lines at exactly
    `width` columns — real SGR escape codes, not Segment objects, so the
    result is a drop-in list[str] for the Component protocol.

    LIGHT_THEME (md_render_.py) is applied here rather than left to each
    renderable, because this is the single Console every widget in the tree
    funnels through — banner, message rows, RichStatic status/hint lines
    alike. One theme at the choke point is what keeps Rich's built-in
    markdown styles (inline code is cyan-on-black by default) from landing
    unreadable on the alt screen's white background."""
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
    """base64_data is whatever the tool actually returned (read_'s output
    is always JPEG — see sanitize_and_encode_image_) — decoded and
    re-encoded as PNG through Pillow. Transmits real PNG bytes (f=100)
    rather than raw pixels, which allows better compression and simpler
    encoding without pixel density parameters. An earlier version used raw
    RGBA (f=32), which required extra parameters and produced larger output
    for most images."""

    # Full-resolution screenshots (e.g. a 2800px-wide Retina capture) are
    # pure waste to transmit at native size — no terminal displays an
    # image at more than a few hundred cells across. A reasonable cap (60 cells)
    # with headroom for pixel density avoids decoding/transmitting resolution
    # that display limits will never show anyway.
    _MAX_DIMENSION_PX = 500
    # Cap display width to 60 cells regardless of terminal width, preventing
    # images from occupying too much of the screen. Using the full row width
    # was tried before but made images too dominant in the layout.
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
        # Passing None here for "unbounded height" (matching the original
        # sketch) is the actual bug behind "image behind text and input":
        # for a portrait/tall image, scaling to fit only the column width
        # can produce a row count far larger than the terminal's visible
        # height, ballooning the placement past the viewport entirely.
        # Capping height prevents this: derive it from the column width and
        # the terminal's actual cell aspect ratio (cells are much taller than
        # wide), so a tall image is bounded to roughly half its column count
        # in rows when no explicit max height is given.
        max_height = max(1, ceil(max_width * cell_px.w / cell_px.h))
        size = calculate_image_cell_size(self.dims, max_width, max_height, cell_px)
        self.image_id = self.image_id or allocate_image_id()
        # crop_kitty_image_line (detect_images_.py) needs the source pixel
        # dimensions to compute y=/h= when this line is only partially in
        # the scroll window — not recoverable from the escape sequence
        # text itself, so it's registered out-of-band here.
        register_kitty_image_metadata(self.image_id, self.dims.width, self.dims.height)
        seq = encode_kitty(self.encoded_data, size.columns, size.rows, self.image_id)
        return [seq] + [""] * (size.rows - 1)


class MessageRow:
    """One message = one component. Render is cached per-width; history
    rows never re-render unless invalidate_cache() is called (edit,
    expand/collapse toggle)."""

    def __init__(self, msg: dict):
        self._msg = msg
        self._cached_width: int | None = None
        self._cached_lines: list[str] | None = None
        self._cached_image: Image | None = None

    def _build(self):
        """Build Rich renderable from msg dict. Verbatim port of
        window_overlay_.MessageRow._build() — content logic is identical,
        only the render() path around it changed."""
        msg = self._msg
        t = msg["type"]
        if t == "user":
            return Text(f"› {msg['content']}", style="bold")
        elif t == "user_queued":
            return Text(f"⋯queued {msg['content']}", style=f"dim italic {theme_store_.get('queued')}")
        elif t == "text":
            return render_md(msg["content"])
        elif t == "thinking":
            return Group(
                Text("∴ Thinking", style="dim italic"),
                render_md(msg["content"], dim=True),
            )
        elif t == "tool_call":
            name = msg['name']
            inp = msg.get("input", {})
            result = msg.get('result')
            expanded = msg.get('expanded')
            if result is None:
                return Text(f"◇ {name} ", style=f"italic {theme_store_.get('warn')}")
            if expanded:
                if name == "edit_":
                    fp = inp.get('file_path', '')
                    old = inp.get('old_string', '')
                    new = inp.get('new_string', '')
                    parts = [Text.assemble((f"⟐ {name}", "bold dim"), ("   click to collapse", "dim italic"))]
                    if fp:
                        parts.append(Text(fp, style="dim"))
                    if old or new:
                        parts.append(Group(*_cap_diff_lines(build_diff_lines(old, new, fp))))
                    parts.append(Text("─" * 40, style="dim"))
                    return Group(*parts)
                _cap = 4000
                shown_inp = _cap_lines(str(inp))[:_cap]
                result_str = str(result)
                capped_result = _cap_lines(result_str)
                tail_note = Text(
                    f"… truncated, {len(result_str)} chars total", style="dim italic"
                ) if len(capped_result) > _cap else None
                capped_result = capped_result[:_cap]
                if name == "bash_":
                    result_body = Text.from_ansi(capped_result)
                else:
                    result_body = render_md(f"```\n{_ansi_plain(capped_result)}\n```")
                return Group(
                    Text.assemble((f"⟐ {name}", "bold dim"), ("   click to collapse", "dim italic")),
                    Text("input", style="dim italic"),
                    render_md(f"```\n{shown_inp}\n```"),
                    Text("result", style="dim italic"),
                    result_body,
                    *([tail_note] if tail_note else []),
                    Text("─" * 40, style="dim"),
                )
            plain_result = _ansi_plain(str(result))
            first_line = plain_result.splitlines()[0] if plain_result else plain_result
            truncated = len(first_line) > 40 or "\n" in plain_result
            preview = first_line[:40] + ("…" if truncated else "")
            return Text(f"⟐ {name} → {preview} (click to expand)", style="dim")
        elif t == "error":
            return Text(f"△ {msg['content']}", style=f"italic {theme_store_.get('error')}")
        elif t == "approval":
            inp = msg.get('input', {})
            name = msg['name']
            expanded = msg.get('expanded')
            hint = "click to collapse" if expanded else "click to view full"
            header = Text.assemble(
                (f"◆ ", f"bold {theme_store_.get('warn')}"), (name, f"bold {theme_store_.get('warn')}"),
                (f"   enter to approve · esc to reject · {hint}", "dim"),
            )
            if name == "bash_":
                code = inp.get('command', str(inp))
                if expanded:
                    body = render_md(f"```bash\n{_cap_lines(code)}\n```")
                else:
                    first_line = code.splitlines()[0] if code else code
                    preview = first_line[:80] + ("…" if len(first_line) > 80 or "\n" in code else "")
                    body = Text(preview, style="dim")
            elif name == "edit_":
                fp = inp.get('file_path', '')
                old = inp.get('old_string', '')
                new = inp.get('new_string', '')
                if expanded:
                    parts = []
                    if fp:
                        parts.append(Text(fp, style="dim"))
                    if old or new:
                        parts.append(Group(*_cap_diff_lines(build_diff_lines(old, new, fp))))
                    body = Group(*parts) if parts else Text(str(inp))
                else:
                    body = Text(fp or str(inp)[:80], style="dim")
            elif name == "write_":
                fp = inp.get('file_path', '')
                content = inp.get('content', '')
                if expanded:
                    parts = []
                    if fp:
                        parts.append(Text(fp, style="dim"))
                    if content:
                        preview = content[:500] + ("…" if len(content) > 500 else "")
                        parts.append(render_md(f"```\n{_cap_lines(preview)}\n```"))
                    body = Group(*parts) if parts else Text(str(inp))
                else:
                    label = f"{fp} ({len(content)} chars)" if fp else str(inp)[:80]
                    body = Text(label, style="dim")
            else:
                body = render_md(f"```\n{_cap_lines(str(inp))}\n```") if expanded else Text(str(inp)[:80] + "…", style="dim")
            return Group(header, body, Text("─" * 40, style="dim"))
        return Text("")

    def _render_image_result(self, width: int) -> list[str]:
        if self._cached_image is None:
            self._cached_image = Image(self._msg["result"]["data"])
        header = _render_to_lines(Text(f"⟐ {self._msg['name']}", style="bold dim"), width)
        return header + self._cached_image.render(width)

    def render(self, width: int) -> list[str]:
        # Cache once per width, never touch it again unless width changes.
        # An earlier version tried to bypass this cache for image rows to
        # downgrade from an expensive transmit to a cheap reposition command
        # after the first call, but that extra machinery broke rendering.
        # Simple caching, same as every other row type, is the proven design.
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
        """Force re-render (e.g. tool_call gets its result). Same name as
        the Textual-era method — etype_handler_.py calls this today."""
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
    """Dedicated component for the currently-streaming text/thinking block.

    No buffering, no timer, no flush: `append()` writes straight into
    _content. The typewriter pacing lives at the SOURCE instead — the caller
    (etype_handler_._stream_delta) awaits a small sleep between each slice
    it appends, which throttles how fast it pulls the next event off the
    claude_loop async generator. Same trick as webui/bridge.py's `reveal()`.
    Since nothing is ever buffered here, there's nothing to hard-flush on
    turn-end or interruption — _content simply IS whatever's been revealed
    so far, always. This replaces an older pending-buffer +
    render-loop-tick design."""

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
            parts = [Text("∴ Thinking", style="dim italic")]
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
    """Cached, cheap stand-in for the Textual `Static` widgets that used to
    carry the banner/statusbar/hint/working/subagent/bgproc lines
    (start_live_.py:723-753) — those are fed Rich console markup
    (`[red]...[/red]`, `[#ff69b4]...[/#ff69b4]`), not plain text, and Rich
    is already a hard dependency here (render_md, MessageRow above), so
    this just reuses Rich's own markup parser via _render_to_lines instead
    of hand-rolling a narrower one. Prefer tui_native/text_.py's `TextLine`
    for anything that's genuinely plain/ANSI text with no markup to parse.
    This class exists only because several existing call sites already assume
    Rich markup syntax and porting that syntax is simpler than refactoring
    all the callers."""

    def __init__(self, content: str = "", single_line: bool = False, max_lines: int | None = None):
        """single_line=True enforces exactly one row, truncated with a
        real ellipsis — for status/hint/working/subagent/bgproc lines,
        which the bottom-bar layout assumes are always 1 row tall (a
        RichStatic that silently wrapped to 2+ rows was throwing off
        every sibling's position — confirmed: a normal-length hint wraps
        to 2 lines at width 80 with the default multi-line behavior).
        Leave False for genuinely multi-line content (the banner,
        ask_question_text).

        max_lines caps wrapped output at N rows (with a trailing "…" on
        the last kept row if content overflowed it) without forcing it
        down to exactly 1 like single_line does — content still renders 0
        rows when empty (see render()) and grows/shrinks naturally up to
        the cap otherwise, same sizing behavior as every sibling in this
        bottom bar, just bounded. Meaningless combined with single_line
        (which already caps at 1); only meant for the False case."""
        self._content = content
        self._single_line = single_line
        self._max_lines = max_lines
        self._cached_width: int | None = None
        self._cached_lines: list[str] | None = None

    def update(self, content: str) -> None:
        if content == self._content:
            return
        self._content = content
        self.invalidate()

    def render(self, width: int) -> list[str]:
        if not self._content:
            return []
        if self._cached_width == width and self._cached_lines is not None:
            return self._cached_lines
        renderable = Text.from_markup(self._content)
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
        return lines

    def invalidate(self) -> None:
        self._cached_width = None
        self._cached_lines = None


class HRule:
    """A full-width horizontal divider — bracketing the prompt input top
    and bottom, matching how cc visually separates it from the working-
    status text above and the statusbar/hint text below. Dim by default
    so it reads as a quiet structural line, not a loud border."""

    def __init__(self, char: str = "─", dim: bool = True):
        self._char = char
        self._dim = dim

    def render(self, width: int) -> list[str]:
        line = self._char * max(0, width)
        return [f"\x1b[2m{line}\x1b[0m" if self._dim else line]

    def invalidate(self) -> None:
        pass
