"""Background pollers for MicroTui (bgproc, memory review, schedule, subagents); module functions taking the app."""

import asyncio
import os

from rich.markup import escape as _markup_escape

from micro_cc import mods_
from micro_cc.tui_native.glyphs_ import glyph
from micro_cc.utils import theme_store_
from micro_cc.utils.msg_store_ import load_memory_review_recap


def poll_bgprocs_tick(app) -> None:
    """Re-render bg process and watch pills, skipping pids shown as tracked subagents."""
    from micro_cc.tools.bash_tool import list_background_processes
    from micro_cc.tools.monitor_watch_ import list_watches
    from micro_cc.utils import subagent_tracker_

    tracked = subagent_tracker_.read_tracked(app._project_dir)["subagents"]
    subagent_pids = {
        entry["pid"] for entry in tracked.values() if entry.get("pid") is not None
    }
    procs = [
        p for p in list_background_processes() if p["pid"] not in subagent_pids
    ]
    watches = list_watches()
    if app._bgproc_viewing:
        app._refresh_bgproc_detail(procs, watches)

    if not procs and not watches:
        app.bgproc_status.update("")
        app.request_render()
        return

    # A stalled process (output stopped, likely at an interactive prompt) gets its own glyph/color.
    lines = [
        f"[{theme_store_.get('warn')}]{glyph('stalled')} {p['pid']} · {p['age']}s · stalled[/{theme_store_.get('warn')}]" if p.get("stalled")
        else f"{glyph('bgproc')} {p['pid']} · {p['age']}s"
        for p in procs
    ]
    lines += [f"{glyph('watch')} {w['watch_id']} · {w['age']}s" for w in watches]
    if lines:
        lines[0] += "  [dim]· ctrl+b to view[/dim]"
    app.bgproc_status.update("\n".join(app._cap_lines(lines)))
    app.request_render()

def poll_memory_review_tick(app) -> None:
    """Flash the latest memory-review recap when a new one lands (the review runs detached)."""
    recap = load_memory_review_recap(app._project_dir)
    if recap is None or recap["ts"] == app._last_memory_review_ts:
        return
    app._last_memory_review_ts = recap["ts"]
    # Escape: the model's recap is untrusted Rich markup; a stray "[" raises MarkupError in render.
    app._flash_memory_review(f"[italic]✎ memory reviewed — {_markup_escape(recap['recap'])}[/italic]")

async def poll_subagents_tick(app, *, cold_start: bool = False) -> None:
    """Poll subagents every 3s, display status, fire wakeups on checkpoint."""
    from micro_cc.utils import subagent_tracker_
    from micro_cc.tools.monitor_ import poll_checkpoints, format_status_glyph, _DONE_STATUSES

    rows = poll_checkpoints(app._project_dir, app._subagent_poll_cache)
    app._tracked_subagents = [r["target"] for r in rows]

    if not rows:
        app._subagent_snapshot = []
        app.subagent_status.update("")
        app.request_render()
        return

    display_lines = []
    snapshot = []
    for r in rows:
        target, entry, info, status = r["target"], r["entry"], r["info"], r["status"]
        if status in _DONE_STATUSES and entry["notified"]:
            # Already notified: hide from the status line, prune() reaps it.
            continue

        name = (
            info["name"]
            if info is not None
            else (os.path.basename(target.rstrip("/")) or target)
        )
        pending = info["pending"] if info is not None else 0
        # tokens only refresh on a real change; .get because the synthetic died-info has no tokens.
        tokens = info.get("tokens") if info is not None else None
        snapshot.append({"target": target, "name": name, "status": status, "pending": pending,
                         "tokens": tokens, "pid": entry.get("pid"),
                         "viewing": target == app._subagent_viewing_target})
        # Leading glyph marks which target is currently swapped into the message area.
        viewing_glyph = "●" if target == app._subagent_viewing_target else "○"
        display_lines.append(f"{viewing_glyph} {format_status_glyph(name, status, pending, tokens)}")

    app._subagent_snapshot = snapshot
    mods_.bump()  # pane renders reading api.subagents() are cached per generation
    # Viewing decoration composed here; format_status_glyph stays UI-agnostic.
    if display_lines:
        boss_glyph = "●" if app._subagent_viewing_target is None else "○"
        # Only on-screen hint for shift+tab.
        all_lines = [f"⚙ {boss_glyph} boss  [dim]· shift+tab to view[/dim]"] + [f"⚙ {line}" for line in display_lines]
        app.subagent_status.update("\n".join(app._cap_lines(all_lines)))
    else:
        app.subagent_status.update("")
    app.request_render()

    woken = [r for r in rows if r["wake"]]
    if woken:
        await wake_for_subagents(app, woken, cold_start=cold_start)
        for r in woken:
            subagent_tracker_.mark_notified(app._project_dir, r["target"])

    # Reap every tick; prune only drops entries already notified, so it can't race mark_notified.
    subagent_tracker_.prune(app._project_dir)

async def wake_for_subagents(app, rows: list, *, cold_start: bool = False) -> None:
    """Wake boss once for every subagent checkpoint it hasn't seen, like an incoming message."""
    from micro_cc.tools.monitor_ import checkpoint_turn

    await app._inject_incoming_turn(checkpoint_turn(rows, cold_start=cold_start))

async def subagent_poll_loop(app) -> None:
    while True:
        await asyncio.sleep(3.0)
        await poll_subagents_tick(app)

async def bgproc_poll_loop(app) -> None:
    while True:
        await asyncio.sleep(3.0)
        poll_bgprocs_tick(app)

async def memory_review_poll_loop(app) -> None:
    while True:
        await asyncio.sleep(3.0)
        poll_memory_review_tick(app)

# --- scheduled prompts (tools/monitor_schedule_) -------------------------
def ensure_schedule_tick(app) -> None:
    """Start the schedule tick if not already running (idempotent)."""
    if app._schedule_poll_timer is not None and not app._schedule_poll_timer.done():
        return
    app._schedule_poll_timer = asyncio.create_task(schedule_poll_loop(app))

async def schedule_poll_loop(app) -> None:
    # 5s is well under the 1-minute cron floor.
    while True:
        await asyncio.sleep(5.0)
        poll_schedule_tick(app)

def poll_schedule_tick(app) -> None:
    """Fire every due schedule; a bad spec must not kill the tick task."""
    from micro_cc.tools import monitor_schedule_runtime as _schedule_rt

    try:
        _schedule_rt.tick(app._project_dir)
    except Exception as e:
        app._show_error_row(f"schedule tick crashed: {type(e).__name__}: {e}")
