_DEFAULT_STATUS_SCRIPT = """#!/usr/bin/env python3
# ~/.micro-cc/statusline.sh — defines micro-cc's status bar (the line under
# the input box). Whatever this script prints to stdout becomes that line,
# verbatim (Rich markup). Edit it freely — there is no other definition of the status bar.
#
# You get one JSON object on stdin: {"tokens": {input,output,trimmed,max,budget},
# "project_dir": "..."} — that's the only stuff that's genuinely live in the
# running app and can't be read any other way. For anything else you want to
# show, read it yourself:
#   ~/.micro-cc/settings.json  -> {"model", "dangerous": [...], "tokens_budget"}
#   ~/.micro-cc/memory.json    -> long-term memory entries (keyed dict)
# Keep it fast (~0.3s timeout) and keep output to one line.
import json, os, sys

ctx = json.load(sys.stdin)
t = ctx.get("tokens", {})
project_dir = ctx.get("project_dir", os.getcwd())

settings = {}
try:
    with open(os.path.expanduser("~/.micro-cc/settings.json")) as f:
        settings = json.load(f)
except Exception:
    pass

SEP = " [dim]\\u00b7[/dim] "


def k(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


parts = [f"model {settings.get('model', '?')}"]
if t.get("input"):
    budget = t.get("budget") or settings.get("tokens_budget") or 0
    if budget:
        pct = min(t["input"] / budget, 1)
        bar, rest = "=" * round(pct * 10), "-" * (10 - round(pct * 10))
        parts.append(f"[dim]ctx[/dim] {bar}[dim]{rest} {k(t['input'])}/{k(budget)} ({pct:.0%})[/dim]")
    else:
        parts.append(f"[dim]ctx {k(t['input'])}[/dim]")
    parts.append(f"[dim]out {k(t.get('output', 0))}[/dim]")
    if t.get("trimmed"):
        parts.append(f"[dim]cut {k(t['trimmed'])}[/dim]")
dangerous = settings.get("dangerous") or []
if dangerous:
    parts.append(f"[dim]! {len(dangerous)} gated[/dim]")
print(SEP.join(parts))
"""
