import logging
import time
import threading
from typing import Dict, Optional, Any

logger = logging.getLogger(__name__)

# In-memory, single-process state store for the running project's loop state
# (todos, streaming flag). microcc is always one user
# in one process, so a namespaced module-level dict with per-entry TTLs is
# all this needs — no separate process, no connection, no serialization
# boundary to manage.

_NAMESPACE = "micro_cc"

_store: Dict[str, Dict[str, Any]] = {}
_lock = threading.RLock()  # reentrant, since some ops call other ops while holding it
_cleanup_running = False


def _make_key(key_type: str, *parts: str) -> str:
    """Generate namespaced key"""
    return f"{_NAMESPACE}:{key_type}:" + ":".join(parts)


def _set_with_ttl(key: str, value: Any, ttl: int) -> None:
    """Set value with expiration timestamp"""
    expiry = time.time() + ttl
    with _lock:
        _store[key] = {"value": value, "expiry": expiry}


def _get(key: str) -> Optional[Any]:
    """Get value if not expired, cleanup if expired"""
    with _lock:
        if key not in _store:
            return None
        entry = _store[key]
        if time.time() > entry["expiry"]:
            del _store[key]
            return None
        return entry["value"]


def _delete(key: str) -> None:
    """Delete key"""
    with _lock:
        _store.pop(key, None)


def _cleanup_expired() -> None:
    """Background task to clean up expired entries"""
    while _cleanup_running:
        time.sleep(60)  # Run every minute
        current_time = time.time()
        with _lock:
            expired_keys = [
                k for k, v in _store.items() if current_time > v["expiry"]
            ]
            for key in expired_keys:
                del _store[key]
            if expired_keys:
                logger.debug("Cleaned up %d expired state entries", len(expired_keys))


def start_cleanup_task() -> None:
    """Start background cleanup thread"""
    global _cleanup_running
    if not _cleanup_running:
        _cleanup_running = True
        cleanup_thread = threading.Thread(
            target=_cleanup_expired, daemon=True, name="StateCleanup"
        )
        cleanup_thread.start()
        logger.debug("State cleanup task started")


def stop_cleanup_task() -> None:
    """Stop background cleanup thread"""
    global _cleanup_running
    _cleanup_running = False


# ========================= Todos =========================
# Stored as the raw dict (in-memory store, no serialization needed).
# Keyed by project_dir, same TTL as plan.

def set_todos(project_dir: str, todos: dict) -> None:
    """Persist the todo store (id -> {content, status, notes, ...})."""
    try:
        key = _make_key("todos", project_dir)
        _set_with_ttl(key, todos, 3600)
    except Exception as e:
        logger.warning("State error in set_todos: %s", e)


def get_todos(project_dir: str) -> Optional[dict]:
    """Return the todo store dict, or None if unset/expired."""
    try:
        key = _make_key("todos", project_dir)
        return _get(key)
    except Exception as e:
        logger.warning("State error in get_todos: %s", e)
        return None


def clear_todos(project_dir: str) -> None:
    """Wipe the todo store for this project."""
    try:
        key = _make_key("todos", project_dir)
        _delete(key)
    except Exception as e:
        logger.warning("State error in clear_todos: %s", e)


########################## streaming state

def set_streaming_state(project_dir: str, active: bool) -> None:
    key = _make_key("streaming", project_dir)
    if active:
        _set_with_ttl(key, True, 600)
    else:
        _delete(key)


def get_streaming_state(project_dir: str) -> bool:
    key = _make_key("streaming", project_dir)
    return bool(_get(key))
