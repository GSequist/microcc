---
name: customizing-micro-cc
description: Read before changing micro-cc itself (look of the TUI, slash commands, keys, glyphs, status line, models, or harness source). Says where each change must go so it survives updates.
---

# Changing micro-cc

- **Core**: the package source (`micro_cc/`). Every `/update` reinstalls it and **overwrites any edit**.
- **User layer**: `~/.micro-cc/`. Updates never touch it.

Rule: put the change in a mod or a config file below. Edit core only when nothing fits, and tell the user the edit is lost on the next update.

| Want | Put it in |
|---|---|
| Anything on screen: message rows, banner, status/hint lines, extra lines above the input or under the banner, glyphs | a mod |
| New slash command, or wrap a builtin one | a mod |
| Key binding | a mod |
| Status bar text | `statusline.sh` |
| Colors | `theme.json` (or `/theme`) |
| Add or tweak a model | `models.json` |
| Skill | `skills/<name>/SKILL.md` |

## Mods

One folder per mod: `~/.micro-cc/mods/<name>/mod.py` (name: lowercase, digits, `-`, `_`; a leading `_` disables it). Helper files beside it import relatively (`from .parts import x`). Saving any file under `mods/` restarts the app into the change at the next idle moment, conversation kept.

```python
def register(on):
    @on("render", component="above_input")
    def ctx(api, e, nxt):
        u = api.usage()
        return [*nxt(e), f"[dim]◆ {u['input']:,} tokens in[/dim]"]

    @on("command", name="standup", hint="summarize today's commits")
    def standup(api, e, nxt):
        api.send_prompt(f"Summarize `git log --since=midnight`. Focus: {e['arg']}")

    @on("key", key="ctrl+g")
    def go(api, e, nxt):
        api.send_prompt("/standup")
        return True
```

Every handler is `fn(api, e, nxt)`. `nxt(e)` runs the rest of the chain and finally the builtin. Return `nxt(e)` to pass through, pass a changed copy of `e` to transform, wrap the result to decorate, or skip `nxt` to replace.

| Event | Filters | `e` | Return |
|---|---|---|---|
| `render` | `component=` one of `banner, header, above_input, working, question, memory_flash, statusbar, hintbar, subagents, bgprocs` | `markup`, `width` | Rich markup: str or list of lines; `nxt(e)` gives a list |
| `render` | `component="message"`, optional `type=` (`user, user_queued, text, thinking, tool_call, error, approval`), `tool=` (e.g. `"bash_"`) | `msg`, `r` | a Rich renderable |
| `glyphs` | | `{}` | dict: `{**nxt(e), "user_prompt": "❯"}` |
| `command` | `name=`, `hint=` | `name`, `arg`, `line` | anything; may be `async` (then `await nxt(e)`) |
| `key` | `key=` like `ctrl+g` | `data` | `True` if consumed |

- `header` (under the banner) and `above_input` are empty until a mod adds lines.
- `r` has `glyph, color, render_md, diff_lines, cap_lines, cap_diff_lines, ansi_plain, Text, Group`. Read `micro_cc/tui_native/renderers_.py` for the builtin message renderers.
- Glyph names: `user_prompt, queued, thinking, tool_pending, tool, error, approval, approval_keys, hint_expand, hint_collapse, hint_view_full, rule, spinner, bgproc, watch, stalled`.
- A command with a builtin's name wraps it: `nxt(e)` runs the builtin.
- `ctrl+c`, `escape` and `enter` can't be bound.
- Component renders are cached; they re-run when the content or width changes, at each status refresh, or on `api.refresh()`.

### `api`, the only object a mod gets
`notify(text)`, `get_input()`, `set_input(text)`, `send_prompt(text)`, `usage()` (token stats dict), `model()`, `project_dir`, `refresh()`, `request_render()`. Never reach into app internals: they change between releases. Handlers run on the UI event loop and must return fast: over 100 ms disables the mod, and one stuck for 0.5 s is interrupted and disabled. `async` command handlers aren't interrupted, so `await` instead of blocking. Start slow work with `send_prompt` or a background thread.

### Failures
A mod that fails to import, raises, is too slow, or returns invalid markup is disabled as a whole and the builtin shows. Problems are flashed at startup (`mods: N notice(s)`), never crash the app. Test a mod before relying on the restart: `python3 -c "import sys; sys.path.insert(0, '<mods dir>/<name>'); import mod"`.

### Old customizations
Files from the earlier layout (`commands/`, `renderers/`, `panels/`, `keys.json`, `glyphs.json`, `banner.txt`) were moved to `~/.micro-cc/legacy/` and no longer load. When asked, port them into a mod.

## statusline.sh
If absent, the builtin default runs from `cache/statusline.default.sh` (harness-owned, never edit). To customize: copy it to `~/.micro-cc/statusline.sh`, `chmod +x`, edit. It gets JSON on stdin (`tokens`, `project_dir`) and prints one line. No restart needed. A mod can still wrap its output via `component="statusbar"`.

## models.json
```json
{"default_model": "sonnet-5",
 "models": {"sonnet-5": {"context_window": 200000},
            "my-proxy-model": {"litellm": "openai.my-model", "thinking": false,
                               "summarized_display": false, "context_window": 128000, "max_output": 16000}}}
```
An existing alias merges field by field; a new alias needs a backend id (`anthropic`, `foundry`, `litellm` or `openai`) plus `thinking`, `summarized_display`, `context_window`, `max_output`. Invalid entries are skipped. Layers: builtin (offline fallback) < GitHub `models.json` (cached in `cache/models.json`, refreshed in the background at start, applies on next start) < this file.

## Editing core
Only when nothing above fits (agent loop, tools, providers, input parsing). Tell the user it will be overwritten by the next `/update`, and suggest reporting it upstream if generally useful. Never edit `self_heal_.py`, `mods_.py` or the update/reload code (`utils/self_reload_.py`, `tui_native/self_reload_ui_.py`, `_run_update` in `tui_native/screen_cmds_.py`): they recover a broken install.
