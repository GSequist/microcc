"""Self-reload UI: manifest poll, pending-restart hint and relaunch for MicroTui; module functions taking the app."""

import asyncio
import os

from micro_cc.tui_native.screen_cmds_ import _pending_subagents
from micro_cc.utils import theme_store_
from micro_cc.utils.msg_store_ import store_msgs


def init_self_manifest(app) -> None:
    """Snapshot package source at startup; works on any install type."""
    from micro_cc.utils import self_reload_

    self_reload_.init_baseline()

async def self_poll_loop(app) -> None:
    while True:
        await asyncio.sleep(2.0)
        # Swallow exceptions; unguarded tick would kill loop forever.
        try:
            app._poll_self_change_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

def poll_self_change_tick(app) -> None:
    if app._restarting:
        return
    from micro_cc.utils import self_reload_

    self_reload_.refresh_changes()  # the loop may have refreshed first; compare against the app's view
    files = set(self_reload_.changed_files())
    if files and files != app._reload_changed:
        app._reload_changed = files
        app._reload_pending = True
        app._static_hint_text()  # show the pending hint (hintbar, not statusbar)
        app._flash_status("↻ harness changed — the model will decide when to reload", seconds=5)

    # Restart only once the model asked for it (reload_harness_); drain every tick so a blocked request self-heals.
    if self_reload_.reload_requested() and app._input_mode == "idle":
        app._safe_task(app._maybe_self_reload(), "self reload")

def self_reload_hint(app) -> str:
    """Short hint: one file name, many files count, blocker reason if blocked."""
    from micro_cc.utils import self_reload_

    n = len(app._reload_changed)
    tail = "reloading…" if self_reload_.reload_requested() or app._restarting else "model decides when to reload"
    if n == 0:
        base = f"↻ {tail}"
    elif n == 1:
        base = f"↻ {os.path.basename(next(iter(app._reload_changed)))} — {tail}"
    else:
        base = f"↻ {n} files — {tail}"
    # Name the blocker if any, so stalls report themselves.
    blocker = app._self_reload_blocker()
    if blocker:
        base += f" ({blocker})"
    return base

def self_reload_blocker(app) -> str | None:
    """Reason restart is blocked, or None if safe to restart."""
    if app._restarting:
        return None
    if app._input_mode != "idle":
        return app._input_mode.replace("_", " ")
    current = app._current_query_task
    if current is not None and not current.done():
        return "turn"
    if getattr(app, "_gui_shutdown", None) is not None:
        return "gui"
    if _pending_subagents(app._project_dir):
        return "subagent"
    return None

async def maybe_self_reload(app) -> None:
    """Restart if the model requested it and it is safe; called at turn-boundary points."""
    from micro_cc.utils import self_reload_

    if not self_reload_.reload_requested() or app._restarting:
        return
    # Same gates /exit and /update respect: nothing in flight, no browser/subagent.
    if app._input_mode != "idle":
        return
    # Exclude current task; lets turn-boundary reload fire immediately.
    current = app._current_query_task
    if current is not None and not current.done() and current is not asyncio.current_task():
        return
    if getattr(app, "_gui_shutdown", None) is not None:
        return
    if _pending_subagents(app._project_dir):
        return
    await app._restart_self()

async def restart_self(app, reason: str | None = None) -> None:
    """Relaunch with changed code; stash prompt, tear down terminal, execv."""
    if app._restarting:
        return
    app._restarting = True

    from micro_cc.utils import self_reload_

    self_reload_.save_pending_prompt(app._project_dir, app.prompt.text)

    # Reuse _self_reload_hint so text has one definition, stays in sync.
    msg = reason or app._self_reload_hint()
    app.static_hintbar.update(f"[{theme_store_.get('accent')}]{msg}[/{theme_store_.get('accent')}]")
    app.request_render()
    try:
        # Let frame land before teardown; handle cancellation to avoid wedging.
        await asyncio.sleep(0.6)
    except asyncio.CancelledError:
        app._restarting = False
        raise

    # Flush loop messages so relaunched process reads them back as history.
    if app._loop_msgs is not None:
        store_msgs(app._project_dir, app._loop_msgs)

    await app.stop()
    # execv replaces this process with fresh interpreter running supervisor.
    self_reload_.exec_relaunch()
