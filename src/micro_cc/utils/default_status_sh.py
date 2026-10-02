_DEFAULT_STATUS_SCRIPT = """#!/usr/bin/env python3
# ~/.micro-cc/statusline.sh — defines micro-cc's status bar (the line under
# the input box). Whatever this script prints to stdout becomes that line,
# verbatim. Edit it freely — there is no other definition of the status bar.
#
# You get one JSON object on stdin: {"tokens": {input,output,trimmed,max},
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

s = f"◈ {settings.get('model', '?')}"
if t.get("input"):
    s += f" | \\u2193 {t['input']:,}"
    if t.get("trimmed"):
        s += f" (\\u2702 {t['trimmed']:,})"
    s += f" \\u2191 {t.get('output', 0):,}"
dangerous = settings.get("dangerous") or []
if dangerous:
    s += f" | \\u26a0 {len(dangerous)} tools gated"
print(s)
"""
