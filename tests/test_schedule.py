"""Executable observations for the scheduled-prompt guarantees.

Run directly:  python tests/test_schedule.py   (exit 0 = held, 1 = failed)

Covers design/architecture.toml's schedule contracts:
  * interval and cron specs parse with standard cron field syntax,
    dom/dow OR semantics and human labels;
  * jitter is deterministic and bounded;
  * a one-shot fires once then is deleted, a recurring task reschedules, and a
    recurring task past its max age fires a final time then is deleted;
  * nothing fires without an interactive listener.
Pure module logic against temp dirs — no real ~/.micro-cc state, no network.
"""

import os
import pathlib
import sys
import tempfile
import time
from datetime import datetime

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from micro_cc.tools import monitor_schedule_ as ms  # noqa: E402
from micro_cc.tools import monitor_schedule_runtime as rt  # noqa: E402


def _fail(msg):
    sys.stderr.write(f"FAIL: {msg}\n")
    sys.exit(1)


def test_parsing():
    for spec, want in [("30s", 60), ("5m", 300), ("2h", 7200), ("1d", 86400),
                       ("every 5 minutes", 300), ("every 2 hours", 7200),
                       ("every 30 seconds", 60)]:
        if ms.parse_interval(spec) != want:
            _fail(f"parse_interval({spec!r}) = {ms.parse_interval(spec)}, want {want}")
    for bad in ("nonsense", "5", "every", "every x minutes"):
        if ms.parse_interval(bad) is not None:
            _fail(f"parse_interval({bad!r}) should be None")

    if ms.parse_cron("*/5 * * * *")["minute"] != [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55]:
        _fail("*/5 minute expansion wrong")
    if ms.parse_cron("0 9 * * 1-5")["day-of-week"] != [1, 2, 3, 4, 5]:
        _fail("weekday range wrong")
    if ms.parse_cron("0 0 * * 7")["day-of-week"] != [0]:
        _fail("dow 7 should alias Sunday(0)")
    for bad in ("bad", "60 * * * *", "* * * *"):
        if ms.parse_cron(bad) is not None:
            _fail(f"parse_cron({bad!r}) should be None")

    # next-run: pinned date jumps to next year's month
    nxt = ms._next_cron_dt(ms.parse_cron("30 14 28 2 *"), datetime(2026, 9, 19, 12, 0))
    if (nxt.month, nxt.day, nxt.hour, nxt.minute) != (2, 28, 14, 30):
        _fail(f"pinned cron next-run wrong: {nxt}")
    # weekday 9am from a Saturday -> Monday
    nxt = ms._next_cron_dt(ms.parse_cron("0 9 * * 1-5"), datetime(2026, 9, 19, 12, 0))
    if (nxt.day, nxt.hour) != (21, 9):
        _fail(f"weekday next-run wrong: {nxt}")

    if ms.cron_to_human("*/5 * * * *") != "every 5 minutes":
        _fail("cron_to_human step wrong")
    if ms.cron_to_human("0 9 * * 1-5") != "weekdays at 09:00":
        _fail("cron_to_human weekdays wrong")
    if ms._interval_to_human(3600) != "every 1 hour":
        _fail("interval_to_human wrong")


def test_jitter():
    t = {"id": "abcdef12", "kind": "cron", "spec": "0 * * * *", "created_at": 0,
         "recurring": True, "last_fired_at": None, "max_age_seconds": 0}
    anchor = datetime(2026, 9, 19, 12, 0)
    base = ms._base_next_fire(anchor, t)
    jit = ms._jittered_recurring(anchor, t)
    if base is None or jit < base:
        _fail("recurring jitter must be >= base")
    gap_ms = (ms._base_next_fire(base, t) - base).total_seconds() * 1000
    if (jit - base).total_seconds() * 1000 > min(0.1 * gap_ms, 15 * 60 * 1000) + 1:
        _fail("recurring jitter exceeded its bound")
    if ms._jittered_recurring(anchor, t) != jit:
        _fail("jitter must be deterministic for a given id")


def test_fire_lifecycle():
    d = pathlib.Path(tempfile.mkdtemp())
    ms._get_storage_dir = lambda pd: d
    pd = "/sched_test"
    ms._session_tasks.clear()
    rt.reset()

    # one-shot fires once, then is deleted
    t, _ = ms.add_schedule(pd, "1m", "one shot", "one shot", recurring=False)
    t["created_at"] = time.time() - 120
    ms._session_tasks[pd][-1] = t
    if ms.fire_schedule(pd, t) != t["id"]:
        _fail("one-shot fire should return the deleted id")
    if any(x["id"] == t["id"] for x in ms.load_schedules(pd)):
        _fail("one-shot must be gone after firing")

    # recurring fires, stays, and advances last_fired_at
    t2, _ = ms.add_schedule(pd, "1m", "rec", "rec", recurring=True)
    if ms.fire_schedule(pd, t2) is not None:
        _fail("recurring fire should not delete")
    after = [x for x in ms.load_schedules(pd) if x["id"] == t2["id"]][0]
    if not after.get("last_fired_at") or time.time() - after["last_fired_at"] > 5:
        _fail("recurring fire must stamp last_fired_at")

    # aged-out recurring: fires a final time then is deleted
    t3, _ = ms.add_schedule(pd, "1m", "old", "old", recurring=True, max_age_days=0.00001)
    t3["created_at"] = time.time() - 10
    if not ms.is_aged_out(t3, time.time()):
        _fail("task past max age should be aged out")
    if ms.fire_schedule(pd, t3) != t3["id"]:
        _fail("aged-out recurring should be deleted on fire")

    # durable persists to disk, session-only does not
    ms.add_schedule(pd, "5m", "durable", "durable", durable=True)
    if not (d / "scheduled_prompts.json").exists():
        _fail("durable schedule must write to disk")
    if len(ms.load_schedules(pd)) < 1:
        _fail("durable schedule must load back")


def test_runtime_gate():
    d = pathlib.Path(tempfile.mkdtemp())
    ms._get_storage_dir = lambda pd: d
    pd = "/sched_runtime"
    ms._session_tasks.clear()
    rt.reset()

    pushed = []
    rt.set_wake_callback(lambda c: pushed.append(c))

    t, _ = ms.add_schedule(pd, "1m", "do the thing", "thing", recurring=False)
    t["created_at"] = time.time() - 120
    ms._session_tasks[pd][-1] = t
    fired = rt.tick(pd)
    if fired != ["do the thing"]:
        _fail(f"due one-shot should fire once, got {fired}")
    if not pushed or "[schedule:" not in pushed[0] or "do the thing" not in pushed[0]:
        _fail(f"push content malformed: {pushed}")
    if any(x["id"] == t["id"] for x in ms.load_schedules(pd)):
        _fail("fired one-shot must be removed from the store")

    # no listener => nothing fires (stored, not lost)
    rt._wake_callback = None
    t2, _ = ms.add_schedule(pd, "1m", "x", "x", recurring=False)
    t2["created_at"] = time.time() - 120
    ms._session_tasks[pd][-1] = t2
    if rt.tick(pd) != []:
        _fail("no listener must not fire")
    if not any(x["id"] == t2["id"] for x in ms.load_schedules(pd)):
        _fail("unfired schedule must remain stored")


def main():
    test_parsing()
    test_jitter()
    test_fire_lifecycle()
    test_runtime_gate()
    sys.stderr.write("OK: scheduled-prompt properties held\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
