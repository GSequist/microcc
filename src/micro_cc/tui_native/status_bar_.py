"""Status bar, hint bar and status flash logic for MicroTui; module functions taking the app."""

import asyncio
import random

from micro_cc import mods_
from micro_cc.utils import hints, settings_store_, statusline_, theme_store_
from micro_cc.utils.msg_store_ import load_checkpoint
from micro_cc.utils.terminal_setup import newline_hint_text
from micro_cc.utils.tokenization_simple import token_stats


def get_random_hint(app):
    # Hint copy shared with GUI; ⌖ is TUI's own decoration.
    return "⌖ " + random.choice(hints.for_surface(hints.TUI))


def static_hint_text(app) -> None:
    # Skip while question is active (bars hidden); _hide_ask_ui will restore.
    if app._input_mode == "question_asked":
        return
    text = f"⏣ {app._shorten_path(app._project_dir, app._PROJECT_DIR_DISPLAY_MAXLEN)}"
    if app._reload_pending and not app._restarting:
        # Reload queued; report delay so user knows something is happening.
        text += f" | [{theme_store_.get('accent')}]{app._self_reload_hint()}[/{theme_store_.get('accent')}]"
    elif app._update_available:
        text += f" | [{theme_store_.get('accent')}]⚠ micro-cc {app._update_available} available — run /update[/{theme_store_.get('accent')}]"
    elif settings_store_.was_reset_for_corruption():
        text += f" | [{theme_store_.get('error')}]⚠ ~/.micro-cc/settings.json was corrupt — reset to defaults[/{theme_store_.get('error')}]"
    else:
        # newline_hint_text() returns None once user learns newlines; fall back to rotation.
        hint = newline_hint_text() or app.get_random_hint()
        hue = app._banner_hue
        text += f" | [{hue}]{hint}[/{hue}]"
    app.static_hintbar.update(
        text[: app._STATIC_HINT_LINE_MAXLEN]
    )
    app.request_render()

def ensure_status_script(app) -> str:
    return statusline_.resolve()

async def status_text(app) -> str:
    script = app._ensure_status_script()
    import json
    payload = json.dumps(
        {"tokens": dict(token_stats), "project_dir": app._project_dir}
    ).encode()
    try:
        proc = await asyncio.create_subprocess_exec(
            script,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(payload), timeout=app._STATUS_LINE_TIMEOUT
        )
        out = stdout.decode().strip()
        if out:
            return out[: app._STATUS_LINE_MAXLEN]
    except Exception:
        pass
    short_dir = app._shorten_path(app._project_dir, app._PROJECT_DIR_DISPLAY_MAXLEN)
    return f"⏣ {short_dir} | ◈ {app._current_model}"  # crash-safety only, not "the default"

# Called at natural stopping points, never during streaming; no debounce needed.
def refresh_status(app):
    # Mods re-render on this cadence; _safe_task guards against exception dumps.
    mods_.bump()
    app._safe_task(app._update_status_bar(), "status bar update")

async def update_status_bar(app):
    # Skip while question is active; _hide_ask_ui will restore.
    if app._input_mode == "question_asked":
        return
    # load_checkpoint() returns None until compaction runs; use app._last_checkpoint_index.
    checkpoint = load_checkpoint(app._project_dir)
    if checkpoint is not None and checkpoint["as_of_index"] != app._last_checkpoint_index:
        app._last_checkpoint_index = checkpoint["as_of_index"]
        # Return to avoid overwriting flash before it renders; timer will restore normal text.
        app._flash_status(
            f"[bold {theme_store_.get('warn')}]✂ compacted past conversation just now[/bold {theme_store_.get('warn')}]",
            seconds=8,
        )
        return

    text = await app._status_text()
    app.statusbar.update(text)
    app.request_render()

def flash_status(app, text: str, seconds: float = 3.0):
    """Show a transient message in the status bar, then restore it."""
    app.statusbar.update(text)
    app.request_render()
    if app._flash_status_timer_task is not None:
        app._flash_status_timer_task.cancel()
    app._flash_status_timer_task = app._call_later(seconds, app._refresh_status)
