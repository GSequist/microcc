"""Boss-side persisted record of which headless subagents are worth watching.

One file per boss session, in the boss's OWN storage dir (never the
subagent's): ~/.micro-cc/projects/{boss}_{hash}/tracked_subagents.json,
shaped {"subagents": {project_dir: {"status": ..., "notified": ..., "updated_at": ...}}}.

Three concerns live on each entry, independently, instead of being inferred
from list membership:
  1. Is it worth showing right now? — a pure predicate over `status`
     (non-terminal, or terminal-but-not-yet-notified).
  2. Has boss already been told? — the `notified` flag, flipped once under
     the same file lock as the wakeup that reports it.
  3. When do we free the entry? — `prune`, a fully separate, low-stakes pass
     that only ever touches entries where (1) and (2) are already resolved.

Populated only by start_headless_.py's self-registration on process startup
(`add_tracked`), off MICROCC_CALLER_PROJECT_DIR — which bash_ stamps on every
subprocess, so a boss-spawned run always registers itself. monitor_ is
read-only: checking a dir never creates an entry.

This is what survives both /clear (a different file, untouched by
cmd_clear's erase_msgs/erase_summary/clear_todos) and a full microcc
restart (on-disk, read back in on_mount) — see start_live_.py's on_mount.

fcntl.flock-guarded on a sidecar lock file, same pattern inbox_store_.py
uses — writers here are separate OS processes (each `microcc-headless`
self-registering is its own PID, not a thread in boss's own process), so an
in-process lock can't serialize them. Reproduced directly once: 8 real
subprocesses calling add_tracked concurrently on a fresh tracker dropped
entries and crashed with FileNotFoundError (two processes' _write both
targeting the same hardcoded .tmp path, one process's os.replace source
vanishing mid-race out from under the other). Per-PID tmp filenames plus
fcntl.flock around the whole read-modify-write closes both holes.
"""

import fcntl
import json
import os
import time
from pathlib import Path

from micro_cc.utils.msg_store_ import _get_storage_dir


def _tracker_path(caller_project_dir: str) -> Path:
    return _get_storage_dir(caller_project_dir) / "tracked_subagents.json"


def _lock_path(caller_project_dir: str) -> Path:
    return _get_storage_dir(caller_project_dir) / "tracked_subagents.lock"


def _with_lock(caller_project_dir: str, fn):
    lock_path = _lock_path(caller_project_dir)
    with open(lock_path, "a+") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def _read(caller_project_dir: str) -> dict:
    path = _tracker_path(caller_project_dir)
    if not path.exists():
        return {"subagents": {}}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {"subagents": {}}

    if "project_dirs" in data and "subagents" not in data:
        # One-time, in-memory upgrade from the pre-v2 shape. Defaults
        # notified=False for every migrated entry even if it logically
        # already got its wakeup under the old scheme — worst case is one
        # redundant re-notification per pre-existing tracked subagent,
        # which is far safer than guessing True and silently dropping a
        # real pending notification. The next _write persists the new
        # shape, so the file self-heals within one read/write cycle.
        last_status = data.get("last_status", {})
        now = time.time()
        data = {"subagents": {
            pd: {"status": last_status.get(pd, "RUNNING"), "notified": False, "updated_at": now}
            for pd in data["project_dirs"]
        }}

    data.setdefault("subagents", {})
    return data


def _write(caller_project_dir: str, data: dict) -> None:
    path = _tracker_path(caller_project_dir)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


# Genuine dead-ends — the headless process has fully exited and nothing more
# will ever happen to it. PAUSED/NEEDS_INPUT are deliberately excluded here:
# those also exited, but still expect a relaunch, so they're not prune-
# eligible the way DONE/FAILED are.
_TERMINAL_STATUSES = {"DONE", "FAILED"}


class NameInUse(Exception):
    """add_tracked refused: `subagent_project_dir` already has a live
    RUNNING run. Without this, two concurrent microcc-headless invocations against
    the same project_dir would silently share one tracker entry AND one
    messages.jsonl conversation history — a real corruption risk, not just a
    bookkeeping one. See add_tracked."""


class CapReached(Exception):
    """add_tracked(max_running=N) refused: N other subagents of this
    boss are already live. Checked under the same flock as the write, so N
    simultaneous spawns can't all read "N-1 running" and all register."""


def _pid_alive(pid) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def add_tracked(
    caller_project_dir: str, subagent_project_dir: str, *,
    max_running: int | None = None,
) -> None:
    """start_headless_.py's self-registration — a microcc-headless process
    announcing "I am starting a run right now," including a legitimate
    relaunch of a project_dir that already finished once before.
    (Over)writes a brand-new RUNNING/not-notified entry, which is what lets a
    relaunch notify again on its second completion — UNLESS
    `subagent_project_dir` is currently claimed by another still-alive
    RUNNING process, in which case this raises NameInUse instead of
    silently clobbering it. The liveness check and the write happen under
    the same flock (_with_lock), so this is race-free against another
    microcc-headless process doing the same check concurrently. A stale
    entry (dead pid — crashed without ever updating status) is still safely
    reclaimed.

    max_running: refuse with CapReached if that many OTHER subagents of this
    boss are live — same flock as the NameInUse check.
    """
    def _do():
        data = _read(caller_project_dir)
        subagents = data["subagents"]
        if max_running is not None:
            running = [
                pd for pd, e in subagents.items()
                if pd != subagent_project_dir and e.get("status") == "RUNNING"
                and e.get("pid") is not None and _pid_alive(e["pid"])
            ]
            if len(running) >= max_running:
                names = ", ".join(os.path.basename(pd.rstrip("/")) or pd for pd in running)
                raise CapReached(
                    f"{len(running)} subagents already running ({names}) — limit is "
                    f"{max_running} (the user can change it with /subagents). Wait for one to "
                    f"finish (you get a checkpoint wakeup), or stop one, then spawn again."
                )
        existing = subagents.get(subagent_project_dir)
        if existing and existing.get("status") == "RUNNING" and existing.get("pid") is not None:
            try:
                os.kill(existing["pid"], 0)
            except ProcessLookupError:
                pass  # stale entry (crashed without updating status) — safe to reclaim
            else:
                raise NameInUse(
                    f"'{subagent_project_dir}' already has a run in progress "
                    f"(pid {existing['pid']}) — wait for it to finish, or check "
                    f"monitor_ before relaunching."
                )
        subagents[subagent_project_dir] = {
            "status": "RUNNING", "notified": False, "updated_at": time.time(),
            # This runs inside the microcc-headless process itself (see
            # start_headless_.py's run_headless), so os.getpid() here IS
            # the subagent for its whole lifetime — lets the ambient
            # poller (start_live_.py's _poll_subagents_tick) tell "still
            # actually running" apart from "killed/crashed before ever
            # writing a terminal STATUS line," which would otherwise
            # leave the entry stuck RUNNING forever (nothing else ever
            # updates status for a process that's dead).
            "pid": os.getpid(),
        }
        _write(caller_project_dir, data)
    _with_lock(caller_project_dir, _do)


def read_tracked(caller_project_dir: str) -> dict:
    return _read(caller_project_dir)


def update_status(caller_project_dir: str, subagent_project_dir: str, status: str) -> None:
    """Called only from _poll_subagents_tick when a fresh read detects a
    changed status. Sets status (and refreshes updated_at); never touches
    notified."""
    def _do():
        data = _read(caller_project_dir)
        entry = data["subagents"].setdefault(
            subagent_project_dir, {"status": status, "notified": False},
        )
        entry["status"] = status
        entry["updated_at"] = time.time()
        _write(caller_project_dir, data)
    _with_lock(caller_project_dir, _do)


def mark_notified(caller_project_dir: str, subagent_project_dir: str) -> None:
    """Called only from _poll_subagents_tick, right after firing a wakeup
    for this entry's current (terminal) status."""
    def _do():
        data = _read(caller_project_dir)
        entry = data["subagents"].get(subagent_project_dir)
        if entry is not None and not entry.get("notified"):
            entry["notified"] = True
            _write(caller_project_dir, data)
    _with_lock(caller_project_dir, _do)


def prune(caller_project_dir: str, *, max_age_s: float | None = None) -> None:
    """Drop entries that are fully resolved: status is terminal (DONE/
    FAILED) and notified is true, optionally only once updated_at is older
    than max_age_s. Safe to call from anywhere, anytime (e.g. once per
    on_mount, or every Nth poll tick) — an entry only ever becomes
    prune-eligible after both the "worth showing" and "boss was told"
    questions are already resolved, so pruning can never race either of
    them."""
    def _do():
        data = _read(caller_project_dir)
        now = time.time()
        changed = False
        for target in list(data["subagents"]):
            entry = data["subagents"][target]
            if entry.get("status") in _TERMINAL_STATUSES and entry.get("notified"):
                if max_age_s is None or (now - entry.get("updated_at", now)) >= max_age_s:
                    del data["subagents"][target]
                    changed = True
        if changed:
            _write(caller_project_dir, data)
    _with_lock(caller_project_dir, _do)
