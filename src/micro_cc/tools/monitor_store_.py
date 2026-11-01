"""monitors.json: persisted polls and persistent watches, so they re-arm after a restart or self-reload.
A record re-arms when durable, or when its pid is ours (self-reload execs in place, keeping the pid)."""

import json
import os
import time
from pathlib import Path

from micro_cc.utils.msg_store_ import _get_storage_dir

DEFAULT_MAX_AGE_DAYS = 7


def _path(project_dir: str) -> Path:
    return _get_storage_dir(project_dir) / "monitors.json"


def load(project_dir: str) -> dict:
    try:
        data = json.loads(_path(project_dir).read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("polls", {})
    data.setdefault("watches", {})
    return data


def _save(project_dir: str, data: dict) -> None:
    path = _path(project_dir)
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def put(project_dir: str, kind: str, rec_id: str, rec: dict) -> None:
    data = load(project_dir)
    data[kind][rec_id] = rec
    _save(project_dir, data)


def drop(project_dir: str, kind: str, rec_id: str) -> None:
    data = load(project_dir)
    if data[kind].pop(rec_id, None) is not None:
        _save(project_dir, data)


def expires_at(max_age_days: float | None, now: float | None = None) -> float | None:
    """None never expires; default is 7 days."""
    days = DEFAULT_MAX_AGE_DAYS if max_age_days is None else max_age_days
    return None if not days else (now or time.time()) + days * 86400


def rearmable(project_dir: str, kind: str) -> list[dict]:
    """Records to resume now; stale session-only ones (another process's) are dropped."""
    data = load(project_dir)
    keep, out = {}, []
    for rec_id, rec in data[kind].items():
        if rec.get("durable") or rec.get("pid") == os.getpid():
            keep[rec_id] = {**rec, "pid": os.getpid()}
            out.append({"id": rec_id, **keep[rec_id]})
    if keep != data[kind]:
        data[kind] = keep
        _save(project_dir, data)
    return out
