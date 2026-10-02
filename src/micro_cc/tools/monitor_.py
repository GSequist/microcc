import json
import os
import time
from typing import List, Literal, Optional

from rich.markup import escape as _markup_escape

from micro_cc.utils.msg_store_ import load_msgs, _get_storage_dir
from micro_cc.utils.inbox_store_ import peek_pending_count
from micro_cc.utils.subagent_tracker_ import read_tracked
from micro_cc.utils.tokenization_simple import peek_token_stats
from micro_cc.utils import status_marker
from micro_cc.tools.bash_tool import list_background_processes, get_output_tail
from micro_cc.tools.monitor_watch_ import start_watch, stop_watch, list_watches
from micro_cc.tools import monitor_schedule_ as sched
from micro_cc.tools.use_tool_ import resolve_call

_POLL_TAIL_BYTES = 8192

# Mirrors subagent_tracker_'s prune-eligible set (DONE/FAILED — dead ends, see
# subagent_tracker_.py's _TERMINAL_STATUSES). Duplicated rather than imported,
# same as screen_cmds_.py's own copy — each answers a locally different
# question and happens to share values.
_DONE_STATUSES = {"DONE", "FAILED"}


def _last_assistant_reply(msgs: list) -> str | None:
    for msg in reversed(msgs):
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
            return msg["content"]
    return None


def _last_raw_message(path) -> dict | None:
    """The last complete JSON line of messages.jsonl, read backwards so a huge
    transcript (or a multi-MB image tool_result line) costs one seek. Raw on
    purpose: load_msgs would repair a live subagent's in-flight tool_use with
    a synthetic result, making "running a tool" look finished."""
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
    """What a RUNNING subagent is doing right now, from disk only: the age of
    its last transcript write plus the shape of its last message, and its
    pid from the boss's tracker. Lets the boss tell "running bash_ for 95s"
    from "waiting on the model for 4m" from "pid is gone" before killing."""
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
    # Preferred source: activity.json (start_headless_._write_activity).
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
    """The one place that turns a project_dir into a status snapshot — used
    by monitor_ (full read, called on-demand by the model) AND by
    the TUI's ambient poller (start_live_.py, called every few seconds, so
    it uses a cheaper stat-gated path — see _tail_status_for_poll there
    instead of this for the hot loop; this one always does the plain full
    load_msgs read, which is fine for an occasional LLM tool call).

    Returns {resolved, name, status, summary, pending, tokens} — status is
    one of status_marker's DONE/FAILED/PAUSED/NEEDS_INPUT/RUNNING/UNKNOWN.
    tokens is that subagent's own persisted token_stats dict (see
    tokenization_simple.peek_token_stats) or None if it hasn't made a model
    call yet.
    """
    resolved = os.path.abspath(os.path.expanduser(project_dir))
    name = os.path.basename(resolved.rstrip("/")) or resolved

    reply = _last_assistant_reply(load_msgs(resolved))
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


def poll_status(project_dir: str, cached_stat: tuple | None) -> tuple:
    """Cheap repeated-poll variant of get_subagent_status, for a timer that
    ticks every few seconds across every tracked subagent (start_live_.py's
    ambient poller) — a plain load_msgs() there would re-read and re-parse
    the WHOLE transcript on every tick regardless of whether anything
    changed.

    store_msgs appends to messages.jsonl rather than rewriting it wholesale
    (see msg_store_.py), but it can still fall back to a full rewrite (its
    self-heal path) or be rewritten by rewind/erase, and checkpoint
    compaction can shrink the effective history separately — so a raw byte-offset
    delta read still can't be trusted to always land on a line boundary.
    The safe equivalent for this storage model: skip entirely if (mtime,
    size) hasn't changed since the last poll (nothing was written, so
    status can't have changed), and when it has, read only the tail
    instead of the whole file — the last assistant message is always at the
    end regardless of how much history precedes it.

    Returns (info_or_None, new_stat). info is None when unchanged (caller
    keeps whatever it already had); new_stat is what to pass back in next
    tick either way.
    """
    resolved = os.path.abspath(os.path.expanduser(project_dir))
    path = _get_storage_dir(resolved) / "messages.jsonl"
    try:
        st = path.stat()
    except FileNotFoundError:
        return None, cached_stat

    stat_key = (st.st_mtime_ns, st.st_size)
    if stat_key == cached_stat:
        return None, cached_stat

    name = os.path.basename(resolved.rstrip("/")) or resolved
    offset = max(0, st.st_size - _POLL_TAIL_BYTES)
    with open(path, "rb") as f:
        f.seek(offset)
        raw = f.read()

    reply = None
    for line in reversed(raw.decode("utf-8", errors="replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue  # the tail read may have cut into the middle of a line
        if msg.get("role") == "assistant" and isinstance(msg.get("content"), str):
            reply = msg["content"]
            break

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
    """total_input (running sum across every call, mirrors real spend) +
    output — see tokenization_simple.token_stats' own field docstring for
    why total_input rather than the input snapshot field."""
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
    # name only, not info["resolved"] — the full project_dir path isn't
    # useful for a human/chat reader, just clutter; the model already has
    # the resolved path from the project_dirs it passed in itself.
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


_STATUS_GLYPH_NAME_MAXLEN = 30


def format_status_glyph(name: str, status: str, pending: int = 0, tokens: dict | None = None) -> str:
    """One glyph + short name — e.g. '✓fetch-data' — for the ambient TUI
    status line (start_live_.py's _poll_subagents_tick), which renders one
    of these per tracked subagent, one per line. format_status_line above
    is the verbose sibling for monitor_'s tool-call output, where
    the model actually needs the full path/summary."""
    symbol = status_marker.SYMBOL.get(status, "?")
    color = status_marker.color(status)
    if len(name) > _STATUS_GLYPH_NAME_MAXLEN:
        name = name[: _STATUS_GLYPH_NAME_MAXLEN - 1] + "…"
    mail = "✉" if pending else ""
    used = _tokens_used(tokens)
    tok_tag = f" {_fmt_tokens(used)}" if used else ""
    # name is a subagent's own project_dir basename — the model picks that
    # path, so it's untrusted text as far as Rich markup is concerned. This
    # whole string gets fed straight into RichStatic.update() (see
    # start_live_tui_.py's _poll_subagents_tick), which parses it as Rich
    # markup on the next render tick — an unescaped "[" here (e.g. a subagent
    # dir named "fetch[api]") raises MarkupError deep inside _render_loop,
    # which has no try/except, permanently killing the render loop (screen
    # freezes with no visible error). Escaping name is what keeps a
    # model-chosen path from ever reaching the markup parser as syntax.
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
    One tool for everything you're keeping an eye on in the background:
    headless subagents, backgrounded bash processes, and live streaming
    watches over either. Pick the action:

    action="check" — on-demand snapshot of one or more `targets`, mixed
        freely (headless subagents and PIDs only — NOT for a live session you
        messaged with message_session_; its delivery result is the answer, a
        reply arrives as its own turn): a subagent's project_dir (absolute path, exactly as passed
        to microcc-headless) reports DONE/FAILED/PAUSED/NEEDS_INPUT (or "no
        output yet") plus its one-line summary and unread message_session_
        mail count; while it's running, also an activity line — pid alive or
        not, and "running <tool> for 3m" vs "waiting on the model for 4m" —
        so you can tell stuck from slow before deciding to kill/relaunch; a background bash process's PID (as a plain digit
        string, e.g. from bash_'s "stop with bash_(...)" line) reports
        whether it's still running plus a tail of its captured output.
        Never blocks, never talks to a running process directly — just
        reads what's already on disk/in memory, so it's safe to call as
        often as you like, including repeatedly on an already-finished
        subagent (won't cause a duplicate wakeup or resurrect a pruned one).
        A PAUSED subagent has already exited — relaunch it
        (`microcc-headless <project_dir> "<answer>"`) to continue.

    action="watch" — start a live streaming watch instead of polling:
        `command` is a shell pipeline whose stdout lines are events (e.g.
        `tail -f server.log | grep --line-buffered ERROR`), `description`
        is a short label shown in every notification. Matching lines are
        pushed to you as their own turn the moment they happen — you do
        NOT need to call action="check" in a loop waiting for them. Only
        works in the live interactive session (not headless/batch runs,
        which execute one turn and exit with no idle loop left to receive
        a push). Set persistent=True for a watch that should run for the
        whole session; otherwise it's killed after timeout_ms (default 5
        min, max 1h). Stderr is captured to disk, not streamed — if the
        command itself seems to be failing rather than just producing no
        matches, check it with action="check" targets=[watch_id].
        A watch firing too fast (bad/missing filter) is auto-stopped with
        a notification telling you to tighten it.

    action="stop" — stop watch `watch_id` early.

    action="list" — every currently active watch you've started, in case
        you've lost track of a watch_id.

    action="schedule" — set a prompt to fire later, once or on a repeating
        cadence. This is the "wake me in 20 minutes" / "check the deploy
        every 5 minutes" tool. Give it a `spec` and a `prompt` (the prompt
        is enqueued as its own turn when the schedule fires — exactly like a
        watch line, no polling on your part):
          spec="20m"                  — once, 20 minutes from now
          spec="2h"                   — once, in 2 hours
          spec="*/5 * * * *"          — every 5 minutes (cron, local time)
          spec="0 9 * * 1-5"          — weekdays at 09:00 local
          spec="30 14 28 2 *"         — Feb 28 at 14:30, this year or next
        Interval specs (Ns/Nm/Nh/Nd, or "every 5 minutes") default to
        one-shot ("wake me in 20m"); cron specs default to recurring (they
        reschedule every match). Override either way with recurring: pass
        recurring=True to repeat on an interval, or recurring=False to fire a
        cron spec once at its next match ("remind me at X"). durable=True
        writes it to the project store so it survives a restart AND a harness
        self-reload; the default (session-only) dies when this session ends —
        use durable only when the user asks for it to persist. Recurring
        schedules auto-expire after 7 days (fires once more, then deleted);
        tell the user that when you schedule one. Schedules only fire while
        this interactive session is idle — a headless run has no idle loop to
        receive the push.

    action="schedules" — list every schedule (durable + session-only) with
        its id, cadence, and next fire time.

    action="unschedule" — cancel schedule `schedule_id`.

    Args:
        targets: For action="check" — project_dir paths and/or PIDs, mixed freely.
        command: For action="watch" — the shell pipeline to run.
        description: For action="watch"/"schedule" — short label for notifications.
        watch_id: For action="stop" — the id returned when the watch started.
        timeout_ms: For action="watch" — kill after this long unless persistent.
        persistent: For action="watch" — run for the whole session instead of timing out.
        spec: For action="schedule" — interval ("20m", "every 5 minutes") or
            5-field cron ("*/5 * * * *").
        prompt: For action="schedule" — the prompt to enqueue when it fires.
        recurring: For action="schedule" — repeat on the cadence (default True).
        durable: For action="schedule" — persist across restarts (default False).
        schedule_id: For action="unschedule" — the id returned when scheduled.
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
        # recurring=None means "infer from the spec's kind": a cron spec is
        # recurring by nature ("every 5 minutes"), an interval spec is a
        # one-shot timer by nature ("wake me in 20m"). Pass recurring=True
        # explicitly to make an interval repeat, or recurring=False to make a
        # cron spec fire once at its next match.
        is_interval = sched.parse_interval(spec) is not None
        eff_recurring = (not is_interval) if recurring is None else bool(recurring)
        task, err = sched.add_schedule(
            project_dir, spec, prompt, description or prompt[:60],
            recurring=eff_recurring, durable=durable,
        )
        if task is None:
            return f"Error: {err}"
        # Make the scheduler live in this session if the TUI is up (it also
        # re-arms durable tasks on start). No-op when no callback is set
        # (headless/batch) — the schedule is stored, it just can't fire here.
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
