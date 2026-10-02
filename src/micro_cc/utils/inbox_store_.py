"""Durable, file-based inbox for message_session_ deliveries to targets that
aren't (or might not be) a live listening process — headless/batch runs.

Local-disk only, deliberately: this backs subagent orchestration, which only
ever happens boss-TUI-side on a local machine (see pg_store_.py/msg_store_'s
Postgres branch, which is for headless-as-cron-in-a-container instead — that
deployment mode never runs this feature, so it doesn't need a Postgres path).

One JSON array per project_dir at .../inbox.json. Writers are separate OS
processes (sibling headless PIDs), not asyncio tasks in one process, so an
in-memory lock can't serialize them — fcntl.flock on a sidecar lock file is
the real cross-process lock. Reads/writes of inbox.json itself go through a
tmp-file + os.replace so a lock-free peek (read_pending_count) never observes
a torn write.
"""

import datetime
import fcntl
import json
import os
from pathlib import Path

from micro_cc.utils.msg_store_ import _get_storage_dir


def _inbox_path(project_dir: str) -> Path:
    return _get_storage_dir(project_dir) / "inbox.json"


def _lock_path(project_dir: str) -> Path:
    return _get_storage_dir(project_dir) / "inbox.lock"


def _atomic_write(path: Path, data: list) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _read(path: Path) -> list:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return []


def _with_lock(project_dir: str, fn):
    lock_path = _lock_path(project_dir)
    with open(lock_path, "a+") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def write_mail(target_project_dir: str, from_dir: str, text: str) -> None:
    """Durably drop a message in target_project_dir's inbox. Delivery just
    means "will be picked up next time that project_dir's headless run
    checks its inbox" (on launch, and at every turn_boundary while running)
    — no live process required on the receiving end."""
    path = _inbox_path(target_project_dir)

    def _do():
        mail = _read(path)
        mail.append({
            "from": from_dir,
            "text": text,
            "timestamp": datetime.datetime.now().isoformat(),
        })
        _atomic_write(path, mail)

    _with_lock(target_project_dir, _do)


def read_and_clear_mail(project_dir: str) -> list:
    """Pop everything pending for project_dir. Atomic read+clear under the
    same lock a concurrent write_mail would take, so nothing lands in the gap
    between reading and truncating."""
    path = _inbox_path(project_dir)

    def _do():
        mail = _read(path)
        if mail:
            _atomic_write(path, [])
        return mail

    return _with_lock(project_dir, _do)


def peek_pending_count(project_dir: str) -> int:
    """Lock-free — for status/monitor display only, never for delivery
    decisions. A stale-by-one-write count is fine there."""
    return len(_read(_inbox_path(project_dir)))
