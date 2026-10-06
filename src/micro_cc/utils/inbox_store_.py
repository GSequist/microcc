"""Local file-based inbox for message_session_ deliveries to a project_dir that may not be running.

One inbox.json per project_dir, guarded by an fcntl flock on a sidecar file (writers are separate processes) and written via tmp + os.replace.
"""

import datetime
import fcntl
import json
import os
import uuid
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
    """Drop a message in target_project_dir's inbox; delivered next time that project's headless run folds mail."""
    path = _inbox_path(target_project_dir)

    def _do():
        mail = _read(path)
        mail.append({
            "id": uuid.uuid4().hex,
            "from": from_dir,
            "text": text,
            "timestamp": datetime.datetime.now().isoformat(),
        })
        _atomic_write(path, mail)

    _with_lock(target_project_dir, _do)


def peek_mail(project_dir: str) -> list:
    """Pending mail without clearing it; entries from before ids existed get one."""
    path = _inbox_path(project_dir)

    def _do():
        mail = _read(path)
        if any("id" not in m for m in mail):
            for m in mail:
                m.setdefault("id", uuid.uuid4().hex)
            _atomic_write(path, mail)
        return mail

    return _with_lock(project_dir, _do)


def ack_mail(project_dir: str, ids: list) -> None:
    """Remove delivered mail by id; mail that arrived since the peek stays."""
    path = _inbox_path(project_dir)
    done = set(ids)

    def _do():
        mail = _read(path)
        rest = [m for m in mail if m.get("id") not in done]
        if len(rest) != len(mail):
            _atomic_write(path, rest)

    _with_lock(project_dir, _do)


def peek_pending_count(project_dir: str) -> int:
    """Lock-free count for status display only, never for delivery."""
    return len(_read(_inbox_path(project_dir)))
