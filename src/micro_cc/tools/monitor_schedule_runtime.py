"""Runtime for scheduled prompts: the tick that pushes a due schedule as a new turn.

Fires only in the live TUI (_wake_callback is set by start_live_tui_.start()). Headless
runs leave the task stored and unfired. The tick runs on the TUI poll loop, so it has one owner.
"""

import time

# Set by start_live_tui_.start(); None means no interactive session is listening.
_wake_callback = None

# Ids already pushed this process, so a lagging store delete cannot double-fire a one-shot.
_fired_this_process: set = set()

# Set by start_live_tui_.start(); None means no TUI.
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
    """Nudge the TUI to start its schedule tick; no-op headless."""
    if _start_tick is not None:
        _start_tick()


def tick(project_dir: str, now: float | None = None) -> list:
    """Fire every due schedule for project_dir; returns the prompts fired.
    Advances the store before pushing, so a raising push cannot re-fire a one-shot."""
    from micro_cc.tools import monitor_schedule_ as sched

    if _wake_callback is None:
        return []

    now = time.time() if now is None else now
    fired = []
    for task in sched.due_schedules(project_dir, now):
        if task["id"] in _fired_this_process and not task.get("recurring", True):
            # Already pushed; its store delete may lag a tick.
            continue
        sched.fire_schedule(project_dir, task, now)
        if not task.get("recurring", True):
            # Only one-shots need the guard; recurring tasks reappear every period.
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
