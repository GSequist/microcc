"""Block idle system sleep while a turn or background work runs; the display may still sleep.
One inhibitor shared by named holders, kept LINGER_S after the last one leaves so back-to-back turns
reuse it. macOS caffeinate, Linux systemd-inhibit, WSL a Windows PowerShell holding
SetThreadExecutionState, native Windows ctypes. Never blocks the caller."""

import os
import platform
import shutil
import subprocess
import tempfile
import threading

LINGER_S = 60.0

_lock = threading.RLock()
_holders: set[str] = set()
_active = False      # an inhibitor was started and not stopped
_proc: subprocess.Popen | None = None
_method: str | None = None
_error: str | None = None
_failed = False      # it died or could not start: no retry until every holder has left
_stderr = None       # temp file: why an inhibitor exited
_linger: threading.Timer | None = None

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
    global _proc, _method, _error, _stderr, _active, _failed
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
        _method, _error, _active = "SetThreadExecutionState", None, True
        return
    method, argv = _command()
    if method is None:
        _method, _error, _failed = None, argv, True
        return
    try:
        _stderr = tempfile.TemporaryFile()
        _proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=_stderr, start_new_session=True)
        _method, _error, _active = method, None, True
    except OSError as e:
        _proc, _method, _error, _failed = None, None, f"{method} failed: {e}", True


def _reap(proc: subprocess.Popen, err_file) -> None:
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    if err_file is not None:
        err_file.close()


def _stop() -> None:
    """Signal the inhibitor and reap it on a thread: never stalls the event loop."""
    global _proc, _stderr, _active
    if os.name == "nt":
        if _active:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
        _active = False
        return
    proc, err_file, _proc, _stderr, _active = _proc, _stderr, None, None, False
    if proc is None:
        if err_file is not None:
            err_file.close()
        return
    try:
        proc.stdin.close()  # PowerShell exits on EOF
    except OSError:
        pass
    try:
        proc.terminate()
    except OSError:
        pass
    threading.Thread(target=_reap, args=(proc, err_file), daemon=True).start()


def _died_reason() -> str:
    try:
        _stderr.seek(0)
        err = _stderr.read().decode("utf-8", "replace").strip()[-300:]
    except (OSError, AttributeError, ValueError):
        err = ""
    return f"{_method} exited with code {_proc.returncode}" + (f": {err}" if err else "")


def _check_died() -> None:
    global _error, _failed
    if _proc is not None and not _failed and _proc.poll() is not None:
        _error, _failed = _died_reason(), True


def _linger_done() -> None:
    global _linger, _failed
    with _lock:
        _linger = None
        if not _holders:
            _stop()
            _failed = False


def hold(tag: str, on: bool = True) -> None:
    """Add or drop holder `tag`; the inhibitor runs while any holder remains, plus LINGER_S. Idempotent."""
    global _linger
    with _lock:
        (_holders.add if on else _holders.discard)(tag)
        _check_died()
        if _holders:
            if _linger is not None:
                _linger.cancel()
                _linger = None
            if not _active and not _failed:
                _start()
        elif _linger is None and (_active or _failed):
            _linger = threading.Timer(LINGER_S, _linger_done)
            _linger.daemon = True
            _linger.start()


def release_all() -> None:
    """App shutdown: stop now, no linger."""
    global _linger, _failed
    with _lock:
        _holders.clear()
        if _linger is not None:
            _linger.cancel()
            _linger = None
        _stop()
        _failed = False


def status() -> dict:
    with _lock:
        _check_died()
        alive = _active and (os.name == "nt" or (_proc is not None and _proc.poll() is None))
        return {"held": bool(_holders) and alive, "holders": sorted(_holders), "method": _method,
                "error": _error, "pid": _proc.pid if _proc else None}
