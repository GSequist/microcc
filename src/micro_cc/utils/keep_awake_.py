"""Block idle system sleep while a turn or background work runs; the display may still sleep.
One inhibitor process per app, shared by named holders. macOS caffeinate, Linux systemd-inhibit,
WSL a Windows PowerShell holding SetThreadExecutionState, native Windows ctypes."""

import os
import platform
import shutil
import subprocess
import tempfile
import time

_holders: set[str] = set()
_proc: subprocess.Popen | None = None
_method: str | None = None
_error: str | None = None
_started_at = 0.0
_stderr = None  # temp file: why an inhibitor exited
RESTART_AFTER_S = 60  # an inhibitor that dies sooner is broken here; don't respawn it every tick

_ES_CONTINUOUS, _ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001

# Holds ES_SYSTEM_REQUIRED until stdin closes, so it also ends if microcc dies.
_PS_SCRIPT = (
    "$t = Add-Type -Name P -Namespace MicroccAwake -PassThru -MemberDefinition "
    "'[DllImport(\"kernel32.dll\")] public static extern uint SetThreadExecutionState(uint f);'; "
    "[void]$t::SetThreadExecutionState([uint32]2147483649); "
    "$null = [Console]::In.ReadToEnd()"
)


def _is_wsl() -> bool:
    return bool(os.environ.get("WSL_DISTRO_NAME")) or "microsoft" in platform.uname().release.lower()


def _powershell() -> str | None:
    return shutil.which("powershell.exe") or next(
        (p for p in ("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",) if os.path.exists(p)), None)


def _command() -> tuple[str, list] | tuple[None, str]:
    """(method, argv) for this OS, or (None, reason)."""
    pid = str(os.getpid())
    system = platform.system()
    if system == "Darwin":
        return "caffeinate", ["caffeinate", "-i", "-w", pid]
    if system == "Linux" and _is_wsl():
        ps = _powershell()
        if not ps:
            return None, "powershell.exe not reachable from WSL (Windows interop off?)"
        return "powershell", [ps, "-NoProfile", "-NonInteractive", "-Command", _PS_SCRIPT]
    if system == "Linux":
        if not shutil.which("systemd-inhibit"):
            return None, "systemd-inhibit not found"
        return "systemd-inhibit", ["systemd-inhibit", "--what=idle:sleep", "--who=microcc",
                                   "--why=microcc is working", "--mode=block",
                                   "sh", "-c", f"while kill -0 {pid} 2>/dev/null; do sleep 15; done"]
    return None, f"unsupported platform {system}"


def _start() -> None:
    global _proc, _method, _error, _started_at, _stderr
    _started_at = time.monotonic()
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
        _method, _error = "SetThreadExecutionState", None
        return
    method, argv = _command()
    if method is None:
        _method, _error = None, argv
        return
    try:
        _stderr = tempfile.TemporaryFile()
        _proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=_stderr, start_new_session=True)
        _method, _error = method, None
    except OSError as e:
        _proc, _method, _error = None, None, f"{method} failed: {e}"


def _stop() -> None:
    global _proc, _stderr
    if _stderr is not None:
        _stderr.close()
        _stderr = None
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        return
    if _proc is None:
        return
    try:
        _proc.stdin.close()  # PowerShell exits on EOF
    except OSError:
        pass
    _proc.terminate()
    try:
        _proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        _proc.kill()
    _proc = None


def _died_reason() -> str:
    try:
        _stderr.seek(0)
        err = _stderr.read().decode("utf-8", "replace").strip()[-300:]
    except (OSError, AttributeError, ValueError):
        err = ""
    return f"{_method} exited with code {_proc.returncode}" + (f": {err}" if err else "")


def hold(tag: str, on: bool = True) -> None:
    """Add or drop holder `tag`; the inhibitor runs while any holder remains. Idempotent."""
    global _error
    before = bool(_holders)
    (_holders.add if on else _holders.discard)(tag)
    died = _proc is not None and _proc.poll() is not None
    if died and _error is None:
        _error = _died_reason()
    if _holders and (not before or (died and time.monotonic() - _started_at > RESTART_AFTER_S)):
        _stop()
        _start()  # first holder, or the inhibitor died a while after starting: (re)start
    elif before and not _holders:
        _stop()


def release_all() -> None:
    _holders.clear()
    _stop()


def status() -> dict:
    global _error
    alive = os.name == "nt" or (_proc is not None and _proc.poll() is None)
    if _proc is not None and not alive and _error is None:
        _error = _died_reason()
    return {"held": bool(_holders) and alive, "holders": sorted(_holders), "method": _method, "error": _error,
            "pid": _proc.pid if _proc else None}
