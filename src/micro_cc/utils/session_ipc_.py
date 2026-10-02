"""Direct process-to-process messaging between live micro-cc sessions.

Each running TUI binds a Unix domain socket at
~/.micro-cc/projects/{name}_{hash}/session.sock (same storage dir msg_store_
already keys by project_dir). message_session_ (tools/message_session_.py)
connects to a target project's socket and hands it a JSON payload directly —
no polling, no on-disk inbox. If the socket isn't there, that session simply
isn't running right now; there is no offline delivery, same as the real
feature this mirrors.
"""

import asyncio
import json
import os
import time
from pathlib import Path

from micro_cc.utils.msg_store_ import _get_storage_dir

_SEND_TIMEOUT = 3.0


def _registry_dir() -> Path:
    return Path.home() / ".micro-cc" / "sessions"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def list_peers(exclude_project_dir: str | None = None) -> list[dict]:
    """Live TUI sessions registered in ~/.micro-cc/sessions/<pid>.json (what
    start_listener writes), minus `exclude_project_dir` (the caller). Entries
    whose pid is dead or whose socket is gone are swept."""
    me = os.path.abspath(os.path.expanduser(exclude_project_dir)) if exclude_project_dir else None
    peers = []
    for f in _registry_dir().glob("*.json"):
        try:
            info = json.loads(f.read_text())
            pid, pd = int(info["pid"]), info["project_dir"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not _pid_alive(pid) or not socket_path(pd).exists():
            f.unlink(missing_ok=True)
            continue
        if pd != me:
            peers.append(info)
    return sorted(peers, key=lambda i: i.get("started_at", 0))


def socket_path(project_dir: str) -> Path:
    return _get_storage_dir(project_dir) / "session.sock"


async def start_listener(project_dir: str, on_message):
    """Bind this project's socket. `on_message(from_dir, text)` is called
    for each inbound message, on the same asyncio loop as the caller —
    Textual apps already run their own loop, so this attaches straight into
    it and the callback can touch app/widget state directly.

    A leftover socket file from an unclean previous exit makes bind() raise
    "address already in use" — remove it first, it can't belong to a still
    -running listener (that process would have removed it, and two live
    TUIs on the same project_dir isn't a supported setup regardless).
    """
    path = socket_path(project_dir)
    if path.exists():
        path.unlink()
    try:
        _registry_dir().mkdir(parents=True, exist_ok=True)
        (_registry_dir() / f"{os.getpid()}.json").write_text(json.dumps({
            "pid": os.getpid(),
            "project_dir": os.path.abspath(os.path.expanduser(project_dir)),
            "started_at": time.time(),
        }))
    except OSError:
        pass  # discovery is best-effort; messaging by explicit path still works

    async def handle(reader, writer):
        try:
            data = await reader.read()
            payload = json.loads(data.decode())
            on_message(payload.get("from", "?"), payload.get("message", ""))
            writer.write(b'{"delivered": true}')
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    return await asyncio.start_unix_server(handle, path=str(path))


def stop_listener(project_dir: str, server) -> None:
    server.close()
    (_registry_dir() / f"{os.getpid()}.json").unlink(missing_ok=True)
    path = socket_path(project_dir)
    if path.exists():
        path.unlink()


async def send_message(target_project_dir: str, from_dir: str, message: str) -> str:
    """Deliver `message` to the live session at target_project_dir. Returns
    a short status string meant to go straight back to the calling model as
    the tool result."""
    path = socket_path(target_project_dir)
    if not path.exists():
        return f"no live session at {target_project_dir} — it must be open in another terminal to receive messages"

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path)), timeout=_SEND_TIMEOUT
        )
        writer.write(json.dumps({"from": from_dir, "message": message}).encode())
        writer.write_eof()
        await writer.drain()
        ack = await asyncio.wait_for(reader.read(), timeout=_SEND_TIMEOUT)
        writer.close()
        if b'"delivered": true' in ack:
            return f"delivered to session at {target_project_dir}"
        return f"session at {target_project_dir} did not acknowledge delivery"
    except (ConnectionRefusedError, FileNotFoundError):
        return f"no live session at {target_project_dir} — it must be open in another terminal to receive messages"
    except asyncio.TimeoutError:
        return f"timed out messaging session at {target_project_dir}"
