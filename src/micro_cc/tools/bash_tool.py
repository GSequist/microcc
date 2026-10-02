import asyncio
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from micro_cc.utils.helpers import _IMAGE_EXTENSIONS, sanitize_and_encode_image_
from micro_cc.utils.msg_store_ import _get_storage_dir


_SHELL = shutil.which("bash") or "/bin/bash"

# Secrets stripped from every bash_ subprocess env. Built-ins cover microcc's
# own; MICROCC_BASH_ENV_DENYLIST (comma-separated) adds an embedding app's own.
_DENIED_ENV = {"ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN", "OPENAI_API_KEY", "MICRO_CC_POSTGRES_URL"}


def _bash_env() -> dict:
    denied = _DENIED_ENV | {n for n in os.environ.get("MICROCC_BASH_ENV_DENYLIST", "").split(",") if n}
    return {k: v for k, v in os.environ.items() if k not in denied}

# Supported platforms are macOS and WSL — both are real POSIX/bash
# environments, so nothing below is platform-conditional. Native (non-WSL)
# Windows has no bash and no POSIX process-group/signal semantics for the
# kill/pipe logic below, so it's refused up front rather than half-supported.
_WINDOWS_MSG = (
    "[bash_ requires a POSIX shell — native Windows isn't supported. "
    "Install WSL (`wsl --install` in PowerShell, then `pip install micro-cc` "
    "inside the WSL shell) and run micro-cc from there.]"
)

# Sticky cwd per project_dir: lets `cd` inside one bash_ call carry over to
# the next, like a real shell session, instead of resetting to project_dir
# on every call.
_cwd_state: dict[str, str] = {}

# Processes a `cmd &`-style backgrounded launch left running after its
# parent bash_ call returned. Populated by _track_survivors below; drained
# for the per-loop <process-status> reminder in claude_loop_.py so the model
# finds out its server/watcher/etc is still alive without a dedicated tool
# call, and can stop it with a plain `bash_("kill <pid>")`.
# Each entry also carries "pgid" and "output_path" — see _drain_to_file and
# get_output_tail below for how those get populated and read back.
_background_procs: dict[int, dict] = {}

# Drain tasks handed off from a bash_ call's own stdout/stderr pipes once a
# survivor is detected (see _drain_to_file) — kept here, keyed by pgid, so
# they aren't garbage-collected out from under the still-running reader and
# can be cancelled once every pid in that group has exited (see
# list_background_processes' pruning).
_output_drain_tasks: dict[int, tuple] = {}

# One-time snapshot of the user's interactive aliases/functions, so bash_
# commands behave like a real terminal instead of a bare, alias-less `bash -c`.
_snapshot_path: str | None = None
_snapshot_attempted = False
_snapshot_lock = asyncio.Lock()

# No command-level restrictions here by design: this tool has no visibility
# into whether it's gated (TUI's dangerous_tools/approval_request already
# covers bash_ end-to-end at the tool-call level -- see claude_loop_.py).
# Baking a second, command-level check in here would either duplicate that
# gate or silently override it, so it doesn't live here.


async def _ensure_shell_snapshot() -> str | None:
    """Capture aliases/functions from an interactive shell once per process
    and reuse them on every bash_ call. Cached including failures, so a
    broken snapshot attempt isn't retried on every single call."""
    global _snapshot_path, _snapshot_attempted
    async with _snapshot_lock:
        if _snapshot_attempted:
            return _snapshot_path
        _snapshot_attempted = True
        fd, path = tempfile.mkstemp(prefix="micro_cc_shell_snapshot_")
        os.close(fd)
        try:
            proc = await asyncio.create_subprocess_exec(
                _SHELL, "-i", "-c",
                f"{{ alias -p; declare -f; }} > {shlex.quote(path)} 2>/dev/null",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Same isolation as the main bash_ subprocess below, and for
                # the same reason: this is "-i", so it sources .bashrc/.zshrc.
                # If a startup file shells out to something like `sudo` that
                # can't prompt via its (DEVNULL) stdin, it falls back to
                # opening /dev/tty directly — without this flag that reaches
                # the REAL terminal the TUI owns, racing Textual's own raw
                # keystroke reads and corrupting input.
                start_new_session=True,
            )
            await asyncio.wait_for(proc.wait(), timeout=5)
            if os.path.getsize(path) > 0:
                _snapshot_path = path
            else:
                os.unlink(path)
        except Exception:
            try:
                os.unlink(path)
            except OSError:
                pass
        return _snapshot_path


def _bgproc_output_dir(project_dir: str) -> Path:
    d = _get_storage_dir(project_dir) / "bgproc_output"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _drain_to_file(pipe, path: Path):
    """Persistent counterpart to _drain_loop for a backgrounded survivor.

    _drain_loop stops reading the moment bash_'s own call returns (see the
    0.05s grace period + _cancel in bash_ below) — fine for the common case,
    but a `cmd &` child inherited the same pipe and keeps writing to it long
    after that, with nothing left to read it. Left unread, the child either
    blocks on write() once the pipe buffer fills or gets SIGPIPE the moment
    we let proc.stdout/stderr get garbage-collected and close our end.

    This keeps the exact same non-blocking add_reader loop _drain_loop uses,
    just appending each chunk to `path` on disk instead of an in-memory list
    the caller has already returned past. Runs until real EOF (the child
    finally exits and every write-end holder has closed) or until cancelled
    (list_background_processes' pruning, once the pid it belongs to is gone).
    """
    fd = pipe.fileno()
    os.set_blocking(fd, False)
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    try:
        out = open(path, "ab")
    except OSError:
        out = None

    def _on_readable():
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            loop.remove_reader(fd)
            if not done.done():
                done.set_result(None)
            return
        if out is not None:
            try:
                out.write(chunk)
                out.flush()
            except OSError:
                pass

    loop.add_reader(fd, _on_readable)
    try:
        await done
    finally:
        try:
            loop.remove_reader(fd)
        except (ValueError, OSError):
            pass
        if out is not None:
            out.close()


STALL_THRESHOLD_S = 45
_STALL_TAIL_CHARS = 1024

# Last-line patterns suggesting a backgrounded process is blocked on interactive
# input: output stopped growing AND the tail reads like a confirmation prompt.
_PROMPT_PATTERNS = [
    re.compile(r"\(y/n\)", re.I),
    re.compile(r"\[y/n\]", re.I),
    re.compile(r"\(yes/no\)", re.I),
    re.compile(r"\b(?:Do you|Would you|Shall I|Are you sure|Ready to)\b.*\?\s*$", re.I),
    re.compile(r"Press (any key|Enter)", re.I),
    re.compile(r"Continue\?", re.I),
    re.compile(r"Overwrite\?", re.I),
]


def _looks_like_prompt(tail: str) -> bool:
    last_line = tail.rstrip().splitlines()[-1] if tail.strip() else ""
    return any(p.search(last_line) for p in _PROMPT_PATTERNS)


_PS_OCTAL_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


def _unescape_ps_octal(s: str) -> str:
    """BSD/macOS `ps -o command=` escapes any non-printable byte in a
    process's argv — most commonly a literal newline — as a backslash plus
    3-digit octal code (`\\012` for `\\n`) instead of the real character.
    Decode those back so a multi-line command reads as actual lines rather
    than literal backslash-digit noise."""
    def _decode(m):
        try:
            return chr(int(m.group(1), 8))
        except ValueError:
            return m.group(0)
    return _PS_OCTAL_ESCAPE_RE.sub(_decode, s)


# Unique to the wrapper script bash_() itself builds below (source snapshot,
# env sourcing, the actual command, then this literal exit-code capture) —
# see bash_()'s script_lines. A `cmd &`-backgrounded COMPOUND command (a
# `for`/`while`/`{ ...; }`) runs as a forked-but-never-exec'd copy of that
# same wrapper bash process for its own job-control subshell, so `ps`
# reports THAT subshell's argv as the entire multi-line wrapper script, not
# the user's actual command — fork() alone never changes argv, only exec()
# does. Detecting that marker and falling back to the original `command`
# argument (what the model actually asked to run) beats trying to make
# sense of our own plumbing leaking through ps's output.
_WRAPPER_SCRIPT_MARKER = "__mcc_ec="


async def _track_survivors(pgid: int, command: str, cwd: str, project_dir: str) -> Path | None:
    """Record any process still alive in bash_'s process group after it exited.

    start_new_session=True made bash's own pid the group id, so once bash
    itself has exited, anything `ps -g` still finds there is a `cmd &` child
    bash left running behind it — e.g. a dev server the model backgrounded.

    Runs on every single bash_ call, so this must not block the event loop:
    a synchronous subprocess.run() here stalls whatever else is in flight
    (other gathered tool calls, the API stream, bash stdout draining on a
    concurrent call) for the ps roundtrip. asyncio.create_subprocess_exec
    yields instead of blocking.

    killpg(pgid, 0) below is a single syscall -- no process spawn -- and
    tells us whether anything is still alive in the group. That's true for
    the rare `cmd &` case and false for the overwhelming majority of calls,
    so it lets us skip forking `ps` (and its own fork/exec cost) on every
    plain `ls`/`git status`/etc. instead of paying for it unconditionally.

    Returns the output-file path survivors should have their pipes drained
    into (see bash_'s own call site, which hands stdout/stderr off to
    _drain_to_file instead of cancelling them), or None when nothing
    survived — the overwhelming majority of calls.
    """
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-o", "pid=,command=", "-g", str(pgid),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
    except Exception:
        return None
    out_path = _bgproc_output_dir(project_dir) / f"{pgid}.log"
    found = False
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        pid_str, _, raw_cmd = line.partition(" ")
        try:
            pid = int(pid_str)
        except ValueError:
            continue
        found = True
        cmd = _unescape_ps_octal(raw_cmd.strip())
        if _WRAPPER_SCRIPT_MARKER in cmd:
            display_cmd = command.strip().splitlines()[-1][:160]
        else:
            display_cmd = cmd[-160:] or command.strip().splitlines()[-1][:160]
        _background_procs[pid] = {
            "command": display_cmd,
            "cwd": cwd,
            "started_at": time.time(),
            "pgid": pgid,
            "output_path": str(out_path),
            "notified": False,
            "last_output_size": 0,
            "last_growth_at": time.time(),
            "stall_notified": False,
        }
    return out_path if found else None


# One-shot "this pid just finished" notices, queued by list_background_processes'
# pruning below and drained by format_background_status. Mirrors
# subagent_tracker_'s notified-flag pattern (see that module's docstring):
# a still-running process is worth polling for on demand (monitor_/bash_
# ps), a state CHANGE (started, finished) is worth pushing once. Plain
# list, not keyed by pid, since a finished entry is removed from
# _background_procs the same tick it's queued here — nothing to dedupe.
_finished_procs: list[dict] = []

# One-shot "this one looks stuck" notices — same drain-once contract as
# _finished_procs, queued by _check_stall below and drained by
# format_background_status.
_stalled_procs: list[dict] = []


def _check_stall(pid: int, info: dict) -> None:
    """Piggybacks on list_background_processes' own per-call pass (run every
    3s regardless of busy/idle by start_live_tui_'s _poll_bgprocs_tick, and
    on every claude_loop_ status build) instead of a dedicated timer — cheap
    when nothing's stalled: one stat() call; the tail-read + regex only run
    once growth has genuinely stopped for STALL_THRESHOLD_S."""
    path = info.get("output_path")
    if not path or info.get("stall_notified"):
        return
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    now = time.time()
    if size > info.get("last_output_size", 0):
        info["last_output_size"] = size
        info["last_growth_at"] = now
        return
    if now - info.get("last_growth_at", now) < STALL_THRESHOLD_S:
        return
    tail = get_output_tail(pid, _STALL_TAIL_CHARS)
    if not tail or not _looks_like_prompt(tail):
        # Not a prompt — keep watching, but don't re-tail on every call until
        # growth resumes or another full STALL_THRESHOLD_S has passed.
        info["last_growth_at"] = now
        return
    info["stall_notified"] = True
    _stalled_procs.append({
        "pid": pid, "command": info["command"], "cwd": info["cwd"], "tail": tail,
    })


def list_background_processes() -> list[dict]:
    """Prune exited processes and return survivors — the same self-scoped
    set format_background_status formats for the model, structured for a
    UI pill instead (see start_live_.py/webui/server.py)."""
    for pid in list(_background_procs):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            info = _background_procs.pop(pid)
            _finished_procs.append({
                "pid": pid, "command": info["command"], "cwd": info["cwd"],
                "output_path": info["output_path"],
            })
        else:
            _check_stall(pid, _background_procs[pid])
    # A pgid's drain tasks (see _drain_to_file) outlive any one child pid
    # entry — only cancel them once nothing left in _background_procs still
    # points at that pgid, i.e. every pid ps -g ever found there is gone.
    live_pgids = {info["pgid"] for info in _background_procs.values()}
    for pgid in list(_output_drain_tasks):
        if pgid not in live_pgids:
            for task in _output_drain_tasks.pop(pgid):
                if not task.done():
                    task.cancel()
    return [
        {"pid": pid, "command": info["command"], "cwd": info["cwd"],
         "age": int(time.time() - info["started_at"]),
         "stalled": info.get("stall_notified", False)}
        for pid, info in _background_procs.items()
    ]


def get_output_tail(pid: int, max_chars: int = 2000) -> str | None:
    """Last bit of captured stdout/stderr for a tracked background pid (see
    _drain_to_file for how it lands on disk). None if nothing's been
    captured yet — the process hasn't written anything, or already had its
    own stdout/stderr redirected elsewhere before we ever got to look."""
    info = _background_procs.get(pid)
    path = info.get("output_path") if info else None
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_chars))
            data = f.read()
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    if size > max_chars:
        text = "...[truncated]...\n" + text
    return text


def format_background_status() -> str | None:
    """Format state CHANGES for the per-loop reminder — same notify-once
    contract as subagent_tracker_ (see that module's docstring: still-alive
    is worth showing once, up front; after that it's pull-on-demand via
    monitor_/bash_, not a repeated per-loop restatement). Concretely:
    - a newly-tracked pid gets exactly one "started" line, right after
      list_background_processes() has already pruned/queued any finished
      ones for this same call, so the notified flag flips only for pids
      that are still genuinely alive this tick.
    - a pid that died since the last check gets exactly one "finished" line,
      drained from _finished_procs and not re-shown.
    An already-notified, still-running pid is silent here — the model was
    told once it exists and can check on it itself; no per-iteration nag.
    """
    procs = list_background_processes()  # prunes dead pids into _finished_procs first
    lines = []
    if _finished_procs:
        for f in _finished_procs:
            lines.append(f"  PID {f['pid']} finished: {f['command']}  (cwd={f['cwd']}) — output was captured to {f['output_path']}, read it if you need to check what happened")
        _finished_procs.clear()
    if _stalled_procs:
        for s in _stalled_procs:
            lines.append(
                f"  PID {s['pid']} looks stuck: {s['command']}  (cwd={s['cwd']}) — output hasn't "
                f"grown in {STALL_THRESHOLD_S}s and the last line looks like an interactive prompt. "
                f'Recent output:\n    {s["tail"][-300:]}\n'
                f'  Consider bash_("kill {s["pid"]}") and re-running non-interactively '
                f"(pipe an answer, e.g. `yes | cmd`, or pass a --yes/--force flag)."
            )
        _stalled_procs.clear()
    new_procs = [p for p in procs if not _background_procs[p["pid"]]["notified"]]
    for p in new_procs:
        lines.append(f"  PID {p['pid']} started: {p['command']}  (cwd={p['cwd']}) — stop with bash_(\"kill {p['pid']}\"), check output with monitor_([\"{p['pid']}\"])")
        _background_procs[p["pid"]]["notified"] = True
    if not lines:
        return None
    return "Background process update:\n" + "\n".join(lines)


def kill_background_processes() -> int:
    """SIGKILL every process group this process's bash_ calls left running
    (`cmd &` survivors). Used when a headless subagent is stopped with
    SIGTERM: those groups live in their own sessions, so killing the
    subagent's python process alone would orphan them — a killed-and-
    relaunched subagent would then compete with its predecessor's leftover
    heavy commands. Not called on a normal finish, where a deliberately
    backgrounded server may be meant to outlive the run. Returns the number
    of groups signalled."""
    killed = 0
    for pgid in {info["pgid"] for info in _background_procs.values()}:
        try:
            os.killpg(pgid, signal.SIGKILL)
            killed += 1
        except (ProcessLookupError, PermissionError):
            pass
    _background_procs.clear()
    return killed


def _kill_tree(proc):
    """SIGKILL the whole process group, not just the direct bash process.

    start_new_session=True makes proc.pid its own process-group id, so this
    also reaps anything the command piped into or backgrounded — a plain
    proc.kill() only hits bash itself and orphans the rest.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def _drain_loop(pipe, chunks: list):
    """Copy whatever's available on `pipe` into `chunks` as it arrives.

    Runs concurrently with the process, not after it — without this, a
    command producing more than the OS pipe buffer (~64KB) blocks on
    write() forever since nothing reads until wait() returns. Left running
    until cancelled; a backgrounded `cmd &` child inheriting this pipe
    would otherwise keep it open past bash's own exit, so we never wait for
    EOF here — the caller cancels once bash itself has exited.

    Event-driven via loop.add_reader (epoll/kqueue), not a poll-sleep loop:
    the previous version woke up every 10ms to check os.read() even when
    the process was silent, which taxes every concurrent bash_ call (real
    load in headless with several agents running at once) for
    CPU no one needed, and added up to 10ms of pure lag per chunk on
    streaming output. add_reader instead fires the callback exactly when
    the fd has data, with no idle wakeups and no added latency.
    """
    fd = pipe.fileno()
    os.set_blocking(fd, False)
    loop = asyncio.get_running_loop()
    done = loop.create_future()

    def _on_readable():
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:  # real EOF: every holder of the write end has closed it
            loop.remove_reader(fd)
            if not done.done():
                done.set_result(None)
            return
        chunks.append(chunk)

    loop.add_reader(fd, _on_readable)
    try:
        await done
    finally:
        try:
            loop.remove_reader(fd)
        except (ValueError, OSError):
            pass


async def _cancel(task):
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _drain_or_timeout(task, timeout: float):
    """Wait for a drain task to hit real EOF, capped at `timeout`.

    shield keeps a timeout from cancelling `task` itself here — the caller
    always runs _cancel(task) right after regardless of which way this
    exits, so cleanup is uniform either way.
    """
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


def _snapshot_image_mtimes(cwd: str) -> dict[str, float]:
    """Top-level image files in `cwd` and their current mtime, taken right
    before the command runs — the baseline _newly_created_image compares
    against. Non-recursive on purpose: matplotlib/PIL/etc. save to the
    working directory in the overwhelming common case (a script writing
    to some nested output/ path is the same signal a human would have to
    go looking for too), and scanning the whole tree on every bash_ call
    is real, avoidable cost for a rare case."""
    try:
        return {
            f: os.path.getmtime(os.path.join(cwd, f))
            for f in os.listdir(cwd)
            if f.lower().endswith(_IMAGE_EXTENSIONS) and os.path.isfile(os.path.join(cwd, f))
        }
    except OSError:
        return {}


def _newly_created_image(cwd: str, before: dict[str, float]) -> str | None:
    """Path of the most recently modified image in `cwd` that's new or
    changed since `before` — the command's own output, not a screenshot
    that happened to already be sitting there. One image per call, not
    every image ever produced: keeps the shape simple (see bash_'s
    docstring note on why this is intentionally narrow for v1)."""
    try:
        candidates = []
        for f in os.listdir(cwd):
            if not f.lower().endswith(_IMAGE_EXTENSIONS):
                continue
            full = os.path.join(cwd, f)
            if not os.path.isfile(full):
                continue
            mtime = os.path.getmtime(full)
            if f not in before or mtime > before[f]:
                candidates.append((mtime, full))
        if not candidates:
            return None
        return max(candidates)[1]
    except OSError:
        return None


async def bash_(
    command: str,
    *,
    project_dir: str,
    path: str = None,
    timeout: int = 120
) -> str:
    """Execute bash command.

    Args:
        command: The bash command to execute
        path: Working directory (default: last known cwd for this project, initially project_dir). Relative paths resolve to project_dir.
        timeout: Max seconds before killing process (default 120)
    """
    if platform.system() == "Windows":
        return _WINDOWS_MSG

    max_output_chars = 60000

    # Determine cwd. `cd` inside a previous command call carries over via
    # _cwd_state, so this behaves like one continuous shell session rather
    # than resetting to project_dir on every call.
    if path:
        if os.path.isabs(path):
            cwd = path
        else:
            cwd = os.path.normpath(os.path.join(project_dir, path))
    else:
        cwd = _cwd_state.get(project_dir, project_dir)

    if not os.path.isdir(cwd):
        return f"[Directory not found: {cwd}]"

    if _SHELL is None:
        return "[No bash found on this machine]"

    snapshot_path = await _ensure_shell_snapshot()

    cwd_fd, cwd_file = tempfile.mkstemp(prefix="micro_cc_cwd_")
    os.close(cwd_fd)

    # Wrap the command so we can (a) source captured aliases/functions and
    # (b) capture the resulting cwd for the next call, regardless of the
    # command's own exit code. Newline-separated (not &&) so `pwd -P` still
    # runs even if `command` exits non-zero.
    script_lines = []
    if snapshot_path:
        script_lines.append(f"source {shlex.quote(snapshot_path)} 2>/dev/null || true")
        script_lines.append("shopt -s expand_aliases 2>/dev/null || true")
    # Project-scoped secrets (see /keys): sourced fresh every call, straight
    # off disk, so a .env written mid-session is picked up immediately with
    # no reload step and without ever touching this long-running process's
    # own os.environ.
    project_env_path = os.path.join(project_dir, ".env")
    if os.path.isfile(project_env_path):
        script_lines.append(f"set -a; source {shlex.quote(project_env_path)} 2>/dev/null || true; set +a")
    script_lines.append(command)
    script_lines.append("__mcc_ec=$?")
    script_lines.append(f"pwd -P > {shlex.quote(cwd_file)}")
    script_lines.append("exit $__mcc_ec")
    image_mtimes_before = _snapshot_image_mtimes(cwd)
    full_command = "\n".join(script_lines)

    try:
        # Plain subprocess.Popen, not asyncio.create_subprocess_exec: asyncio's
        # subprocess transport ties proc.wait()/returncode to ALL stdio pipes
        # reaching EOF, not just the process exiting. A backgrounded `cmd &`
        # child inherits those same pipes and can hold them open indefinitely,
        # which made the asyncio version hang for the full timeout on every
        # backgrounded launch. Popen.wait() (run in an executor thread below)
        # reflects the shell's own exit immediately, independent of who still
        # holds the pipes.
        proc = subprocess.Popen(
            [_SHELL, "-c", full_command],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # MICROCC_CALLER_PROJECT_DIR: lets any microcc-headless process
            # this shell (or a descendant of it) launches self-register with
            # OUR subagent tracker on startup — see start_headless_.py's
            # run_headless. Always set, unconditionally: cheap, and correct
            # by construction (project_dir here is the real caller identity
            # the framework handed this tool call, not text to pattern-match
            # against). Replaces a former bash_tool_-side regex/shlex scan
            # over the command string trying to detect+parse a
            # microcc-headless invocation after the fact — that approach
            # mistracked `mkdir`/`|` as project_dirs the moment a command
            # chained multiple spawns or ran an unrelated `grep
            # microcc-headless`, twice in production (see
            # subagent_tracker_.py's module docstring).
            env={**_bash_env(), "TERM": "dumb", "MICROCC_CALLER_PROJECT_DIR": project_dir},
            # Detach into a new session: no controlling terminal, own
            # process group. Without this, a child that opens /dev/tty
            # directly (ssh -t, vim, another Textual app, ...) reaches the
            # REAL terminal the host TUI owns — writes its own raw-mode/
            # alt-screen escape codes onto it and steals keyboard input,
            # which is exactly the "bleeds onto terminal and breaks the
            # app" failure. New session also means proc.pid is a process
            # group id, so a timeout can kill the whole tree, not just bash.
            start_new_session=True,
        )

        stdout_chunks: list = []
        stderr_chunks: list = []
        stdout_task = asyncio.ensure_future(_drain_loop(proc.stdout, stdout_chunks))
        stderr_task = asyncio.ensure_future(_drain_loop(proc.stderr, stderr_chunks))

        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(loop.run_in_executor(None, proc.wait), timeout=timeout)
        except asyncio.TimeoutError:
            _kill_tree(proc)
            await _cancel(stdout_task)
            await _cancel(stderr_task)
            return f"[Command timed out after {timeout}s]"
        except asyncio.CancelledError:
            _kill_tree(proc)
            await _cancel(stdout_task)
            await _cancel(stderr_task)
            raise  # re-raise so gather/task cancellation propagates

        # bash exited. If nothing backgrounded a child that inherited these
        # pipes, the drain loops already hit real EOF within one 10ms poll
        # tick of proc.wait() returning — waiting a flat 50ms unconditionally
        # on every single call taxed the common (non-backgrounding) case for
        # no reason. Still cap at 50ms so a `cmd &` child holding a pipe open
        # gets the same grace period as before; it just doesn't cost the
        # other 99% of calls anymore.
        await asyncio.gather(
            _drain_or_timeout(stdout_task, 0.05),
            _drain_or_timeout(stderr_task, 0.05),
        )

        survivor_output_path = await _track_survivors(proc.pid, command, cwd, project_dir)
        if survivor_output_path is not None:
            # A `cmd &` child is still alive and holding these pipes open —
            # hand the still-live ones off to a persistent drain instead of
            # cancelling them (see _drain_to_file), so its future output
            # keeps landing on disk instead of getting SIGPIPE'd once we let
            # proc.stdout/stderr get garbage-collected. A pipe that already
            # hit real EOF here (task.done()) means that particular stream
            # was already closed/redirected elsewhere by the child itself —
            # nothing to hand off, cancel is a no-op either way.
            handed_off = []
            if stdout_task.done():
                await _cancel(stdout_task)
            else:
                handed_off.append(asyncio.ensure_future(_drain_to_file(proc.stdout, survivor_output_path)))
            if stderr_task.done():
                await _cancel(stderr_task)
            else:
                handed_off.append(asyncio.ensure_future(_drain_to_file(proc.stderr, survivor_output_path)))
            if handed_off:
                _output_drain_tasks[proc.pid] = tuple(handed_off)
        else:
            await _cancel(stdout_task)
            await _cancel(stderr_task)

        stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")

        # Build output
        if proc.returncode != 0:
            output = f"[Exit code: {proc.returncode}]\n"
            if stderr:
                output += f"STDERR:\n{stderr}\n"
            if stdout:
                output += f"STDOUT:\n{stdout}"
        else:
            output = stdout
            if stderr:
                output += f"\n[STDERR]: {stderr}"

        # Truncate if too long (keep head + tail)
        if len(output) > max_output_chars:
            half = max_output_chars // 2
            output = (
                output[:half] +
                f"\n\n... [TRUNCATED {len(output) - max_output_chars} chars] ...\n\n" +
                output[-half:]
            )

        output_text = output.strip() or "[No output]"

        # Display-only, never vision: unlike read_'s {"type": "image", ...}
        # (which claude_loop_ routes as a real tool_result image block, a
        # genuine vision call), this shape keeps `output` as the normal
        # string tool_result Claude reasons about exactly as before —
        # display_image only ever reaches the TUI's own render path. A
        # bash command that happens to produce an image (a matplotlib
        # chart, say) doesn't silently cost a vision call on every run;
        # the model can still look at it explicitly via its own read_
        # call on the file, same as today.
        image_path = _newly_created_image(cwd, image_mtimes_before)
        if image_path:
            encoded = sanitize_and_encode_image_(image_path)
            if encoded is not None:
                return {"output": output_text, "display_image": encoded}

        return output_text

    except asyncio.CancelledError:
        raise  # always propagate
    except Exception as e:
        return f"[Bash error: {type(e).__name__}: {e}]"
    finally:
        # Pick up wherever `command` left the shell (via `cd`), so the next
        # call starts there instead of resetting to project_dir. Empty/missing
        # on timeout or a crash before `pwd -P` ran -- just skip the update.
        try:
            with open(cwd_file, "r") as f:
                new_cwd = f.read().strip()
            if new_cwd:
                _cwd_state[project_dir] = new_cwd
        except OSError:
            pass
        try:
            os.unlink(cwd_file)
        except OSError:
            pass
