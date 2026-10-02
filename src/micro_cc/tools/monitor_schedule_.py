"""Scheduled prompts: "wake me in 20 minutes", "check the deploy every 5m",
"every weekday at 9 tell me the standup" — the third thing monitor_ watches,
alongside headless subagents and live streaming watches.

Design: a schedule tool plus a cron scheduler, built for micro-cc's
single-process asyncio model:

  * `interval` specs ("30s", "5m", "2h", "1d", or "every 5 minutes") and
    standard 5-field `cron` specs (local time) both compile to a next-fire
    time. cron parsing is a faithful Python port of their `cron.ts` — same
    field syntax (wildcard / N / step / range / list), same day-of-month OR
    day-of-week semantics, same DST behaviour.
  * Anti-herd jitter is ported too: a recurring task fires up to 10% of its
    period late (capped at 15 min), deterministically from its id, so a fleet
    of users asking for "hourly" doesn't all hit the model at :00. One-shots
    landing on :00/:30 fire up to 90s early. Picking an off-minute is still
    the bigger lever (see monitor_'s own prompt text).
  * Recurring tasks auto-expire after `DEFAULT_MAX_AGE_DAYS` (7) — they fire
    one last time, then are deleted. Bounds how long a single session can run
    on its own. `max_age_days=0` means unlimited.

Persistence is per-project (the project's own storage dir, beside
messages.jsonl), so a durable schedule survives a restart AND a self-reload —
the TUI re-arms every stored schedule on start. Session-only schedules
(`durable=False`) live in the module dict and die with the process, matching
CC-oss's durable/session split.

Firing is a push, not a poll: the scheduler's tick calls the same wake
callback monitor_watch_ uses, so a due task arrives as its own turn exactly
like a watch line or a subagent checkpoint. This is why schedules only fire in
the live interactive session — a headless run executes one turn and exits with
no idle loop standing by to receive the push.
"""

import json
import os
import time
import uuid
from datetime import datetime, timedelta

from micro_cc.utils.msg_store_ import _get_storage_dir

# ---------------------------------------------------------------- jitter config
_RECURRING_FRAC = 0.1                 # up to 10% of the period, late
_RECURRING_CAP_MS = 15 * 60 * 1000    # …capped at 15 min
_ONE_SHOT_MAX_MS = 90 * 1000          # one-shot may fire up to 90s early
_ONE_SHOT_MINUTE_MOD = 30             # only jitter one-shots on :00/:30

DEFAULT_MAX_AGE_DAYS = 7
MIN_INTERVAL_SECONDS = 60             # cron/interval floor is 1 minute

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


# ---------------------------------------------------------------- interval specs
def parse_interval(spec: str) -> int | None:
    """'30s' | '5m' | '2h' | '1d' | 'every 5 minutes' -> seconds, else None.
    Sub-minute is rounded up to the 1-minute floor (cron's granularity)."""
    if not spec:
        return None
    s = spec.strip().lower()
    if s.startswith("every "):
        s = s[len("every "):].strip()
        # "5 minutes" / "2 hours" / "1 day" / "30 seconds"
        parts = s.split()
        if len(parts) == 2 and parts[0].isdigit():
            word = parts[1].rstrip("s")
            unit = {"second": "s", "minute": "m", "hour": "h", "day": "d"}.get(word)
            if unit is None:
                return None
            return max(MIN_INTERVAL_SECONDS, int(parts[0]) * _UNIT_SECONDS[unit])
        return None
    # "5m" / "30s" / "2h" / "1d"
    if len(s) >= 2 and s[-1] in _UNIT_SECONDS and s[:-1].isdigit():
        secs = int(s[:-1]) * _UNIT_SECONDS[s[-1]]
        return max(MIN_INTERVAL_SECONDS, secs)
    return None


# ---------------------------------------------------------------- cron specs
# 5-field, local time:
#   minute hour day-of-month month day-of-week
# Field syntax: wildcard, N, */N (step), N-M, N-M/S, comma-lists.
# day-of-week 0=Sunday, 7 accepted as Sunday alias.
_FIELD_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")


def _expand_field(field: str, lo: int, hi: int) -> list | None:
    out = set()
    for part in field.split(","):
        step_m = _re_match(r"^\*(?:/(\d+))?$", part)
        if step_m is not None:
            step = int(step_m[0]) if step_m[0] else 1
            if step < 1:
                return None
            out.update(range(lo, hi + 1, step))
            continue
        rng = _re_match(r"^(\d+)-(\d+)(?:/(\d+))?$", part)
        if rng is not None:
            lo2, hi2, step2 = rng
            step = int(step2) if step2 else 1
            is_dow = (lo == 0 and hi == 6)
            eff_hi = 7 if is_dow else hi
            if int(lo2) > int(hi2) or step < 1 or int(lo2) < lo or int(hi2) > eff_hi:
                return None
            for i in range(int(lo2), int(hi2) + 1, step):
                out.add(0 if (is_dow and i == 7) else i)
            continue
        if _re_match(r"^\d+$", part) is not None:
            n = int(part)
            if lo == 0 and hi == 6 and n == 7:
                n = 0
            if n < lo or n > hi:
                return None
            out.add(n)
            continue
        return None
    if not out:
        return None
    return sorted(out)


def _re_match(pattern: str, text: str) -> tuple | None:
    """Return the tuple of capture groups on match, or None on miss. Callers
    distinguish match-vs-miss by `is not None`; an absent optional group is
    None inside the tuple (callers use `or ''`/truthiness)."""
    import re
    m = re.match(pattern, text)
    return m.groups() if m else None


def parse_cron(expr: str) -> dict | None:
    """5-field cron -> {'minute': [...], ...} or None if invalid."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return None
    expanded = {}
    for i, (lo, hi) in enumerate(_FIELD_RANGES):
        vals = _expand_field(parts[i], lo, hi)
        if vals is None:
            return None
        expanded[_FIELD_NAMES[i]] = vals
    return expanded


def validate_spec(spec: str) -> tuple[str, str] | None:
    """Return (kind, normalized) where kind is 'interval' or 'cron', or None
    if the spec is neither. Normalized is the original spec for cron, or the
    raw spec for interval (kept verbatim for display)."""
    if parse_interval(spec) is not None:
        return ("interval", spec.strip())
    if parse_cron(spec) is not None:
        return ("cron", spec.strip())
    return None


def _next_cron_dt(fields: dict, after: datetime) -> datetime | None:
    """Next datetime strictly after `after` matching the cron fields, in local
    time. Minute-by-minute walk, month/day/hour jumps, bounded at 366 days.
    Ported from cron.ts computeNextCronRun (same DST + dom/dow-OR behaviour)."""
    minute_set = set(fields["minute"])
    hour_set = set(fields["hour"])
    dom_set = set(fields["day-of-month"])
    month_set = set(fields["month"])
    dow_set = set(fields["day-of-week"])

    dom_wild = len(fields["day-of-month"]) == 31
    dow_wild = len(fields["day-of-week"]) == 7

    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if t.month not in month_set:
            # jump to start of next month
            y, m = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
            t = t.replace(year=y, month=m, day=1, hour=0, minute=0)
            continue
        # Python: Monday=0..Sunday=6; cron: Sunday=0..Saturday=6
        dow = (t.weekday() + 1) % 7
        dom = t.day
        if dom_wild and dow_wild:
            day_ok = True
        elif dom_wild:
            day_ok = dow in dow_set
        elif dow_wild:
            day_ok = dom in dom_set
        else:
            day_ok = dom in dom_set or dow in dow_set
        if not day_ok:
            t = (t + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        if t.hour not in hour_set:
            t = (t + timedelta(hours=1)).replace(minute=0)
            continue
        if t.minute not in minute_set:
            t = t + timedelta(minutes=1)
            continue
        return t
    return None


def cron_to_human(expr: str) -> str:
    """Best-effort human label; falls back to the raw cron. Narrow on purpose
    — only the common shapes, same as cron.ts cronToHuman."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return expr
    minute, hour, dom, month, dow = parts
    every_min = _re_match(r"^\*/(\d+)$", minute)
    if every_min and hour == dom == month == dow == "*":
        n = int(minute[2:])
        return "every minute" if n == 1 else f"every {n} minutes"
    if _re_match(r"^\d+$", minute) is not None and hour == dom == month == dow == "*":
        m = int(minute)
        return "every hour" if m == 0 else f"every hour at :{m:02d}"
    every_hour = _re_match(r"^\*/(\d+)$", hour)
    if _re_match(r"^\d+$", minute) is not None and every_hour and dom == month == dow == "*":
        n = int(hour[2:])
        m = int(minute)
        suffix = "" if m == 0 else f" at :{m:02d}"
        return f"every hour{suffix}" if n == 1 else f"every {n} hours{suffix}"
    if _re_match(r"^\d+$", minute) is not None and _re_match(r"^\d+$", hour) is not None:
        m, h = int(minute), int(hour)
        if dom == month == dow == "*":
            return f"every day at {h:02d}:{m:02d}"
        if dom == month == "*" and _re_match(r"^\d$", dow) is not None:
            names = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
            return f"every {names[int(dow) % 7]} at {h:02d}:{m:02d}"
        if dom == month == "*" and dow == "1-5":
            return f"weekdays at {h:02d}:{m:02d}"
    return expr


# ---------------------------------------------------------------- jitter
def _jitter_frac(task_id: str) -> float:
    """Deterministic [0,1) from the id's first 8 hex chars — stable across
    restarts, uniform across tasks. Non-hex ids fall back to 0 (no jitter)."""
    try:
        return int(task_id[:8], 16) / 0x1_0000_0000
    except (ValueError, IndexError):
        return 0.0


def _jittered_recurring(anchor: datetime, task: dict) -> datetime:
    """Next recurring fire: base next-run + deterministic forward delay of up
    to _RECURRING_FRAC of the gap between successive fires, capped. Only
    meaningful for cron tasks (interval tasks have a fixed period, so the
    jitter is just a fraction of that period)."""
    base = _base_next_fire(anchor, task)
    if base is None:
        return None
    # Period estimate: next fire after base.
    after_base = _base_next_fire(base + timedelta(seconds=1), task)
    if after_base is None:
        return base
    gap_ms = (after_base - base).total_seconds() * 1000
    delay_ms = min(_jitter_frac(task["id"]) * _RECURRING_FRAC * gap_ms, _RECURRING_CAP_MS)
    return base + timedelta(milliseconds=delay_ms)


def _jittered_one_shot(anchor: datetime, task: dict) -> datetime:
    """One-shot: base next-run, minus a deterministic lead of up to 90s, but
    only when the fire minute is a :00/:30 boundary (the human-rounding
    hotspots). Off-minute one-shots fire exactly on time."""
    base = _base_next_fire(anchor, task)
    if base is None:
        return None
    if base.minute % _ONE_SHOT_MINUTE_MOD == 0:
        lead_ms = _jitter_frac(task["id"]) * _ONE_SHOT_MAX_MS
        return base - timedelta(milliseconds=lead_ms)
    return base


def _base_next_fire(anchor: datetime, task: dict) -> datetime | None:
    """The un-jittered next fire strictly after `anchor`."""
    if task["kind"] == "interval":
        return anchor + timedelta(seconds=task["interval_seconds"])
    fields = parse_cron(task["spec"])
    if fields is None:
        return None
    return _next_cron_dt(fields, anchor)


def next_fire_at(task: dict, now: float | None = None) -> float | None:
    """Epoch seconds of the next fire, or None if the spec can't produce one.

    Anchor is last_fired_at if the task has fired before, else created_at —
    so a never-fired pinned cron (`30 14 27 2 *`) doesn't jump to next year
    just because the process started late."""
    now = time.time() if now is None else now
    anchor = datetime.fromtimestamp(task.get("last_fired_at") or task["created_at"])
    if task.get("recurring", True):
        nxt = _jittered_recurring(anchor, task)
    else:
        nxt = _jittered_one_shot(anchor, task)
    return nxt.timestamp() if nxt is not None else None


# ---------------------------------------------------------------- store
def _store_path(project_dir: str):
    return _get_storage_dir(project_dir) / "scheduled_prompts.json"


# Session-only tasks (durable=False): process-private, keyed by project_dir.
# Same lifetime rule as monitor_watch_._watches — meaningful only inside the
# live TUI, gone when it exits.
_session_tasks: dict[str, list] = {}


def _read_durable(project_dir: str) -> list:
    path = _store_path(project_dir)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    tasks = data.get("tasks") if isinstance(data, dict) else None
    return [t for t in tasks if isinstance(t, dict) and t.get("id") and t.get("spec")] if tasks else []


def _write_durable(project_dir: str, tasks: list) -> None:
    path = _store_path(project_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"tasks": tasks}, indent=2))
    except OSError:
        pass


def load_schedules(project_dir: str) -> list:
    """Durable tasks from disk + this session's in-memory ones."""
    return _read_durable(project_dir) + _session_tasks.get(project_dir, [])


def add_schedule(
    project_dir: str,
    spec: str,
    prompt: str,
    description: str,
    *,
    recurring: bool = True,
    durable: bool = False,
    max_age_days: float = DEFAULT_MAX_AGE_DAYS,
) -> tuple[dict | None, str]:
    """Create a schedule. Returns (task, error) — task is None on a bad spec.
    `durable=True` writes to the project store (survives restart); the default
    is session-only, matching CC-oss's "most 'remind me in 5 minutes' stay
    session-only" guidance."""
    validated = validate_spec(spec)
    if validated is None:
        return None, (
            f"Invalid schedule spec '{spec}'. Use an interval (30s, 5m, 2h, "
            "1d, 'every 5 minutes') or a 5-field cron expression "
            "('*/5 * * * *', '30 14 * * *')."
        )
    kind, normalized = validated
    task = {
        "id": uuid.uuid4().hex[:8],
        "kind": kind,
        "spec": normalized,
        "prompt": prompt,
        "description": description or prompt[:60],
        "created_at": time.time(),
        "recurring": bool(recurring),
        "durable": bool(durable),
        "max_age_seconds": 0 if max_age_days in (0, None) else max_age_days * 86400,
    }
    if kind == "interval":
        task["interval_seconds"] = parse_interval(normalized)

    if durable:
        tasks = _read_durable(project_dir)
        tasks.append(task)
        _write_durable(project_dir, tasks)
    else:
        _session_tasks.setdefault(project_dir, []).append(task)
    return task, ""


def remove_schedule(project_dir: str, task_id: str) -> bool:
    """Remove a schedule (durable or session). True if something was removed."""
    removed = False
    tasks = _read_durable(project_dir)
    kept = [t for t in tasks if t["id"] != task_id]
    if len(kept) != len(tasks):
        _write_durable(project_dir, kept)
        removed = True
    sess = _session_tasks.get(project_dir, [])
    kept_s = [t for t in sess if t["id"] != task_id]
    if len(kept_s) != len(sess):
        _session_tasks[project_dir] = kept_s
        removed = True
    if removed:
        # Local import: monitor_schedule_runtime's tick() already imports this
        # module locally to avoid a module-level cycle; mirror that direction
        # here instead of adding one back the other way.
        from micro_cc.tools import monitor_schedule_runtime as _rt
        _rt.forget(task_id)
    return removed


def _update_task(project_dir: str, task: dict) -> None:
    """Persist a fired task's new last_fired_at (durable) or replace it in the
    session list. Called by the runtime after a fire."""
    if task.get("durable"):
        tasks = _read_durable(project_dir)
        for i, t in enumerate(tasks):
            if t["id"] == task["id"]:
                tasks[i] = task
                break
        _write_durable(project_dir, tasks)
    else:
        sess = _session_tasks.get(project_dir, [])
        for i, t in enumerate(sess):
            if t["id"] == task["id"]:
                sess[i] = task
                break


def is_aged_out(task: dict, now: float) -> bool:
    """A recurring task past its max age: fires one last time, then is
    deleted. `max_age_seconds == 0` never ages out."""
    max_age = task.get("max_age_seconds", 0)
    if not task.get("recurring", True) or max_age == 0:
        return False
    return (now - task["created_at"]) >= max_age


def due_schedules(project_dir: str, now: float | None = None) -> list:
    """Every schedule whose next fire time has arrived. Pure read — the caller
    (the TUI tick) is responsible for firing and advancing them."""
    now = time.time() if now is None else now
    out = []
    for task in load_schedules(project_dir):
        nxt = next_fire_at(task, now)
        if nxt is not None and now >= nxt:
            out.append(task)
    return out


def fire_schedule(project_dir: str, task: dict, now: float | None = None) -> str | None:
    """Advance a fired task: stamp last_fired_at, then either delete it
    (one-shot, or recurring-but-aged-out) or leave it for the next period.

    Recurring tasks reschedule from `now`, not from their previous next-fire —
    so a tick that lands late (a long turn blocked the loop) can't trigger a catch-up storm of missed fires.

    Returns the task id if it was deleted, else None. Caller fires the prompt.
    """
    now = time.time() if now is None else now
    aged = is_aged_out(task, now)
    if task.get("recurring", True) and not aged:
        task = {**task, "last_fired_at": now}
        _update_task(project_dir, task)
        return None
    # one-shot, or a recurring task that just aged out: fire then delete.
    remove_schedule(project_dir, task["id"])
    return task["id"]


def format_schedule_line(task: dict, now: float | None = None) -> str:
    """One-line description for monitor_'s listing / the model's view."""
    now = time.time() if now is None else now
    if task["kind"] == "interval":
        label = _interval_to_human(task["interval_seconds"])
    else:
        label = cron_to_human(task["spec"])
    kind = "recurring" if task.get("recurring", True) else "one-shot"
    scope = "durable" if task.get("durable") else "session-only"
    nxt = next_fire_at(task, now)
    when = ""
    if nxt is not None:
        delta = max(0, int(nxt - now))
        when = f", next in {_fmt_delta(delta)}"
    return f"{task['id']} · {label} ({kind}, {scope}){when} · {task['description']}"


def _interval_to_human(seconds: int) -> str:
    if seconds % 86400 == 0:
        d = seconds // 86400
        return f"every {d} day{'s' if d != 1 else ''}"
    if seconds % 3600 == 0:
        h = seconds // 3600
        return f"every {h} hour{'s' if h != 1 else ''}"
    m = seconds // 60
    return f"every {m} minute{'s' if m != 1 else ''}"


def _fmt_delta(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d"
