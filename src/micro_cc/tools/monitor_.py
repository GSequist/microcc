import json
import os
import time
from typing import List, Literal, Optional

from rich.markup import escape as _markup_escape

from micro_cc.utils.msg_store_ import load_msgs, _get_storage_dir
from micro_cc.utils.inbox_store_ import peek_pending_count
from micro_cc.utils.subagent_tracker_ import read_tracked
from micro_cc.utils.tokenization_simple import peek_token_stats
from micro_cc.utils import status_marker, subagent_notice_
from micro_cc.tools.bash_tool import list_background_processes, get_output_tail
from micro_cc.tools.monitor_watch_ import start_watch, stop_watch, list_watches
from micro_cc.tools import monitor_schedule_ as sched
from micro_cc.tools.use_tool_ import resolve_call

_POLL_TAIL_BYTES = 8192

# Mirrors subagent_tracker_'s DONE/FAILED set; duplicated to avoid an import.
_DONE_STATUSES = {"DONE", "FAILED"}


def _final_reply(msgs: list) -> str | None:
    """Last text-only message if the assistant's; a newer user turn (e.g. a relaunch prompt) means still running."""
    for msg in reversed(msgs):
        if msg.get("role") in ("user", "assistant") and isinstance(msg.get("content"), str):
            return msg["content"] if msg["role"] == "assistant" else None
    return None


def _past_baseline(resolved: str, baseline: int) -> bool:
    """True once the transcript has grown past the size it had when this run registered."""
    try:
        return (_get_storage_dir(resolved) / "messages.jsonl").stat().st_size > baseline
    except OSError:
        return False


def _last_raw_message(path) -> dict | None:
    """Last raw JSON line of messages.jsonl, read from the tail.
    Raw on purpose: load_msgs would fake a result for an in-flight tool_use."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            pos, buf = end, b""
            while pos > 0 and buf.count(b"\n") < 2 and end - pos < (32 << 20):
                step = min(1 << 16, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
    except OSError:
        return None
    for line in reversed(buf.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None  # the tail cut mid-line (a line bigger than the cap)
    return None


def _fmt_age(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _activity(resolved: str, caller_project_dir: str | None) -> str | None:
    """Activity line for a RUNNING subagent, from disk only: last write age, last message shape, pid."""
    path = _get_storage_dir(resolved) / "messages.jsonl"
    parts = []
    pid = None
    if caller_project_dir:
        entry = read_tracked(caller_project_dir)["subagents"].get(resolved) or {}
        pid = entry.get("pid")
    if pid is not None:
        try:
            os.kill(pid, 0)
            parts.append(f"pid {pid} alive")
        except ProcessLookupError:
            parts.append(f"pid {pid} NOT running")
        except PermissionError:
            parts.append(f"pid {pid} alive")
    # activity.json is preferred (written by start_headless_).
    try:
        act = json.loads((_get_storage_dir(resolved) / "activity.json").read_text())
    except (OSError, json.JSONDecodeError):
        act = None
    if act and (pid is None or act.get("pid") == pid):
        age = _fmt_age(time.time() - act.get("since", time.time()))
        tools = act.get("tools") or []
        if act.get("phase") == "tools" and tools:
            names = ", ".join(f"{n} ×{tools.count(n)}" if tools.count(n) > 1 else n for n in dict.fromkeys(tools))
            parts.append(f"running {names} for {age}")
        else:
            parts.append(f"waiting on the model for {age} (a first call on a long prompt can take minutes)")
        return "activity: " + ", ".join(parts)

    try:
        age = _fmt_age(time.time() - path.stat().st_mtime)
    except OSError:
        return ", ".join(parts) or None
    last = _last_raw_message(path)
    content = (last or {}).get("content")
    if last and last.get("role") == "assistant" and isinstance(content, list):
        tools = [resolve_call(b)[0] or "?" for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        if tools:
            names = ", ".join(f"{n} ×{tools.count(n)}" if tools.count(n) > 1 else n for n in dict.fromkeys(tools))
            parts.append(f"running {names} for {age}")
        else:
            parts.append(f"last write {age} ago")
    elif last and last.get("role") == "user":
        parts.append(f"waiting on the model for {age} (a first call on a long prompt can take minutes)")
    else:
        parts.append(f"last write {age} ago")
    return "activity: " + ", ".join(parts)


def get_subagent_status(project_dir: str, caller_project_dir: str | None = None) -> dict:
    """Status snapshot for a subagent project_dir (full transcript read).
    Returns {resolved, name, status, summary, pending, tokens, activity}. Use poll_status for the cheap TUI tick."""
    resolved = os.path.abspath(os.path.expanduser(project_dir))
    name = os.path.basename(resolved.rstrip("/")) or resolved

    entry = read_tracked(caller_project_dir)["subagents"].get(resolved, {}) if caller_project_dir else {}
    reply = _final_reply(load_msgs(resolved)) if _past_baseline(resolved, entry.get("baseline", -1)) else None
    if reply is None:
        status, summary = "RUNNING", "no completed turn yet"
    else:
        status, summary, _body = status_marker.parse(reply)

    activity = None
    if status not in _DONE_STATUSES and status not in ("PAUSED", "NEEDS_INPUT"):
        try:
            activity = _activity(resolved, caller_project_dir)
        except Exception:
            activity = None  # diagnostics only — never fail the status read

    return {
        "resolved": resolved,
        "name": name,
        "status": status,
        "summary": summary,
        "pending": peek_pending_count(resolved),
        "tokens": peek_token_stats(resolved),
        "activity": activity,
    }


def poll_status(project_dir: str, cached_stat: tuple | None, baseline: int = 0) -> tuple:
    """Cheap poll variant of get_subagent_status for the TUI tick.
    Skips when (mtime, size) is unchanged; otherwise reads only the file tail past `baseline` (the run's start).
    Returns (info_or_None, new_stat)."""
    resolved =os.path.abspath(os.path.expanduser(project_dir))
    path = _get_storage_dir(resolved) / "messages.jsonl"
    try:
        st = path.stat()
    except FileNotFoundError:
        return None, cached_stat

    stat_key = (st.st_mtime_ns, st.st_size)
    if stat_key == cached_stat:
        return None, cached_stat

    name = os.path.basename(resolved.rstrip("/")) or resolved
    if baseline > st.st_size:
        baseline = 0  # transcript was rewritten shorter; the turn-order rule still applies
    offset = max(baseline, st.st_size - _POLL_TAIL_BYTES)
    with open(path, "rb") as f:
        f.seek(offset)
        raw = f.read()

    msgs = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        try:
            msgs.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # blank, or the tail read cut into the middle of a line
    reply = _final_reply(msgs)
    if reply is None and not msgs and offset > baseline and b"\n" not in raw.rstrip(b"\n"):
        # The tail sits inside one huge last line (a long final reply): read that line in full.
        last = _last_raw_message(path) or {}
        if last.get("role") == "assistant" and isinstance(last.get("content"), str):
            reply = last["content"]

    if reply is None:
        status, summary = "RUNNING", "no completed turn yet"
    else:
        status, summary, _body = status_marker.parse(reply)

    info = {
        "resolved": resolved,
        "name": name,
        "status": status,
        "summary": summary,
        "pending": peek_pending_count(resolved),
        "tokens": peek_token_stats(resolved),
    }
    return info, stat_key


def _tokens_used(tokens: dict | None) -> int:
    """total_input (running sum of every call) + output."""
    if not tokens:
        return 0
    return tokens.get("total_input", 0) + tokens.get("output", 0)


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def format_status_line(info: dict) -> str:
    # name only: the full project_dir is clutter for a human reader.
    symbol = status_marker.SYMBOL.get(info["status"], "?")
    line = f"{symbol} {info['name']} — {info['status']}: {info['summary']}"
    if info["pending"]:
        line += f"  [{info['pending']} unread mail]"
    used = _tokens_used(info.get("tokens"))
    if used:
        line += f"  [{used:,} tokens]"
    if info.get("activity"):
        line += f"\n    {info['activity']}"
    return line


CHECKPOINT_STATUSES = {"DONE", "FAILED", "PAUSED", "NEEDS_INPUT"}

COLD_START_PREFIX = subagent_notice_.COLD_START_PREFIX


def poll_checkpoints(caller_project_dir: str, cache: dict) -> list[dict]:
    """One poll over tracked subagents: [{target, entry, info, status, wake}]; a dead RUNNING pid becomes FAILED."""
    from micro_cc.utils.subagent_tracker_ import update_status, _pid_alive

    rows = []
    for target, entry in read_tracked(caller_project_dir)["subagents"].items():
        info, cache[target] = poll_status(target, cache.get(target), entry.get("baseline", 0))
        if info is None:
            # Unchanged since last poll: a killed subagent never writes a terminal STATUS, so check its pid.
            status = entry["status"]
            pid = entry.get("pid")
            if status not in CHECKPOINT_STATUSES and pid is not None and not _pid_alive(pid):
                name = os.path.basename(target.rstrip("/")) or target
                info = {
                    "resolved": target, "name": name, "status": "FAILED",
                    "summary": "process is no longer running (stopped or killed before finishing)",
                    "pending": peek_pending_count(target),
                }
                status = "FAILED"
                update_status(caller_project_dir, target, status)
        else:
            status = info["status"]
            if status != entry["status"]:
                update_status(caller_project_dir, target, status)
                entry.update(status=status, updated_at=time.time())  # mirror the write for checkpoint_turn
        wake = info is not None and status in CHECKPOINT_STATUSES and not entry["notified"]
        rows.append({"target": target, "entry": entry, "info": info, "status": status, "wake": wake})
    return rows


def _run_usage(msgs: list, entry: dict, tokens: dict | None) -> str:
    """"N tokens · N tool uses · 3m12s" for the child's latest run (it starts at the prompt carrying STATUS_INSTRUCTIONS)."""
    start = max((i for i, m in enumerate(msgs) if m.get("role") == "user" and isinstance(m.get("content"), str)
                 and status_marker.STATUS_INSTRUCTIONS in m["content"]), default=0)
    uses = sum(1 for m in msgs[start:] if m.get("role") == "assistant" and isinstance(m.get("content"), list)
               for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use")
    parts = [f"{_tokens_used(tokens):,} tokens"] if _tokens_used(tokens) else []
    parts.append(f"{uses} tool uses")
    if entry.get("started_at"):
        parts.append(_fmt_age(entry.get("updated_at", time.time()) - entry["started_at"]))
    return " · ".join(parts)


def checkpoint_turn(rows: list[dict], *, cold_start: bool = False) -> str:
    """User turn waking the boss: one self-contained <subagent-notification> per woken poll_checkpoints row."""
    blocks = []
    for r in rows:
        info, target = r["info"], r["target"]
        transcript = str(_get_storage_dir(target) / "messages.jsonl")
        # Nothing written since this run registered: no result or usage from a prior run.
        msgs = load_msgs(target) if _past_baseline(target, r["entry"].get("baseline", -1)) else []
        reply = _final_reply(msgs) or ""
        # A killed child's last reply can still be stale; only attach one that matches this status.
        status, _summary, body = status_marker.parse(reply) if reply.strip() else ("", "", "")
        blocks.append(subagent_notice_.block(
            name=info["name"], project_dir=target, status=info["status"], summary=info["summary"],
            result=subagent_notice_.cap_result(body, transcript) if status == info["status"] else "",
            usage=_run_usage(msgs, r["entry"], info.get("tokens")), unread_mail=info.get("pending"),
            transcript=transcript,
        ))
    return (COLD_START_PREFIX if cold_start else "") + "\n".join(blocks)


_STATUS_GLYPH_NAME_MAXLEN = 30


def format_status_glyph(name: str, status: str, pending: int = 0, tokens: dict | None = None) -> str:
    """One glyph + short name for the ambient TUI status line; format_status_line is the verbose form."""
    symbol = status_marker.SYMBOL.get(status, "?")
    color = status_marker.color(status)
    if len(name) > _STATUS_GLYPH_NAME_MAXLEN:
        name = name[: _STATUS_GLYPH_NAME_MAXLEN - 1] + "…"
    mail = "✉" if pending else ""
    used = _tokens_used(tokens)
    tok_tag = f" {_fmt_tokens(used)}" if used else ""
    # name is model-chosen: an unescaped "[" raises MarkupError in the render loop.
    return f"[{color}]{symbol}[/{color}]{_markup_escape(name)}{mail}{tok_tag}"


def _format_bgproc_line(pid: int) -> str:
    procs = {p["pid"]: p for p in list_background_processes()}
    info = procs.get(pid)
    if info is None:
        return f"✗ PID {pid} — not running (exited, or never existed)"
    stall_note = " [output stalled — may be waiting on an interactive prompt]" if info.get("stalled") else ""
    line = f"▶ PID {pid} — running {info['age']}s: {info['command']}  (cwd={info['cwd']}){stall_note}"
    tail = get_output_tail(pid)
    line += f"\n  recent output:\n{tail}" if tail else "\n  (no output captured yet)"
    return line


async def monitor_(
    action: Literal[
        "check", "watch", "stop", "list",
        "schedule", "schedules", "unschedule",
    ],
    *,
    project_dir: str,
    targets: Optional[List[str]] = None,
    command: Optional[str] = None,
    description: Optional[str] = None,
    watch_id: Optional[str] = None,
    timeout_ms: int = 300_000,
    persistent: bool = False,
    spec: Optional[str] = None,
    prompt: Optional[str] = None,
    recurring: Optional[bool] = None,
    durable: bool = False,
    schedule_id: Optional[str] = None,
) -> str:
    """
    Background work: headless subagents, background bash PIDs, live streaming watches,
    and scheduled prompts. Pick an action:

    action="check": snapshot of `targets` (mixed freely). A subagent project_dir (absolute,
    as passed to microcc-headless) reports DONE/FAILED/PAUSED/NEEDS_INPUT, a summary, unread
    message_session_ mail, and while running an activity line (pid alive, "running <tool> for 3m",
    or "waiting on the model for 4m") to tell stuck from slow. A bash PID (digit string, from
    bash_'s "stop with bash_(...)" line) reports whether it runs plus an output tail. Never
    blocks or talks to a running process; safe to repeat. A PAUSED subagent has exited:
    relaunch it with `microcc-headless <project_dir> "<answer>"`. Not for live sessions
    reached with message_session_.

    action="watch": start a live streaming watch. `command` is a shell pipeline whose stdout
    lines are events (e.g. `tail -f server.log | grep --line-buffered ERROR`); `description`
    labels every notification. Matching lines arrive as their own turn, no polling needed
    (a top-level headless run stays up until its watches end). persistent=True runs for the whole session; otherwise it is
    killed after timeout_ms (default 5 min, max 1h). Stderr goes to a file: if the command
    seems to fail, check it with action="check" targets=[watch_id]. A watch that fires too
    fast is auto-stopped with a notice to tighten the filter.

    action="stop": stop watch `watch_id` early.
    action="list": active watches, in case a watch_id was lost.

    action="schedule": fire `prompt` later, once or repeating. `spec` is an interval
    ("20m", "2h", "every 5 minutes") or a 5-field cron in local time ("*/5 * * * *",
    "0 9 * * 1-5"). The prompt arrives as its own turn when due. Intervals default to one-shot,
    cron defaults to recurring; override with recurring=True/False. durable=True persists across
    restarts and self-reloads (default is session-only). Recurring schedules auto-expire after
    7 days (one final fire, then deleted), so tell the user when you schedule one. Schedules
    fire only while the interactive session is idle, never in a headless run.

    action="schedules": list every schedule (durable and session-only) with id, cadence, next fire.
    action="unschedule": cancel `schedule_id`.

    Args:
        targets: For "check": project_dir paths and/or PIDs, mixed freely.
        command: For "watch": the shell pipeline to run.
        description: For "watch"/"schedule": short label for notifications.
        watch_id: For "stop": the id returned when the watch started.
        timeout_ms: For "watch": kill after this long unless persistent.
        persistent: For "watch": run for the whole session instead of timing out.
        spec: For "schedule": interval or 5-field cron.
        prompt: For "schedule": the prompt to enqueue when it fires.
        recurring: For "schedule": repeat on cadence; default inferred from spec.
        durable: For "schedule": persist across restarts (default False).
        schedule_id: For "unschedule": the id returned when scheduled.
    """
    if action == "check":
        if not targets:
            return "Error: action='check' requires targets."
        lines = []
        for target in targets:
            if target.strip().isdigit():
                lines.append(_format_bgproc_line(int(target.strip())))
            else:
                info = get_subagent_status(target, caller_project_dir=project_dir)
                lines.append(format_status_line(info))
        return "\n".join(lines)

    if action == "watch":
        if not command or not description:
            return "Error: action='watch' requires command and description."
        return await start_watch(
            command, description, project_dir=project_dir,
            timeout_ms=timeout_ms, persistent=persistent,
        )

    if action == "stop":
        if not watch_id:
            return "Error: action='stop' requires watch_id."
        return await stop_watch(watch_id)

    if action == "list":
        watches = list_watches()
        if not watches:
            return "No active watches."
        return "\n".join(
            f"{w['watch_id']} · {w['description']} · running {w['age']}s"
            for w in watches
        )

    if action == "schedule":
        if not spec or not prompt:
            return "Error: action='schedule' requires spec and prompt."
        # recurring=None: cron specs repeat, interval specs are one-shot; an explicit flag overrides.
        is_interval = sched.parse_interval(spec) is not None
        eff_recurring = (not is_interval) if recurring is None else bool(recurring)
        task, err = sched.add_schedule(
            project_dir, spec, prompt, description or prompt[:60],
            recurring=eff_recurring, durable=durable,
        )
        if task is None:
            return f"Error: {err}"
        # Starts the TUI tick if one is up; headless runs just store the schedule.
        from micro_cc.tools import monitor_schedule_runtime as _rt
        _rt.ensure_running(project_dir)
        kind = "recurring" if task["recurring"] else "one-shot"
        scope = "durable (survives restart)" if task["durable"] else "session-only"
        label = (
            sched._interval_to_human(task["interval_seconds"])
            if task["kind"] == "interval" else sched.cron_to_human(task["spec"])
        )
        extra = ""
        if task["recurring"]:
            extra = " Recurring schedules auto-expire after 7 days unless you unschedule sooner."
        return (
            f"Scheduled {task['id']} — {label} ({kind}, {scope}).{extra} "
            f"It fires as its own turn when due. Cancel with "
            f"monitor_(action='unschedule', schedule_id='{task['id']}')."
        )

    if action == "schedules":
        tasks = sched.load_schedules(project_dir)
        if not tasks:
            return "No schedules."
        return "\n".join(sched.format_schedule_line(t) for t in tasks)

    if action == "unschedule":
        if not schedule_id:
            return "Error: action='unschedule' requires schedule_id."
        if sched.remove_schedule(project_dir, schedule_id):
            return f"Unscheduled {schedule_id}."
        return f"No schedule with id '{schedule_id}'."

    return (
        f"Error: unknown action '{action}'. Use "
        "check|watch|stop|list|schedule|schedules|unschedule."
    )
