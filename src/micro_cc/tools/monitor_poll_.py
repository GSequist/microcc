"""Poll watches (monitor_ action="poll"): run a cheap probe (shell command or MCP tool) on an interval,
diff against items already seen, and wake the model only for new ones. Idle probes cost no tokens.
Live in the interactive session only; persisted in monitors.json and re-armed on start."""

import asyncio
import hashlib
import json
import os
import signal
import time
import uuid

from micro_cc.tools import monitor_store_ as store

_polls: dict[str, dict] = {}   # id -> record + "task"
_wake_callback = None          # set by the TUI; None means nothing listens

MIN_EVERY_S = 60
PROBE_TIMEOUT_S = 120
MAX_FAILS = 5                  # consecutive failures before the poll stops itself
MAX_BACKOFF_S = 3600
SEEN_CAP = 2000
SHOW_ITEMS = 30
ITEM_CHARS = 500
OUTPUT_CAP = 1 << 20


def set_wake_callback(cb) -> None:
    global _wake_callback
    _wake_callback = cb


def has_listener() -> bool:
    return _wake_callback is not None


def _h(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def _find_list(data, depth: int = 0):
    """First list of objects in parsed JSON (top level or nested a few levels down)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and depth < 3:
        for v in data.values():
            if isinstance(v, list) and any(isinstance(x, dict) for x in v):
                return v
        for v in data.values():
            found = _find_list(v, depth + 1)
            if found is not None:
                return found
    return None


def extract_items(output: str, key: str | None) -> list[tuple[str, str]]:
    """[(dedupe_key, display)]. With `key`: JSON (or JSON lines) objects by that field; else one item per line."""
    if not key:
        return [(ln, ln) for ln in (l.strip() for l in output.splitlines()) if ln]
    try:
        objs = _find_list(json.loads(output))
    except json.JSONDecodeError:
        objs = []
        for ln in output.splitlines():
            try:
                objs.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    if objs is None:
        raise ValueError(f"no list of objects found in the probe output to read '{key}' from")
    items = [(str(o[key]), json.dumps(o, ensure_ascii=False)) for o in objs if isinstance(o, dict) and o.get(key) is not None]
    if not items and output.strip():
        raise ValueError(f"no items with field '{key}' in the probe output (drop key= to diff by line)")
    return items


async def _run_shell(command: str, project_dir: str) -> str:
    from micro_cc.tools.bash_tool import _bash_env
    proc = await asyncio.create_subprocess_shell(
        command, cwd=project_dir, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**_bash_env(), "TERM": "dumb"}, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT_S)
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if isinstance(e, asyncio.CancelledError):
            raise
        raise RuntimeError(f"probe timed out after {PROBE_TIMEOUT_S}s") from None
    if proc.returncode != 0:
        tail = (err or out).decode("utf-8", "replace").strip()[-400:]
        raise RuntimeError(f"probe exited {proc.returncode}: {tail}")
    return out[:OUTPUT_CAP].decode("utf-8", "replace")


async def _run_mcp(rec: dict, project_dir: str, unattended: bool) -> str:
    from micro_cc.tools import mcp_oauth
    from micro_cc.tools.mcp_client_ import call_mcp_tool, _server_key
    from micro_cc.tools.search_tool_ import get_effective_mcp_catalog
    entry = get_effective_mcp_catalog(project_dir).get(rec["mcp_server"])
    if entry is None:
        raise RuntimeError(f"MCP server '{rec['mcp_server']}' is no longer configured")
    token = mcp_oauth.UNATTENDED.set(unattended)
    try:
        out = await call_mcp_tool(_server_key(entry["server"]), rec["mcp_remote"], rec.get("mcp_args") or {},
                                  entry=entry, project_dir=project_dir)
    finally:
        mcp_oauth.UNATTENDED.reset(token)
    if out.startswith("MCP tool error"):
        raise RuntimeError(out[:400])
    return out


async def probe(rec: dict, project_dir: str, unattended: bool = True) -> list[tuple[str, str]]:
    out = await (_run_mcp(rec, project_dir, unattended) if rec.get("mcp_server")
                 else _run_shell(rec["command"], project_dir))
    return extract_items(out, rec.get("key"))


async def resolve_mcp(tool: str, project_dir: str) -> tuple[str, str] | None:
    """(server, remote tool name) for an MCP tool as the model calls it; loads servers on demand."""
    from micro_cc.tools.search_tool_ import MCP_RESOLVED, get_effective_mcp_catalog
    from micro_cc.tools.mcp_client_ import resolve_mcp_tools

    def _hit(routing):
        r = routing.get(tool)
        return (r["entry"]["server"]["name"], r.get("tool", tool)) if r and r.get("entry") else None

    for res in MCP_RESOLVED.values():
        if hit := _hit(res["routing"]):
            return hit
    for name, entry in get_effective_mcp_catalog(project_dir).items():
        if name in MCP_RESOLVED:
            continue
        tools, routing, failures = await resolve_mcp_tools([entry], project_dir)
        if not failures:
            MCP_RESOLVED[name] = {"tools": tools, "routing": routing}
            if hit := _hit(routing):
                return hit
    return None


def _wake(text: str) -> None:
    if _wake_callback is not None:
        _wake_callback(text)


def _fmt_new(rec: dict, new: list[tuple[str, str]]) -> str:
    lines = [f"[monitor: {rec['description']}]"]
    if rec.get("prompt"):
        lines.append(rec["prompt"])
    lines.append(f"{len(new)} new item{'s' if len(new) != 1 else ''}:")
    for _k, disp in new[:SHOW_ITEMS]:
        lines.append(f"- {disp[:ITEM_CHARS]}{'…' if len(disp) > ITEM_CHARS else ''}")
    if len(new) > SHOW_ITEMS:
        lines.append(f"…and {len(new) - SHOW_ITEMS} more (run the probe yourself to see them all)")
    return "\n".join(lines)


def _persist(poll_id: str) -> None:
    rec = _polls.get(poll_id)
    if rec is not None:
        store.put(rec["project_dir"], "polls", poll_id, {k: v for k, v in rec.items() if k != "task"})


def _stop(poll_id: str, reason: str | None) -> None:
    rec = _polls.pop(poll_id, None)
    if rec is None:
        return
    store.drop(rec["project_dir"], "polls", poll_id)
    task = rec.get("task")
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()
    if reason:
        _wake(f"[monitor: {rec['description']}] stopped — {reason}")


async def tick(poll_id: str, now: float | None = None) -> float:
    """One probe; wakes on new items. Returns seconds until the next probe (0 = stopped)."""
    from micro_cc.tools.mcp_oauth import AuthExpired
    rec = _polls.get(poll_id)
    if rec is None:
        return 0
    now = time.time() if now is None else now
    if rec.get("expires_at") and now >= rec["expires_at"]:
        _stop(poll_id, "it reached its time limit. Tell the user and offer to start it again.")
        return 0
    try:
        items = await probe(rec, rec["project_dir"])
    except asyncio.CancelledError:
        raise
    except AuthExpired as e:
        _stop(poll_id, f"{e}. Tell the user, then start the poll again once logged in.")
        return 0
    except Exception as e:
        rec["fails"] = rec.get("fails", 0) + 1
        rec["last_error"] = str(e)[:400]
        if rec["fails"] >= MAX_FAILS:
            _stop(poll_id, f"{rec['fails']} probes in a row failed, last: {rec['last_error']}")
            return 0
        _persist(poll_id)
        return min(rec["every"] * 2 ** rec["fails"], MAX_BACKOFF_S)
    seen = rec["seen"]
    seen_set = set(seen)
    new = [(k, d) for k, d in items if _h(k) not in seen_set]
    new = list({_h(k): (k, d) for k, d in new}.values())  # dedupe within one probe
    seen.extend(_h(k) for k, _ in new)
    del seen[:-SEEN_CAP]
    rec.update(fails=0, last_error=None, last_ok=now)
    _persist(poll_id)
    if new:
        _wake(_fmt_new(rec, new))
    return rec["every"]


async def _loop(poll_id: str, first_delay: float) -> None:
    delay = first_delay
    try:
        while poll_id in _polls:
            await asyncio.sleep(delay)
            delay = await tick(poll_id)
            if not delay:
                return
    except asyncio.CancelledError:
        return
    except Exception as e:  # a bug here must not die silently
        _stop(poll_id, f"internal error: {type(e).__name__}: {e}")


def _arm(rec: dict, first_delay: float) -> None:
    _polls[rec["id"]] = rec
    rec["task"] = asyncio.ensure_future(_loop(rec["id"], first_delay))


async def start_poll(project_dir: str, *, description: str, every: int, command: str | None = None,
                     mcp_tool: str | None = None, mcp_args: dict | None = None, key: str | None = None,
                     prompt: str | None = None, durable: bool = False, max_age_days: float | None = None) -> str:
    if not has_listener():
        return ("[monitor poll unavailable: polls run only in the interactive session. "
                "In a headless run, let cron re-run you instead.]")
    rec = {"description": description, "every": max(MIN_EVERY_S, int(every)), "key": key,
           "prompt": prompt or "", "project_dir": project_dir, "durable": bool(durable), "pid": os.getpid(),
           "created_at": time.time(), "expires_at": store.expires_at(max_age_days), "fails": 0, "seen": []}
    if mcp_tool:
        hit = await resolve_mcp(mcp_tool, project_dir)
        if hit is None:
            return f"Error: MCP tool '{mcp_tool}' not found on any configured server (load it with search_tools first)."
        rec.update(mcp_server=hit[0], mcp_remote=hit[1], mcp_tool=mcp_tool, mcp_args=mcp_args or {})
    else:
        rec["command"] = command
    try:
        items = await probe(rec, project_dir, unattended=False)  # user is here: an OAuth login may prompt
    except Exception as e:
        return f"Error: first probe failed, poll not started: {e}"
    rec["seen"] = list(dict.fromkeys(_h(k) for k, _ in items))[-SEEN_CAP:]
    rec["last_ok"] = time.time()
    rec["id"] = uuid.uuid4().hex[:8]
    _arm(rec, rec["every"])
    _persist(rec["id"])
    life = "for this session" if not durable else "across restarts"
    until = (f", stops in {max_age_days if max_age_days is not None else store.DEFAULT_MAX_AGE_DAYS:g} days"
             if rec["expires_at"] else ", no time limit")
    return (f"Poll started: {rec['id']} — {description}. Checks every {rec['every'] // 60} min {life}{until}. "
            f"{len(items)} existing item(s) recorded as already seen; only new ones wake you. "
            f"Stop with monitor_(action='stop', watch_id='{rec['id']}').")


def stop_poll(poll_id: str) -> str | None:
    """None when no such poll (caller tries a watch)."""
    if poll_id not in _polls:
        return None
    _stop(poll_id, None)
    return f"Poll {poll_id} stopped."


def list_polls() -> list[dict]:
    now = time.time()
    out = []
    for pid_, r in _polls.items():
        nxt = (r.get("last_ok") or r["created_at"]) + r["every"]
        probe_ = f"mcp {r['mcp_tool']}" if r.get("mcp_tool") else r.get("command", "")
        out.append({"watch_id": pid_, "description": r["description"], "every": r["every"], "command": probe_,
                    "next_in": max(0, int(nxt - now)), "fails": r.get("fails", 0), "last_error": r.get("last_error"),
                    "durable": r.get("durable", False), "age": int(now - r["created_at"])})
    return out


def rearm(project_dir: str) -> int:
    """Resume persisted polls (TUI start); returns how many."""
    now = time.time()
    n = 0
    for rec in store.rearmable(project_dir, "polls"):
        if rec["id"] in _polls:
            continue
        rec["project_dir"] = project_dir
        _arm(rec, max(1.0, (rec.get("last_ok") or now) + rec["every"] - now))
        n += 1
    return n


def kill_all() -> None:
    """App shutdown: cancel tasks, keep records for re-arm."""
    for rec in _polls.values():
        task = rec.get("task")
        if task is not None and not task.done():
            task.cancel()
    _polls.clear()
