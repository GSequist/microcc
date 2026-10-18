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
from typing import Optional

from micro_cc.utils.helpers import _IMAGE_EXTENSIONS, sanitize_and_encode_image_
from micro_cc.utils.msg_store_ import _get_storage_dir


_SHELL = shutil.which("bash") or "/bin/bash"

# Env vars stripped from bash_ subprocesses: built-in and provider keys, plus MICROCC_BASH_ENV_DENYLIST.
_DENIED_ENV = {"ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN", "OPENAI_API_KEY", "MICRO_CC_POSTGRES_URL", "MICRO_CC_MIRROR_POSTGRES_URL"}


def _bash_env() -> dict:
    from micro_cc.models.providers import secret_env_keys
    denied = _DENIED_ENV | secret_env_keys() | {n for n in os.environ.get("MICROCC_BASH_ENV_DENYLIST", "").split(",") if n}
    return {k: v for k, v in os.environ.items() if k not in denied}

# macOS and WSL only; native Windows is refused up front (no POSIX process-group semantics).
_WINDOWS_MSG = (
    "[bash_ requires a POSIX shell — native Windows isn't supported. "
    "Install WSL (`wsl --install` in PowerShell, then `pip install micro-cc` "
    "inside the WSL shell) and run micro-cc from there.]"
)

# Last cwd per project_dir, so `cd` carries over between bash_ calls.
_cwd_state: dict[str, str] = {}

# Survivors of backgrounded `cmd &` launches, drained for the per-loop status reminder.
_background_procs: dict[int, dict] = {}

# Persistent pipe drains for survivors, keyed by pgid; cancelled once the group exits.
_output_drain_tasks: dict[int, tuple] = {}

# Cached aliases/functions from an interactive shell, sourced into bash_ commands.
_snapshot_path: str | None = None
_snapshot_attempted = False
_snapshot_lock = asyncio.Lock()

# No command-level checks here: the TUI approval gate covers bash_ per tool call.


async def _ensure_shell_snapshot() -> str | None:
    """Capture aliases/functions from an interactive shell once per process; failures are cached too."""
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
                # New session: no /dev/tty access, so a sourced rc file can't reach the TUI's terminal.
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
    """Like _drain_loop, but appends to `path` on disk so a backgrounded survivor never blocks or gets SIGPIPE."""
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

# Last-line patterns of an interactive prompt, checked when a background process stalls.
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
    """Decode the 3-digit octal escapes BSD `ps` emits for non-printable argv bytes (e.g. newlines)."""
    def _decode(m):
        try:
            return chr(int(m.group(1), 8))
        except ValueError:
            return m.group(0)
    return _PS_OCTAL_ESCAPE_RE.sub(_decode, s)


# Marks bash_'s wrapper script; a backgrounded compound command's ps argv shows it, so fall back to the raw command.
_WRAPPER_SCRIPT_MARKER = "__mcc_ec="


async def _track_survivors(pgid: int, command: str, cwd: str, project_dir: str) -> Path | None:
    """Record processes still alive in bash_'s process group after it exited; return their output path or None."""
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


# One-shot "pid finished" notices, drained by format_background_status.
_finished_procs: list[dict] = []

# One-shot "looks stuck" notices, drained by format_background_status.
_stalled_procs: list[dict] = []


def _check_stall(pid: int, info: dict) -> None:
    """Flag a background pid as stalled: output stopped growing and the tail looks like a prompt."""
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
        # Not a prompt: keep watching; re-tail only after growth resumes or the threshold passes again.
        info["last_growth_at"] = now
        return
    info["stall_notified"] = True
    _stalled_procs.append({
        "pid": pid, "command": info["command"], "cwd": info["cwd"], "tail": tail,
    })


def list_background_processes() -> list[dict]:
    """Prune exited processes and return survivors for the UI pill."""
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
    # Cancel a pgid's drain tasks only once no live pid still uses it.
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
    """Format new, finished and stuck background processes for the per-loop reminder; each notice shows once."""
    procs = list_background_processes()
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
    """SIGKILL leftover bash_ process groups on headless-subagent SIGTERM; returns the count signalled."""
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
    """SIGKILL the whole process group (start_new_session makes proc.pid its pgid)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def _drain_loop(pipe, chunks: list):
    """Copy pipe output into `chunks` as it arrives; ends at EOF or when cancelled."""
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
    """Wait for a drain task to reach EOF, capped at `timeout`; never cancels it."""
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


def _snapshot_image_mtimes(cwd: str) -> dict[str, float]:
    """Mtimes of top-level image files in `cwd`, taken before the command runs; non-recursive by design."""
    try:
        return {
            f: os.path.getmtime(os.path.join(cwd, f))
            for f in os.listdir(cwd)
            if f.lower().endswith(_IMAGE_EXTENSIONS) and os.path.isfile(os.path.join(cwd, f))
        }
    except OSError:
        return {}


def _newly_created_image(cwd: str, before: dict[str, float]) -> str | None:
    """Most recently modified image in `cwd` that is new or changed since `before`; one per call."""
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
    timeout: int = 120,
    description: Optional[str] = None
) -> str:
    """Execute a bash command.

    Args:
        command: The bash command to execute
        path: Working directory (default: last cwd for this project, initially project_dir); relative paths resolve to project_dir
        timeout: Max seconds before the process is killed (default 120)
        description: Short description of what you are about to do, in active voice
    """
    if platform.system() == "Windows":
        return _WINDOWS_MSG

    max_output_chars = 60000

    # `cd` persists via _cwd_state across calls.
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

    # Aliases load, and the cwd capture runs even on non-zero exit (newline-joined, not &&).
    script_lines = []
    if snapshot_path:
        script_lines.append(f"source {shlex.quote(snapshot_path)} 2>/dev/null || true")
        script_lines.append("shopt -s expand_aliases 2>/dev/null || true")
    # Project .env is sourced into the subshell each call, never into os.environ.
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
        # Popen, not asyncio: a `cmd &` child holding the pipes would stall asyncio's wait().
        proc = subprocess.Popen(
            [_SHELL, "-c", full_command],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Lets microcc-headless children self-register with this caller's subagent tracker.
            env={**_bash_env(), "TERM": "dumb", "MICROCC_CALLER_PROJECT_DIR": project_dir},
            # New session: no controlling tty; proc.pid doubles as the pgid for _kill_tree.
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

        # Drains hit EOF on their own; cap the wait at 50ms for a `cmd &` child holding a pipe.
        await asyncio.gather(
            _drain_or_timeout(stdout_task, 0.05),
            _drain_or_timeout(stderr_task, 0.05),
        )

        survivor_output_path = await _track_survivors(proc.pid, command, cwd, project_dir)
        if survivor_output_path is not None:
            # Live survivor pipes go to _drain_to_file; already-EOF'd ones are just cancelled.
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

        # Display-only: rendered by the TUI; the model still gets the plain string.
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
        # Sync cwd from `pwd -P`; skipped if the command timed out or crashed before it ran.
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
