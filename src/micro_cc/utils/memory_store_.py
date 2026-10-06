import datetime
import json
import os
from pathlib import Path
from typing import Callable, Optional
from micro_cc.postgres_store import pg_store_
from micro_cc.utils.helpers import project_hash as _project_hash

MAX_KEYS = 100

_MEMORY_PATH = Path.home() / ".micro-cc" / "memory.json"


def _use_postgres() -> bool:
    """Routes to Postgres if MICRO_CC_POSTGRES_URL is set."""
    return bool(os.getenv("MICRO_CC_POSTGRES_URL"))


# Post-local-write callback: (project_dir, action, key, description, content).
_sink: Optional[Callable[[Optional[str], str, str, str, str], None]] = None


def set_sink(fn) -> None:
    """Register a post-local-write callback. None to unregister."""
    global _sink
    _sink = fn


def _notify_sink(project_dir: Optional[str], action: str, key: str, description: str, content: str) -> None:
    if _sink is None:
        return
    try:
        _sink(project_dir, action, key, description, content)
    except Exception as e:
        print(f"[memory_store_] sink failed for {project_dir!r} key {key!r}: {e}", flush=True)


def _project_memory_path(project_dir: str) -> Path:
    """Path to project's memory.json in its storage dir."""
    from micro_cc.utils.msg_store_ import _get_storage_dir
    return _get_storage_dir(project_dir) / "memory.json"


def _path_for(project_dir: str | None) -> Path:
    return _project_memory_path(project_dir) if project_dir else _MEMORY_PATH


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def _save(path: Path, store: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2))


def _phash(project_dir: str | None) -> str:
    """'' means global scope — see pg_store_._ensure_memory_table."""
    return _project_hash(project_dir) if project_dir else ""


def hydrate_local(entries: dict, project_dir: str | None = None) -> None:
    """Seed local memory from an embedding app's durable copy. entries: {key: {description, content, updated_at}}. Full replace, no sink notify."""
    _save(_path_for(project_dir), entries)


def list_memories(project_dir: str | None = None) -> list:
    """List [{key, description, updated_at}], newest first."""
    if _use_postgres():
        return pg_store_.list_memories(project_hash=_phash(project_dir))

    store = _load(_path_for(project_dir))
    items = [
        {"key": k, "description": v.get("description", ""), "updated_at": v.get("updated_at", "")}
        for k, v in store.items()
    ]
    items.sort(key=lambda it: it["updated_at"], reverse=True)
    return items


def get_memory(key: str, project_dir: str | None = None) -> dict | None:
    if _use_postgres():
        return pg_store_.get_memory(key, project_hash=_phash(project_dir))

    store = _load(_path_for(project_dir))
    entry = store.get(key)
    if not entry:
        return None
    return {"key": key, **entry}


def count_memories(project_dir: str | None = None) -> int:
    if _use_postgres():
        return pg_store_.count_memories(project_hash=_phash(project_dir))
    return len(_load(_path_for(project_dir)))


def add_memory(key: str, description: str, content: str, project_dir: str | None = None) -> dict:
    """Returns {'status': 'ok'|'exists'}."""
    if _use_postgres():
        return pg_store_.add_memory(key, description, content, project_hash=_phash(project_dir))

    path = _path_for(project_dir)
    store = _load(path)
    if key in store:
        return {"status": "exists", "key": key}
    store[key] = {
        "description": description,
        "content": content,
        "updated_at": datetime.datetime.now().isoformat(),
    }
    _save(path, store)
    _notify_sink(project_dir, "add", key, description, content)
    return {"status": "ok", "key": key}


def edit_memory(
    key: str,
    new_content: str | None = None,
    new_description: str | None = None,
    project_dir: str | None = None,
) -> dict:
    """Returns {'status': 'ok'|'missing'}."""
    if _use_postgres():
        return pg_store_.edit_memory(key, new_content, new_description, project_hash=_phash(project_dir))

    path = _path_for(project_dir)
    store = _load(path)
    if key not in store:
        return {"status": "missing", "key": key}
    if new_content is not None:
        store[key]["content"] = new_content
    if new_description is not None:
        store[key]["description"] = new_description
    store[key]["updated_at"] = datetime.datetime.now().isoformat()
    _save(path, store)
    # Relay merged values, not the partial diff.
    _notify_sink(project_dir, "edit", key, store[key]["description"], store[key]["content"])
    return {"status": "ok", "key": key}


def delete_memory(key: str, project_dir: str | None = None) -> dict:
    """Returns {'status': 'ok'|'missing'}."""
    if _use_postgres():
        return pg_store_.delete_memory(key, project_hash=_phash(project_dir))

    path = _path_for(project_dir)
    store = _load(path)
    if key not in store:
        return {"status": "missing", "key": key}
    del store[key]
    _save(path, store)
    _notify_sink(project_dir, "delete", key, "", "")
    return {"status": "ok", "key": key}
