"""Background pollers for MicroTui (bgproc, memory review, schedule, subagents); module functions taking the app."""

import asyncio
import os

from rich.markup import escape as _markup_escape

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
    from micro_cc.tools.monitor_ import poll_status, format_status_glyph
    from micro_cc.utils.inbox_store_ import peek_pending_count

    data = subagent_tracker_.read_tracked(app._project_dir)
    subagents = data["subagents"]
    app._tracked_subagents = list(subagents)

    if not subagents:
        app.subagent_status.update("")
        app.request_render()
        return

    display_lines = []
    wakeups = []
    for target, entry in subagents.items():
        cached = app._subagent_poll_cache.get(target)
        info, new_stat = poll_status(target, cached)
        app._subagent_poll_cache[target] = new_stat

        if info is None:
            # Unchanged since last poll: reuse the last known status.
            status = entry["status"]
            if status not in app._SUBAGENT_CHECKPOINT_STATUSES:
                # A killed subagent never writes a terminal STATUS, so detect it via pid liveness.
                pid = entry.get("pid")
                if pid is not None:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        name = os.path.basename(target.rstrip("/")) or target
                        info = {
                            "resolved": target,
                            "name": name,
                            "status": "FAILED",
                            "summary": "process is no longer running (stopped or killed before finishing)",
                            "pending": peek_pending_count(target),
                        }
                        status = info["status"]
                        subagent_tracker_.update_status(
                            app._project_dir, target, status
                        )
                        if not entry["notified"]:
                            wakeups.append((target, info))
                    except PermissionError:
                        pass  # pid got reused by something we don't own — can't tell, leave as-is
        else:
            status = info["status"]
            if status != entry["status"]:
                subagent_tracker_.update_status(app._project_dir, target, status)
            if (
                status in app._SUBAGENT_CHECKPOINT_STATUSES
                and not entry["notified"]
            ):
                wakeups.append((target, info))

        if status in app._SUBAGENT_TERMINAL_STATUSES and entry["notified"]:
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
        # Leading glyph marks which target is currently swapped into the message area.
        viewing_glyph = "●" if target == app._subagent_viewing_target else "○"
        display_lines.append(f"{viewing_glyph} {format_status_glyph(name, status, pending, tokens)}")

    # Viewing decoration composed here; format_status_glyph stays UI-agnostic.
    if display_lines:
        boss_glyph = "●" if app._subagent_viewing_target is None else "○"
        # Only on-screen hint for shift+tab.
        all_lines = [f"⚙ {boss_glyph} boss  [dim]· shift+tab to view[/dim]"] + [f"⚙ {line}" for line in display_lines]
        app.subagent_status.update("\n".join(app._cap_lines(all_lines)))
    else:
        app.subagent_status.update("")
    app.request_render()

    for target, info in wakeups:
        await app._wake_for_subagent(info, cold_start=cold_start)
        subagent_tracker_.mark_notified(app._project_dir, target)

    # Reap every tick; prune only drops entries already notified, so it can't race mark_notified.
    subagent_tracker_.prune(app._project_dir)

async def wake_for_subagent(app, info: dict, *, cold_start: bool = False) -> None:
    """Wake boss for a subagent checkpoint it hasn't seen, like an incoming message."""
    from micro_cc.tools.monitor_ import format_status_line

    prefix = app._SUBAGENT_COLD_START_PREFIX if cold_start else ""
    content = (
        f"{prefix}[subagent checkpoint]\n{format_status_line(info)}\n\n"
        f"{app._SUBAGENT_WAKEUP_REMINDER}"
    )
    await app._inject_incoming_turn(content)

async def subagent_poll_loop(app) -> None:
    while True:
        await asyncio.sleep(3.0)
        await app._poll_subagents_tick()

async def bgproc_poll_loop(app) -> None:
    while True:
        await asyncio.sleep(3.0)
        app._poll_bgprocs_tick()

async def memory_review_poll_loop(app) -> None:
    while True:
        await asyncio.sleep(3.0)
        app._poll_memory_review_tick()

# --- scheduled prompts (tools/monitor_schedule_) -------------------------
def ensure_schedule_tick(app) -> None:
    """Start the schedule tick if not already running (idempotent)."""
    if app._schedule_poll_timer is not None and not app._schedule_poll_timer.done():
        return
    app._schedule_poll_timer = asyncio.create_task(app._schedule_poll_loop())

async def schedule_poll_loop(app) -> None:
    # 5s is well under the 1-minute cron floor.
    while True:
        await asyncio.sleep(5.0)
        app._poll_schedule_tick()

def poll_schedule_tick(app) -> None:
    """Fire every due schedule; a bad spec must not kill the tick task."""
    from micro_cc.tools import monitor_schedule_runtime as _schedule_rt

    try:
        _schedule_rt.tick(app._project_dir)
    except Exception as e:
        app._show_error_row(f"schedule tick crashed: {type(e).__name__}: {e}")
