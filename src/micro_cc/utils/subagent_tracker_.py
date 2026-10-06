"""Persisted record of live headless subagents with status, notifications, and pruning.
Uses fcntl.flock for safe concurrent writes from separate OS processes."""

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
        # In-memory upgrade from the pre-v2 shape; notified=False can cause one redundant wakeup but never drops one.
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


# Statuses where the process has fully exited and nothing more will happen.
_TERMINAL_STATUSES = {"DONE", "FAILED"}


class NameInUse(Exception):
    """add_tracked refused: subagent_project_dir already has a live RUNNING process."""


class CapReached(Exception):
    """add_tracked refused: max_running subagents already running."""


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
    """Register a starting headless subagent, check for name collisions and running limits."""
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
            "pid": os.getpid(),  # Detect stale RUNNING entries if subagent crashes.
        }
        _write(caller_project_dir, data)
    _with_lock(caller_project_dir, _do)


def read_tracked(caller_project_dir: str) -> dict:
    return _read(caller_project_dir)


def update_status(caller_project_dir: str, subagent_project_dir: str, status: str) -> None:
    """Update subagent status and timestamp without touching notified flag."""
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
    """Mark subagent's terminal status as notified."""
    def _do():
        data = _read(caller_project_dir)
        entry = data["subagents"].get(subagent_project_dir)
        if entry is not None and not entry.get("notified"):
            entry["notified"] = True
            _write(caller_project_dir, data)
    _with_lock(caller_project_dir, _do)


def prune(caller_project_dir: str, *, max_age_s: float | None = None) -> None:
    """Drop terminal, notified entries older than max_age_s."""
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
