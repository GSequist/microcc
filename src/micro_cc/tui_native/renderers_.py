"""Builtin message renderers; mods wrap them via render(component="message")."""

import copy
from types import SimpleNamespace

from rich.console import Group
from rich.text import Text

from micro_cc.tui_native.diff_viewer_ import build_diff_lines
from micro_cc.tui_native.glyphs_ import glyph
from micro_cc.tui_native.md_render_ import render_md
from micro_cc import mods_
from micro_cc.utils import theme_store_

_MAX_EXPANDED_LINES = 40


def ansi_plain(raw: str) -> str:
    return Text.from_ansi(raw).plain


def cap_lines(text: str, max_lines: int = _MAX_EXPANDED_LINES) -> str:
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return text
    return "\n".join(lines[:max_lines]) + f"\n… truncated, {len(lines)} lines total"


def cap_diff_lines(diff_lines: list, max_lines: int = _MAX_EXPANDED_LINES) -> list:
    if len(diff_lines) <= max_lines:
        return diff_lines
    note = Text(f"… truncated, {len(diff_lines)} lines total", style="dim italic")
    return diff_lines[:max_lines] + [note]


def helpers() -> SimpleNamespace:
    return SimpleNamespace(
        glyph=glyph, color=theme_store_.get, render_md=render_md, diff_lines=build_diff_lines,
        cap_lines=cap_lines, cap_diff_lines=cap_diff_lines, ansi_plain=ansi_plain,
        Text=Text, Group=Group,
    )


# --- builtin renderers ---------------------------------------------------

def _user(msg, r):
    return Text(f"{glyph('user_prompt')} {msg['content']}", style="bold")


def _user_queued(msg, r):
    return Text(f"{glyph('queued')} {msg['content']}", style=f"dim italic {theme_store_.get('queued')}")


def _text(msg, r):
    return render_md(msg["content"])


def _thinking(msg, r):
    return Group(Text(glyph("thinking"), style="dim italic"), render_md(msg["content"], dim=True))


def _error(msg, r):
    return Text(f"{glyph('error')} {msg['content']}", style=f"italic {theme_store_.get('error')}")


def _tool_call(msg, r, ansi_result=False):
    name = msg["name"]
    inp = msg.get("input", {})
    result = msg.get("result")
    if result is None:
        return Text(f"{glyph('tool_pending')} {name} ", style=f"italic {theme_store_.get('warn')}")
    if msg.get("expanded"):
        _cap = 4000
        shown_inp = cap_lines(str(inp))[:_cap]
        result_str = str(result)
        capped_result = cap_lines(result_str)
        tail_note = Text(
            f"… truncated, {len(result_str)} chars total", style="dim italic"
        ) if len(capped_result) > _cap else None
        capped_result = capped_result[:_cap]
        if ansi_result:
            result_body = Text.from_ansi(capped_result)
        else:
            result_body = render_md(f"```\n{ansi_plain(capped_result)}\n```")
        return Group(
            Text.assemble((f"{glyph('tool')} {name}", "bold dim"), (f"   {glyph('hint_collapse')}", "dim italic")),
            Text("input", style="dim italic"),
            render_md(f"```\n{shown_inp}\n```"),
            Text("result", style="dim italic"),
            result_body,
            *([tail_note] if tail_note else []),
            Text(glyph("rule") * 40, style="dim"),
        )
    plain_result = ansi_plain(str(result))
    first_line = plain_result.splitlines()[0] if plain_result else plain_result
    truncated = len(first_line) > 40 or "\n" in plain_result
    preview = first_line[:40] + ("…" if truncated else "")
    return Text(f"{glyph('tool')} {name} → {preview} ({glyph('hint_expand')})", style="dim")


def _tool_call_bash(msg, r):
    return _tool_call(msg, r, ansi_result=True)


def _tool_call_edit(msg, r):
    if msg.get("result") is None or not msg.get("expanded"):
        return _tool_call(msg, r)
    inp = msg.get("input", {})
    fp = inp.get("file_path", "")
    old = inp.get("old_string", "")
    new = inp.get("new_string", "")
    parts = [Text.assemble((f"{glyph('tool')} {msg['name']}", "bold dim"), (f"   {glyph('hint_collapse')}", "dim italic"))]
    if fp:
        parts.append(Text(fp, style="dim"))
    if old or new:
        parts.append(Group(*cap_diff_lines(build_diff_lines(old, new, fp))))
    parts.append(Text(glyph("rule") * 40, style="dim"))
    return Group(*parts)


def _approval_frame(msg, body):
    name = msg["name"]
    hint = glyph("hint_collapse") if msg.get("expanded") else glyph("hint_view_full")
    warn = theme_store_.get("warn")
    header = Text.assemble(
        (f"{glyph('approval')} ", f"bold {warn}"), (name, f"bold {warn}"),
        (f"   {glyph('approval_keys')} · {hint}", "dim"),
    )
    return Group(header, body, Text(glyph("rule") * 40, style="dim"))


def _approval_bash(msg, r):
    inp = msg.get("input", {})
    code = inp.get("command", str(inp))
    if msg.get("expanded"):
        body = render_md(f"```bash\n{cap_lines(code)}\n```")
    else:
        first_line = code.splitlines()[0] if code else code
        preview = first_line[:80] + ("…" if len(first_line) > 80 or "\n" in code else "")
        body = Text(preview, style="dim")
    return _approval_frame(msg, body)


def _approval_edit(msg, r):
    inp = msg.get("input", {})
    fp = inp.get("file_path", "")
    old = inp.get("old_string", "")
    new = inp.get("new_string", "")
    if msg.get("expanded"):
        parts = []
        if fp:
            parts.append(Text(fp, style="dim"))
        if old or new:
            parts.append(Group(*cap_diff_lines(build_diff_lines(old, new, fp))))
        body = Group(*parts) if parts else Text(str(inp))
    else:
        body = Text(fp or str(inp)[:80], style="dim")
    return _approval_frame(msg, body)


def _approval_write(msg, r):
    inp = msg.get("input", {})
    fp = inp.get("file_path", "")
    content = inp.get("content", "")
    if msg.get("expanded"):
        parts = []
        if fp:
            parts.append(Text(fp, style="dim"))
        if content:
            preview = content[:500] + ("…" if len(content) > 500 else "")
            parts.append(render_md(f"```\n{cap_lines(preview)}\n```"))
        body = Group(*parts) if parts else Text(str(inp))
    else:
        label = f"{fp} ({len(content)} chars)" if fp else str(inp)[:80]
        body = Text(label, style="dim")
    return _approval_frame(msg, body)


def _approval(msg, r):
    inp = msg.get("input", {})
    if msg.get("expanded"):
        body = render_md(f"```\n{cap_lines(str(inp))}\n```")
    else:
        body = Text(str(inp)[:80] + "…", style="dim")
    return _approval_frame(msg, body)


BUILTIN = {
    "user": _user,
    "user_queued": _user_queued,
    "text": _text,
    "thinking": _thinking,
    "tool_call": _tool_call,
    "tool_call:bash_": _tool_call_bash,
    "tool_call:edit_": _tool_call_edit,
    "error": _error,
    "approval": _approval,
    "approval:bash_": _approval_bash,
    "approval:edit_": _approval_edit,
    "approval:write_": _approval_write,
}

# --- dispatch ------------------------------------------------------------

_helpers: SimpleNamespace | None = None


def _keys(msg) -> list[str]:
    t = msg.get("type")
    name = msg.get("name")
    return [f"{t}:{name}", t] if name and t in ("tool_call", "approval") else [t]


def _builtin(e):
    for k in _keys(e["msg"]):
        fn = BUILTIN.get(k)
        if fn is not None:
            return fn(e["msg"], e["r"])
    return Text("")


def render_msg(msg: dict):
    """Rich renderable for a message: mod chain around the builtin renderer."""
    global _helpers
    if _helpers is None:
        _helpers = helpers()
    t = msg.get("type")
    tool = msg.get("name") if t in ("tool_call", "approval") else None
    if mods_.has("render", component="message", type=t, tool=tool):
        msg = copy.deepcopy(msg)  # a mod editing e["msg"] in place must not touch the stored row
    out = mods_.dispatch("render", {"msg": msg, "r": _helpers}, _builtin, component="message", type=t, tool=tool)
    return Text("") if out is None else out
