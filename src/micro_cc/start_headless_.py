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
from micro_cc.utils.inbox_store_ import peek_mail, ack_mail
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
    """Append pending inbox mail to msgs, flush and ack."""
    mail = peek_mail(project_dir)
    if not mail:
        return
    for item in mail:
        tag = f"#{item['id'][:12]}"
        header = f"[incoming from {item['from']} {tag}]"
        if not any(
            m.get("role") == "user" and isinstance(m.get("content"), str) and m["content"].startswith(header)
            for m in msgs
        ):
            msgs.append({"role": "user", "content": f"{header}\n{item['text']}"})
    store_msgs(project_dir, msgs)
    ack_mail(project_dir, [item["id"] for item in mail])


WRAP_UP_LOOPS = 5  # extra turn_boundary rounds granted after max_loops, one reminder each


def _wrap_up_reminder(max_loops: int, loops_left: int) -> dict:
    """Emit a wrap-up reminder when approaching loop limit."""
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
    """Register with tracker; return refusal message or None."""
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
    """End transcript after SIGTERM/SIGINT with valid STATUS: FAILED line."""
    tail = msgs[-1] if msgs else None
    dangling = []
    if tail is not None and tail.get("role") == "assistant" and isinstance(tail.get("content"), list):
        dangling = [_field(b, "id") for b in tail["content"] if _field(b, "type") == "tool_use"]
    if dangling:  # Answer unanswered tool calls from the incomplete turn
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
    """Stop background processes, watches, and asyncio tasks after kill signal."""
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
    """Log current phase and tools for monitor diagnostics."""
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
    """Wait for fire-and-forget asyncio tasks to finish before process exits."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def run_headless(
    project_dir, prompt, model=DEFAULT_MODEL, max_loops=None, verbose=False,
    *, kill_state: dict | None = None, registered: bool = False,
):
    """Run prompt to completion; persists to ~/.micro-cc/projects/{name}_{hash}/. Autonomous with max_loops cap and signal handling."""
    max_tokens = effective_trim_budget(model)
    os.makedirs(project_dir, exist_ok=True)
    caller_project_dir = os.environ.get("MICROCC_CALLER_PROJECT_DIR")
    if not registered and caller_project_dir and caller_project_dir != project_dir:
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


# --- detached spawn --
# Parent spawns foreground process to register, detached child runs actual work
_VERDICT_FD_ENV = "MICROCC_HEADLESS_VERDICT_FD"
SPAWN_VERDICT_TIMEOUT = 90  # child imports + registers; generous for a starved host


def _headless_log_path(project_dir: str) -> str:
    return str(_get_storage_dir(project_dir) / "headless.log")


def _read_verdict(fd: int, timeout: float) -> str | None:
    """Read first line from fd with timeout, or None."""
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
    """Spawn detached child; return 0=started, 2=refused, 1=failed, 3=no-verdict."""
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
    """Run headless with CLI signal handling (graceful on first, exit on second)."""
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
