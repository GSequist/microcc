import argparse
import asyncio
import datetime
import json
import os
import select
import signal
import subprocess
import sys
import time

from micro_cc.claude_loop_ import claude_loop
from micro_cc.utils.msg_store_ import load_msgs, store_msgs, _get_storage_dir
from micro_cc.utils.helpers import has_configured_endpoint, effective_trim_budget
from micro_cc.models.registry import DEFAULT_MODEL
from micro_cc.utils import status_marker
from micro_cc.utils.inbox_store_ import read_and_clear_mail
from micro_cc.utils.settings_store_ import get_setting


def _build_prompt(prompt: str, files: list) -> str:
    parts = []
    if files:
        resolved = [os.path.abspath(os.path.expanduser(f)) for f in files]
        parts.append("Files to work on:\n" + "\n".join(f"- {f}" for f in resolved))
    parts.append(prompt)
    parts.append(status_marker.STATUS_INSTRUCTIONS)
    return "\n\n".join(parts)


def _fold_mail(msgs: list, project_dir: str) -> None:
    """Drain project_dir's message_session_ inbox straight onto the live
    msgs list — same splice handle_turn_boundary does for the live TUI's
    in-memory queue (etype_handler_.py), just sourced from the durable
    mailbox instead. Safe to call at a turn_boundary: msgs always ends on a
    tool_result or prior user turn there, never mid tool_use/tool_result
    pairing, so appending another plain user message is exactly the splice
    point claude_loop's own turn_boundary comment documents."""
    for mail in read_and_clear_mail(project_dir):
        msgs.append({
            "role": "user",
            "content": f"[incoming from {mail['from']}]\n{mail['text']}",
        })


WRAP_UP_LOOPS = 5  # extra turn_boundary rounds granted after max_loops, one reminder each


def _wrap_up_reminder(max_loops: int, loops_left: int) -> dict:
    return {
        "role": "user",
        "content": (
            f"<system-reminder>\nLoop limit reached ({max_loops} tool-call rounds). "
            "Wrap up now: finish or checkpoint your current work and give a final "
            f"answer. {loops_left} round(s) left before this run is cut off regardless.\n"
            "</system-reminder>"
        ),
    }


def _register(caller_project_dir: str, project_dir: str) -> str | None:
    """Self-register with the boss's tracker, enforcing the name lock and the
    concurrent-subagent cap under one flock. Returns a refusal message, or
    None when this run may proceed (including when bookkeeping itself failed
    — losing the tracker entry must never abort the actual task)."""
    from micro_cc.utils.subagent_tracker_ import add_tracked, NameInUse, CapReached
    from micro_cc.utils.settings_store_ import max_subagents
    try:
        add_tracked(caller_project_dir, project_dir, max_running=max_subagents())
    except (NameInUse, CapReached) as e:
        return str(e)
    except OSError:
        pass
    return None


def _field(block, key):
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


def _finalize_killed(msgs: list, sig_name: str, in_flight: list) -> str:
    """Leave the transcript valid and truthful after a SIGTERM/SIGINT stop,
    ending in a STATUS: FAILED line the boss's poller parses into its wakeup.
    Returns the summary.

    claude_loop answers open calls itself on cancel; a dangling tail is still
    answered here so the next load has nothing to repair."""
    tail = msgs[-1] if msgs else None
    dangling = []
    if tail is not None and tail.get("role") == "assistant" and isinstance(tail.get("content"), list):
        dangling = [_field(b, "id") for b in tail["content"] if _field(b, "type") == "tool_use"]
    if dangling:
        msgs.append({
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": f"Stopped by {sig_name} before this tool finished — no result.",
                    "is_error": True,
                }
                for tid in dangling
            ],
        })
    if in_flight:
        where = f"while running {', '.join(dict.fromkeys(in_flight))}"
    else:
        where = "while waiting on the model"
    summary = f"stopped by {sig_name} {where}"
    finished = tail is not None and tail.get("role") == "assistant" and isinstance(tail.get("content"), str)
    if not finished:
        msgs.append({"role": "assistant", "content": f"STATUS: FAILED — {summary}"})
    return summary


async def _stop_background_work() -> None:
    """After a kill: stop our own leftovers instead of draining them — bash_
    `cmd &` process groups, monitor watches, and pending asyncio tasks
    (e.g. a compact_checkpoint mid model call), bounded so a stuck task
    can't hold the process open."""
    from micro_cc.tools.bash_tool import kill_background_processes
    from micro_cc.tools.monitor_watch_ import kill_all as kill_watches
    kill_background_processes()
    kill_watches()
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.wait(pending, timeout=5)


def _write_activity(project_dir: str, phase: str, tools: list | None = None) -> None:
    """Current phase for monitor_._activity (slow vs dead); diagnostics only, never fails the run."""
    try:
        path = _get_storage_dir(project_dir) / "activity.json"
        tmp = path.with_name(f"activity.json.tmp.{os.getpid()}")
        tmp.write_text(json.dumps({"phase": phase, "tools": tools or [], "since": time.time(), "pid": os.getpid()}))
        tmp.replace(path)
    except OSError:
        pass


def _summarize_question(question: dict) -> str:
    questions = question.get("input", {}).get("questions") or []
    if questions:
        return questions[0].get("question", "needs input")[:150]
    return "needs input"


async def _drain_background_tasks() -> None:
    """Wait for any fire-and-forget asyncio.create_task calls claude_loop_
    fired during this run (compact_checkpoint is the one that does this —
    it also runs a memory-review pass in the same task, see
    msg_store_._review_memory) to actually finish, instead of letting them
    get silently cancelled.

    asyncio.run() cancels every still-pending task the instant the
    coroutine it was given returns — confirmed empirically, not a rare
    race: a task can even start and then get cut off mid-await. Harmless
    in a long-running process (the TUI's event loop keeps spinning, a
    fire-and-forget task gets its turn eventually) but fatal here — a
    one-shot headless/container invocation exits the moment run_headless
    returns, so without this, a compact_checkpoint fired on the final loop
    iteration would routinely never actually finish writing its summary.

    Uses asyncio.all_tasks() rather than tracking task handles explicitly,
    since claude_loop_ creates these itself inside a call run_headless
    doesn't otherwise reach into. Correct to wait for "everything still
    pending" here specifically because this is the outermost async
    boundary for every headless invocation (a standalone `microcc-headless`
    cron call, or the CRM orchestrator's per-ticket loop) — nothing
    unrelated should be running concurrently in this process.
    """
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def run_headless(
    project_dir, prompt, model=DEFAULT_MODEL, max_loops=None, verbose=False,
    *, kill_state: dict | None = None, registered: bool = False,
):
    """Drive a single ad hoc prompt to completion against project_dir's own
    conversation scope — the same ~/.micro-cc/projects/{name}_{hash}/
    persistence the interactive TUI uses. No manifest, no task tree, just
    this prompt appended to whatever history already lives at project_dir.
    The microcc equivalent of `claude -p`.

    Fully autonomous (dangerous_tools=set()) since nothing is listening on
    stdin to approve a gated tool. Still a single blocking call with the
    same signature and return contract as always — safe to invoke from
    cron exactly as before. The only new behavior (folding in message_session_ mail, real
    PAUSED on a question) is a no-op when nobody's messaging this project_dir
    and nothing asks a question, which is the plain-cron case.

    question_asked gets a real PAUSED status instead of a fake answer: we
    deliberately leave event["answered"]["answered"] unset, which makes
    claude_loop itself yield "cancelled" and return (claude_loop_.py:347 —
    the same short-circuit Esc-to-reject uses in the live TUI) rather than
    letting the model fabricate a STATUS line for a question nobody answered.
    Because that abort happens before the turn's assistant message is ever
    appended to msgs, the question itself is never persisted by claude_loop —
    we append a synthetic STATUS: PAUSED message ourselves so the outcome is
    visible on disk to whoever (a monitor tool, a relaunch) looks next.

    max_loops caps the number of tool-call rounds (claude_loop's own
    "turn_boundary" events, one per round that used a tool) before this run
    starts winding down instead of running unattended forever. Hitting the
    cap doesn't cut the generator immediately: it splices a plain user
    message onto msgs (the same safe turn_boundary point _fold_mail uses)
    telling the model the limit is reached and to wrap up, then grants up to
    WRAP_UP_LOOPS more turn_boundary rounds, reminding again — with a
    shrinking round count — at each one. If the model finishes on its own
    (final_text) at any point during that grace period, this returns
    normally, no PAUSED. Only once WRAP_UP_LOOPS reminders have all gone by
    with no final_text does this actually aclose() the generator and report
    PAUSED — same relaunch-to-continue contract as a real question_asked
    PAUSED: just `microcc-headless <same project_dir> "<same or refined
    prompt>"` again to pick the conversation back up. None (the default)
    means unlimited, same as before this cap existed. Every stop/splice
    point here is right after a turn_boundary specifically because that's
    the one place per iteration claude_loop's own docstring already calls
    out as safe to splice a plain user message onto msgs — tool_result for
    that round is already appended and persisted (store_msgs already ran
    off the tool_result event) by the time turn_boundary fires, so neither
    the reminder splice nor the final aclose() ever leaves a dangling
    tool_use with no matching tool_result.

    Self-registers with the caller's subagent tracker when
    MICROCC_CALLER_PROJECT_DIR is set — bash_tool_'s bash_() stamps every
    subprocess it launches with that env var (its own caller's real
    project_dir, not text to parse), so a `microcc-headless ...` invocation
    spawned from inside a boss's bash_ call always knows exactly who to
    report to, with zero string-matching. This replaced an earlier
    bash_tool_-side scheme that tried to detect+parse the spawn from the
    shell command text after the fact — regex/shlex over an arbitrary
    command string can't reliably tell "a real microcc-headless invocation"
    apart from "the literal string microcc-headless appearing anywhere"
    (e.g. `ps aux | grep microcc-headless`), and mistracked garbage
    project_dirs in production. A plain `microcc-headless <dir> "<prompt>"`
    typed directly into a terminal has no such env var set, so it's simply
    not tracked by anyone — correct, since there's no boss to report to.

    Registration also enforces the concurrent-subagent cap (settings
    "max_subagents", /subagents) — see _register. registered=True skips it
    when the detached-spawn parent's child already registered (see
    _spawn_detached).

    kill_state: the CLI's signal handler (_run_cli) sets kill_state["signal"]
    and cancels this task. The cancel may be swallowed inside claude_loop
    (it breaks on CancelledError) or surface here; either way, once the flag
    is set this stops leftover background work and ends the transcript
    validly with STATUS: FAILED (see _finalize_killed) instead of leaving a
    dangling tool_use. Embedders that don't pass it keep plain cancellation.
    """
    max_tokens = effective_trim_budget(model)
    os.makedirs(project_dir, exist_ok=True)
    caller_project_dir = os.environ.get("MICROCC_CALLER_PROJECT_DIR")
    if not registered and caller_project_dir and caller_project_dir != project_dir:
        # Refuse before touching messages.jsonl at all — a second run against
        # project_dir while one's still live would otherwise race the first
        # over the same conversation history, not just the tracker entry.
        refusal = _register(caller_project_dir, project_dir)
        if refusal:
            return {"status": "FAILED", "summary": refusal, "body": ""}
    msgs = load_msgs(project_dir)
    _fold_mail(msgs, project_dir)  # pick up anything sent before this run started
    final_content = None
    error_msg = None
    paused_question = None
    loop_count = 0
    wrap_count = 0
    wrapping = False
    loop_limit_hit = False
    in_flight = []  # tool names called since the last turn_boundary — for a kill summary
    _write_activity(project_dir, "model")

    loop_gen = claude_loop(
        query=prompt,
        msgs=msgs,
        project_dir=project_dir,
        model=model,
        max_tokens=max_tokens,
        dangerous_tools=set(),
    )
    try:
        async for event in loop_gen:
            etype = event.get("type")

            if etype == "tool_call":
                in_flight.append(event.get("name", "?"))
                _write_activity(project_dir, "tools", in_flight)
            if etype == "turn_boundary":
                in_flight = []
                _write_activity(project_dir, "model")
                _fold_mail(msgs, project_dir)
            elif etype == "question_asked":
                paused_question = {"name": event["name"], "input": event["input"]}
                # answered["answered"] stays unset on purpose — see docstring.
            elif etype == "final_text":
                final_content = msgs[-1]["content"]
            elif etype == "error":
                error_msg = event.get("message", "unknown error")

            if verbose and etype == "tool_call":
                print(f"⟐ {event['name']} {json.dumps(event['input'])[:200]}", file=sys.stderr, flush=True)
            elif verbose and etype == "tool_result":
                print(f"  → {event['output'][:200]}", file=sys.stderr, flush=True)

            if etype in ("tool_call", "tool_result", "final_text", "done", "error"):
                store_msgs(project_dir, msgs)

            if etype == "turn_boundary":
                loop_count += 1
                if wrapping:
                    wrap_count += 1
                    if wrap_count >= WRAP_UP_LOOPS:
                        loop_limit_hit = True
                        await loop_gen.aclose()
                        break
                    msgs.append(_wrap_up_reminder(max_loops, WRAP_UP_LOOPS - wrap_count))
                elif max_loops is not None and loop_count >= max_loops:
                    wrapping = True
                    msgs.append(_wrap_up_reminder(max_loops, WRAP_UP_LOOPS))
    except asyncio.CancelledError:
        if not (kill_state and kill_state.get("signal")):
            raise
        try:
            await loop_gen.aclose()
        except BaseException:
            pass

    if kill_state and kill_state.get("signal"):
        task = asyncio.current_task()
        while task is not None and task.cancelling():
            task.uncancel()  # the stop was handled; later awaits must not see it
        summary = _finalize_killed(msgs, kill_state["signal"], in_flight)
        store_msgs(project_dir, msgs)
        await _stop_background_work()
        return {"status": "FAILED", "summary": summary, "body": ""}

    try:
        await _drain_background_tasks()
    except asyncio.CancelledError:
        # Killed after the run itself finished: its result is already on disk,
        # so stop waiting on stragglers and report that result normally.
        if not (kill_state and kill_state.get("signal")):
            raise
        asyncio.current_task().uncancel()
        await _stop_background_work()

    if loop_limit_hit:
        summary = f"max loops ({max_loops}) reached, {WRAP_UP_LOOPS} wrap-up round(s) exhausted"
        msgs.append({"role": "assistant", "content": f"STATUS: PAUSED — {summary}"})
        store_msgs(project_dir, msgs)
        return {"status": "PAUSED", "summary": summary, "body": ""}

    if paused_question is not None:
        summary = _summarize_question(paused_question)
        msgs.append({"role": "assistant", "content": f"STATUS: PAUSED — {summary}"})
        store_msgs(project_dir, msgs)
        return {"status": "PAUSED", "summary": summary, "body": json.dumps(paused_question)}

    if error_msg:
        return {"status": "FAILED", "summary": error_msg, "body": ""}

    if isinstance(final_content, str) and final_content.strip():
        status, summary, body = status_marker.parse(final_content)
        return {"status": status, "summary": summary, "body": body}

    return {"status": "FAILED", "summary": "no final response", "body": ""}


def print_result(result):
    symbol = status_marker.SYMBOL[result["status"]]
    print(f"{symbol} {result['status']} — {result['summary']}")
    if result["body"]:
        print(f"\n{result['body']}")


# --- detached spawn ----------------------------------------------------------
# A boss spawns a subagent with a plain foreground `microcc-headless <dir>
# "<prompt>"` via bash_. That foreground process only registers the run and
# reports the verdict; the actual run is a detached child. So a refusal (cap
# reached, name in use) comes straight back as the bash_ tool result, instead
# of being printed later into a backgrounded `&` job's output nobody reads.
#
#   parent (bash_'s child)            child (own session, stdio -> headless.log)
#   ──────────────────────            ─────────────────────────────────────────
#   Popen(-m start_headless_ ...) ──▶ _register() under the tracker flock
#   write raw prompt on stdin          write "OK <pid>" / "REFUSED <why>" on fd
#   read verdict (≤ 90s) ◀──────────── run (if OK), STATUS line to transcript
#   print it, exit 0 / 2
#
# Fork+exec (not a bare fork): the child starts with a clean interpreter, no
# inherited threads or half-initialised macOS frameworks. start_new_session:
# the child leaves bash_'s process group, so a bash_ timeout's killpg can't
# take it down and bash_'s survivor scan doesn't list it — the tracker (pid
# recorded by the child itself) is the one place it's tracked.
_VERDICT_FD_ENV = "MICROCC_HEADLESS_VERDICT_FD"
SPAWN_VERDICT_TIMEOUT = 90  # child imports + registers; generous for a starved host


def _headless_log_path(project_dir: str) -> str:
    return str(_get_storage_dir(project_dir) / "headless.log")


def _read_verdict(fd: int, timeout: float) -> str | None:
    """First line written on fd, or None on EOF (child died) / timeout."""
    buf = b""
    deadline = time.monotonic() + timeout
    while b"\n" not in buf:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        ready, _, _ = select.select([fd], [], [], left)
        if not ready:
            return None
        chunk = os.read(fd, 4096)
        if not chunk:
            break
        buf += chunk
    line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
    return line or None


def _log_tail(path: str, n: int = 15) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 8192))
            return "\n".join(f.read().decode("utf-8", errors="replace").splitlines()[-n:])
    except OSError:
        return ""


def _spawn_detached(args, raw_prompt: str, project_dir: str, model: str) -> int:
    """Parent side of the detached spawn (see the diagram above). Prints the
    verdict and returns the exit code: 0 started, 2 refused, 1 child failed
    to start, 3 no verdict in time (child left running)."""
    name = os.path.basename(project_dir.rstrip("/")) or project_dir
    log_path = _headless_log_path(project_dir)
    argv = [sys.executable, "-m", "micro_cc.start_headless_", project_dir, "--model", model]
    if args.max_loops is not None:
        argv += ["--max-loops", str(args.max_loops)]
    for f in args.files:
        argv += ["-f", os.path.abspath(os.path.expanduser(f))]
    if args.verbose:
        argv.append("--verbose")

    r, w = os.pipe()
    with open(log_path, "a") as log:
        log.write(f"\n=== {datetime.datetime.now().isoformat(timespec='seconds')} spawn ===\n")
        log.flush()
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**os.environ, _VERDICT_FD_ENV: str(w)},
            pass_fds=(w,),
            start_new_session=True,
        )
    os.close(w)
    try:
        proc.stdin.write(raw_prompt.encode("utf-8"))
        proc.stdin.close()
    except BrokenPipeError:
        pass  # child died before reading — the verdict read below reports it

    verdict = _read_verdict(r, SPAWN_VERDICT_TIMEOUT)
    os.close(r)
    as_json = args.output_format == "json"

    def _emit(status: str, summary: str, **extra) -> None:
        if as_json:
            print(json.dumps({"status": status, "summary": summary, "project_dir": project_dir, "log": log_path, **extra}))
        else:
            print(f"{summary}")

    if verdict and verdict.startswith("OK "):
        pid = int(verdict.split()[1])
        _emit(
            "STARTED",
            f"▶ STARTED — {name} running detached as pid {pid} (log: {log_path}). You'll be "
            f"woken at its checkpoint (DONE/FAILED/PAUSED/NEEDS_INPUT); stop it with `kill {pid}`.",
            pid=pid,
        )
        return 0
    if verdict and verdict.startswith("REFUSED "):
        _emit("REFUSED", f"✗ REFUSED — {name}: {verdict[len('REFUSED '):]}")
        return 2
    if proc.poll() is None:
        _emit(
            "UNCONFIRMED",
            f"? UNCONFIRMED — {name} (pid {proc.pid}) did not confirm registration within "
            f"{SPAWN_VERDICT_TIMEOUT}s (host overloaded?). It was left running; check it with "
            f"monitor_ check or its log ({log_path}) before spawning it again.",
            pid=proc.pid,
        )
        return 3
    _emit("FAILED", f"✗ FAILED — {name} exited before starting (code {proc.returncode}). Log tail:\n{_log_tail(log_path)}")
    return 1


def _send_verdict(fd: int, line: str) -> None:
    try:
        os.write(fd, (line.replace("\n", " ") + "\n").encode("utf-8"))
    except OSError:
        pass  # parent gone (timed out and exited) — the run itself still proceeds
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


async def _run_cli(project_dir, prompt, model, max_loops, verbose, registered):
    """run_headless under CLI signal handling: first SIGTERM/SIGINT stops
    gracefully (see run_headless's kill_state), a second one exits at once.
    Only the CLI installs these — an embedder calling run_headless directly
    keeps its own signal handling."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    kill_state: dict = {}

    def _on_signal(sig: signal.Signals) -> None:
        if kill_state:
            os._exit(128 + sig.value)
        kill_state["signal"] = sig.name
        task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal, sig)
    return await run_headless(
        project_dir, prompt, model=model, max_loops=max_loops, verbose=verbose,
        kill_state=kill_state, registered=registered,
    )


def start_headless_():
    parser = argparse.ArgumentParser(
        description="Run a single prompt headlessly, no TUI/GUI — the "
        "microcc equivalent of `claude -p`. Point it at a project_dir and "
        "a prompt; it picks up that dir's existing conversation history, "
        "runs to completion, prints the outcome, and exits with a real "
        "status code. Invoke it from cron whenever, no manifest required. "
        "When spawned by a micro-cc session's bash_ (a subagent), it instead "
        "registers, detaches, and returns at once with STARTED or REFUSED.",
    )
    parser.add_argument("project_dir", help="Directory scoping this run's conversation history")
    parser.add_argument("prompt", nargs="?", help="Prompt text; reads stdin if omitted")
    parser.add_argument("-f", "--file", action="append", default=[], dest="files",
                         help="File to point the prompt at (repeatable)")
    parser.add_argument("--model", default=None,
                         help="Exact alias/slug for the current backend. Omit to reuse "
                         "whatever's actually configured (~/.micro-cc/settings.json's "
                         "\"model\" — set at /login for every backend, including a "
                         "free-text OpenRouter/Ollama model) instead of the Anthropic "
                         "registry default.")
    parser.add_argument("--max-loops", type=int, default=None,
                         help="Cap tool-call rounds before pausing with STATUS: PAUSED instead "
                         "of running unattended forever — relaunch the same project_dir to "
                         "pick the conversation back up. Omit for unlimited (default).")
    parser.add_argument("--output-format", choices=["text", "json"], default="text")
    parser.add_argument("--verbose", action="store_true",
                         help="Stream tool calls/results to stderr as they happen")
    parser.add_argument("--foreground", action="store_true",
                         help="Subagent spawns only: run in this process and block until done "
                         "instead of detaching (the old `&`-less behavior).")
    args = parser.parse_args()

    # Child of a detached spawn: take the verdict fd out of the environment so
    # this run's own bash_ children never inherit it.
    verdict_fd = os.environ.pop(_VERDICT_FD_ENV, None)

    if not has_configured_endpoint():
        if verdict_fd is not None:
            _send_verdict(int(verdict_fd), "REFUSED no API endpoint configured — run `microcc` once and use /login")
        print("No API endpoint configured — run `microcc` once and use /login", file=sys.stderr)
        sys.exit(1)

    raw_prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    if not raw_prompt or not raw_prompt.strip():
        if verdict_fd is not None:
            _send_verdict(int(verdict_fd), "REFUSED empty prompt")
        print("Error: prompt must be provided as an argument or via stdin", file=sys.stderr)
        sys.exit(1)

    project_dir = os.path.abspath(os.path.expanduser(args.project_dir))
    model = args.model or get_setting("model") or DEFAULT_MODEL
    caller_project_dir = os.environ.get("MICROCC_CALLER_PROJECT_DIR")
    is_subagent = bool(caller_project_dir) and caller_project_dir != project_dir

    registered = False
    if verdict_fd is not None:
        refusal = _register(caller_project_dir, project_dir) if is_subagent else None
        _send_verdict(int(verdict_fd), f"REFUSED {refusal}" if refusal else f"OK {os.getpid()}")
        if refusal:
            print(f"✗ REFUSED — {refusal}")
            sys.exit(2)
        registered = True
        print(f"[headless] pid {os.getpid()} running {project_dir} (model {model})", flush=True)
    elif is_subagent and not args.foreground:
        os.makedirs(project_dir, exist_ok=True)
        sys.exit(_spawn_detached(args, raw_prompt.strip(), project_dir, model))

    prompt = _build_prompt(raw_prompt.strip(), args.files)
    result = asyncio.run(_run_cli(
        project_dir, prompt, model, args.max_loops, args.verbose, registered,
    ))

    if args.output_format == "json":
        print(json.dumps({**result, "project_dir": project_dir}))
    else:
        print_result(result)

    sys.exit(0 if result["status"] == "DONE" else 1)


if __name__ == "__main__":
    start_headless_()
