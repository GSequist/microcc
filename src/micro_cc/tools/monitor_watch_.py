"""Streaming counterpart to monitor_'s "check" snapshot action.

A watch is a shell command spawned once and left running — `tail -f log |
grep ERROR`, a poll loop, whatever — whose stdout lines are treated as
events and pushed straight into the running TUI's own turn queue as they
happen (see set_wake_callback), instead of the model having to remember to
call monitor_(action="check") again to notice anything changed.

In-process module state only, no disk persistence: a watch is only ever
meaningful inside the interactive TUI, which sits idle between turns and can
receive a push at any time. A headless/batch run executes exactly one turn
and exits — there is no idle loop left standing by to receive one, so
start_watch refuses outright (via the wake_callback-not-set check) rather
than starting a subprocess nothing will ever read the events from.
"""

import asyncio
import os
import signal
import time
import uuid
from pathlib import Path

from micro_cc.tools.bash_tool import _SHELL, _bash_env
from micro_cc.utils.msg_store_ import _get_storage_dir

_watches: dict[str, dict] = {}

# Set once by start_live_tui_.py's start() — a plain sync function taking
# the fully-formatted content string to inject as boss's next turn. None
# means no interactive session is listening (headless/batch, or the TUI
# hasn't finished starting yet), and start_watch below refuses in that case
# instead of spawning a process whose events would just vanish.
_wake_callback = None

# Stdout lines arriving within this window of each other are batched into
# one notification rather than one per line. A burst from one event (a
# multi-line stack trace, say) reads as one turn instead of N.
_BATCH_WINDOW_S = 0.2

# A watch whose filter is too broad (or missing entirely) can otherwise
# flood claude_loop_ with a full model round-trip per firing. More than this
# many firings inside _RATE_LIMIT_WINDOW_S auto-stops the watch instead of
# continuing to fire, preventing runaway event loops.
_RATE_LIMIT_WINDOW_S = 10.0
_RATE_LIMIT_MAX_FIRES = 15


def set_wake_callback(cb) -> None:
    global _wake_callback
    _wake_callback = cb


def list_watches() -> list[dict]:
    return [
        {
            "watch_id": watch_id,
            "description": w["description"],
            "command": w["command"],
            "age": int(time.time() - w["started_at"]),
        }
        for watch_id, w in _watches.items()
        if w["status"] == "running"
    ]


def _output_path(project_dir: str, watch_id: str) -> Path:
    d = _get_storage_dir(project_dir) / "monitor_output"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{watch_id}.log"


def get_watch_output_path(watch_id: str) -> str | None:
    w = _watches.get(watch_id)
    return w["output_path"] if w else None


async def start_watch(
    command: str,
    description: str,
    *,
    project_dir: str,
    timeout_ms: int = 300_000,
    persistent: bool = False,
) -> str:
    if _wake_callback is None:
        return (
            "[monitor watch unavailable: no interactive session is listening "
            "for pushed events right now — watches only work inside the live "
            "TUI, not headless/batch runs. Use action='check' instead.]"
        )

    watch_id = uuid.uuid4().hex[:8]
    out_path = _output_path(project_dir, watch_id)
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=project_dir,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**_bash_env(), "TERM": "dumb"},
        # New session/process-group, same reason bash_ uses it: lets stop_watch
        # (and kill_all on app exit) reach the whole tree, not just this shell.
        start_new_session=True,
    )
    _watches[watch_id] = {
        "description": description,
        "command": command,
        "proc": proc,
        "project_dir": project_dir,
        "started_at": time.time(),
        "status": "running",
        "fire_times": [],
        "output_path": str(out_path),
    }
    _watches[watch_id]["reader_task"] = asyncio.ensure_future(_reader_loop(watch_id))
    _watches[watch_id]["stderr_task"] = asyncio.ensure_future(_stderr_to_file(watch_id))
    if not persistent:
        _watches[watch_id]["timeout_task"] = asyncio.ensure_future(
            _timeout_guard(watch_id, timeout_ms)
        )

    return (
        f"Watch started: {watch_id} — {description}. Matching output will "
        f"arrive as its own turn as it happens; no need to poll. Stop early "
        f"with monitor_(action='stop', watch_id='{watch_id}')."
    )


async def _timeout_guard(watch_id: str, timeout_ms: int) -> None:
    try:
        await asyncio.sleep(timeout_ms / 1000)
    except asyncio.CancelledError:
        return
    if _watches.get(watch_id, {}).get("status") == "running":
        await stop_watch(watch_id, reason=f"timed out after {timeout_ms}ms")


async def _reader_loop(watch_id: str) -> None:
    entry = _watches[watch_id]
    proc = entry["proc"]
    pending: list[str] = []
    try:
        while True:
            try:
                # No pending batch yet: block indefinitely for the first
                # line. A batch already started: cap the wait at the
                # batching window so it flushes even if more lines are
                # still coming, instead of growing unbounded.
                line = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=_BATCH_WINDOW_S if pending else None,
                )
            except asyncio.TimeoutError:
                _fire(watch_id, pending)
                pending = []
                continue
            if not line:  # real EOF — the script exited
                break
            pending.append(line.decode("utf-8", errors="replace").rstrip("\n"))
        if pending:
            _fire(watch_id, pending)
    except asyncio.CancelledError:
        return
    finally:
        code = await proc.wait()
        if _watches.get(watch_id, {}).get("status") == "running":
            _finish(watch_id, f"stream ended (exit code {code})")


async def _stderr_to_file(watch_id: str) -> None:
    """Stderr is captured to disk, not streamed as events. Stdout is the
    event stream, while stderr is available for a human/model to read on
    demand if the command itself is failing rather than just producing no
    matches."""
    entry = _watches[watch_id]
    proc = entry["proc"]
    try:
        with open(entry["output_path"], "ab") as f:
            while True:
                chunk = await proc.stderr.read(65536)
                if not chunk:
                    return
                f.write(chunk)
                f.flush()
    except (asyncio.CancelledError, OSError):
        return


def _fire(watch_id: str, lines: list[str]) -> None:
    entry = _watches.get(watch_id)
    if entry is None or not lines:
        return
    now = time.time()
    entry["fire_times"] = [t for t in entry["fire_times"] if now - t < _RATE_LIMIT_WINDOW_S]
    entry["fire_times"].append(now)
    if len(entry["fire_times"]) > _RATE_LIMIT_MAX_FIRES:
        asyncio.ensure_future(stop_watch(
            watch_id,
            reason=(
                "stopped automatically — producing too many events too fast "
                "(more than "
                f"{_RATE_LIMIT_MAX_FIRES} in {_RATE_LIMIT_WINDOW_S:.0f}s). "
                "Tighten the filter before starting a new watch."
            ),
        ))
        return
    content = f"[monitor: {entry['description']}]\n" + "\n".join(lines)
    if _wake_callback is not None:
        _wake_callback(content)


def _finish(watch_id: str, reason: str) -> None:
    entry = _watches.get(watch_id)
    if entry is None:
        return
    entry["status"] = "stopped"
    if _wake_callback is not None:
        _wake_callback(f"[monitor: {entry['description']}] {reason}")


async def stop_watch(watch_id: str, *, reason: str = "stopped by request") -> str:
    entry = _watches.get(watch_id)
    if entry is None:
        return f"No active watch with id '{watch_id}'."
    if entry["status"] != "running":
        return f"Watch {watch_id} is already stopped."
    # Set before cancelling: _reader_loop's own finally re-checks this same
    # flag before calling _finish, so it never double-fires a second
    # "stream ended" notification on top of the reason fired here.
    entry["status"] = "stopped"
    for key in ("reader_task", "stderr_task", "timeout_task"):
        task = entry.get(key)
        if task is not None and not task.done():
            task.cancel()
    try:
        os.killpg(entry["proc"].pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    if reason != "stopped by request" and _wake_callback is not None:
        _wake_callback(f"[monitor: {entry['description']}] {reason}")
    return f"Watch {watch_id} stopped ({reason})."


def kill_all() -> None:
    """App-shutdown cleanup (start_live_tui_.stop()) — a watch is OUR OWN
    subprocess, not a deliberately-detached `cmd &` the user wants to
    survive past this session the way bash_'s own background procs do, so
    nothing here should still be running once the TUI process exits."""
    for entry in _watches.values():
        if entry["status"] == "running":
            try:
                os.killpg(entry["proc"].pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
