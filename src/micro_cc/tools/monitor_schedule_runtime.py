"""Runtime for scheduled prompts (tools/monitor_schedule_.py): the tick that
notices a due schedule and pushes its prompt as a new turn.

Split from the pure schedule store so the store stays importable/testable
without a running event loop, and so the "is anything listening" question
lives in one place — the same shape monitor_watch_ uses for its own pushes.

A schedule only fires inside the live interactive TUI: the tick's push goes to
`_wake_callback`, which start_live_tui_.start() sets to the same
_on_monitor_event handler a watch line uses. Headless/batch runs never set it
(one turn, then exit — no idle loop to receive a push), so a due schedule
there is simply not fired; the task stays stored. That matches monitor_watch_'s
own "no interactive session is listening" refusal, just quieter (a schedule is
durable data, not a subprocess that would leak).

The tick is driven by the TUI's own poll loop (start_live_tui_._schedule_poll_
loop) rather than an asyncio task created here, so there is exactly one owner
of the timer and stop() cancels it like every other poll timer. ensure_running
exists only so monitor_(action="schedule") can nudge the TUI to (re)start the
tick if a schedule is added after startup — it is a no-op when no session owns
the callback.
"""

import time

# Set once by start_live_tui_.start() — a plain sync callable taking the
# formatted content string to inject as the next turn. None means no
# interactive session is listening (headless/batch, or before start()).
_wake_callback = None

# Ids already fired this process, so a tick that lands while the previous
# fire's turn is still queued can't double-fire the same one-shot before
# fire_schedule's delete lands. Cleared on remove/re-arm.
_fired_this_process: set = set()

# Called by ensure_running to (re)start the TUI's tick task. Set by
# start_live_tui_.start(). None => no TUI (nothing to start).
_start_tick = None


def set_wake_callback(cb) -> None:
    global _wake_callback
    _wake_callback = cb


def set_start_tick(fn) -> None:
    """Register the TUI's 'ensure the schedule tick task is running' hook."""
    global _start_tick
    _start_tick = fn


def has_listener() -> bool:
    return _wake_callback is not None


def ensure_running(project_dir: str) -> None:
    """Nudge the TUI to start its schedule tick if a session is up. No-op
    otherwise (headless/batch) — the schedule is still stored, it just can't
    fire until an interactive session arms it."""
    if _start_tick is not None:
        _start_tick()


def tick(project_dir: str, now: float | None = None) -> list:
    """Fire every due schedule for project_dir. Returns the prompts fired, for
    callers/tests that want to observe without a live callback.

    Firing order: advance/delete the task FIRST (fire_schedule), then push —
    so if the push triggers a turn that immediately re-schedules or cancels,
    the store is already consistent, and a one-shot can't be pushed twice if
    the push path raises.
    """
    from micro_cc.tools import monitor_schedule_ as sched

    if _wake_callback is None:
        return []

    now = time.time() if now is None else now
    fired = []
    for task in sched.due_schedules(project_dir, now):
        if task["id"] in _fired_this_process and not task.get("recurring", True):
            # A one-shot already pushed but not yet deleted (delete can lag a
            # tick if the store write failed) — never push it twice.
            continue
        sched.fire_schedule(project_dir, task, now)
        if not task.get("recurring", True):
            # Only one-shots need the double-fire guard — they're deleted on
            # fire and a lagging store write could let the next tick see them
            # again. A recurring task is expected to appear every period.
            _fired_this_process.add(task["id"])

        label = task.get("description") or task.get("prompt", "")[:60]
        content = (
            f"[schedule: {label}]\n{task['prompt']}"
        )
        _wake_callback(content)
        fired.append(task["prompt"])
    return fired


def forget(task_id: str) -> None:
    """Drop a fired-id marker — called when a task is removed/unscheduled so
    the set doesn't grow unbounded across a long session."""
    _fired_this_process.discard(task_id)


def reset() -> None:
    """Test/teardown helper — clear the fired-id set."""
    _fired_this_process.clear()
