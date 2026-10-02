"""Supervisor entry point: run the TUI, and if it fails to BOOT, reinstall
the published micro-cc wheel over whatever is on disk and try again.

This is the second half of the "malleable harness" mechanism (the first is
utils/self_reload_.py's restart-on-self-change). Because the harness is plain
.py files loaded from a source tree, the model can edit its own code — and an
edit can break the tree so the app no longer starts. Without a supervisor that
failure is unrecoverable from inside the app: the process that would report
the error is the process that won't start.

So the TUI is now launched through this module (pyproject's `microcc` console
script points here, and self_reload_.exec_relaunch re-execs into here). It:

  1. runs the real app (start_live_tui_.start_),
  2. on a *boot* failure — start_ raised before the app ever signalled it was
     up — runs the same `pip install --force-reinstall micro-cc` the /update
     command uses, then re-execs itself,
  3. bounds that: at most MAX_BOOT_ATTEMPTS consecutive boot failures, then it
     stops reinstalling and reports recovery instructions instead of looping.

"Booted" is a real signal, not a timer guess: start_live_tui_.start() calls
note_boot_ok() once it is up, which clears the attempt counter. So the counter
only ever accumulates *consecutive failures to reach a running UI* — a
long-lived session that later crashes on some unrelated bug is not a boot
failure and does not trigger a reinstall.

Deliberately stdlib-only at module scope, and it imports nothing from
micro_cc outside the guarded block in main() — a broken harness module must
surface as a caught boot failure here, not as an ImportError that kills the
supervisor before it can act.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import traceback

MAX_BOOT_ATTEMPTS = 3

_STATE_PATH = os.path.join(os.path.expanduser("~"), ".micro-cc", "self_heal_state.json")
_CRASH_LOG = os.path.join(os.path.expanduser("~"), ".micro-cc", "boot_crash.log")

_ENTRY_MODULE = "micro_cc.self_heal_"


def _project_dir_from_argv() -> str:
    """Same resolution start_live_tui_.start_ uses, so the state key matches
    the project the app will actually open."""
    if len(sys.argv) > 1:
        return os.path.abspath(sys.argv[1])
    return os.getcwd()


def _load_all() -> dict:
    try:
        with open(_STATE_PATH) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_all(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        with open(_STATE_PATH, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def _entry(project_dir: str) -> dict:
    return _load_all().get(project_dir) or {"attempts": 0, "booted": False}


def _save_entry(project_dir: str, attempts: int, booted: bool) -> None:
    data = _load_all()
    data[project_dir] = {"attempts": attempts, "booted": booted, "ts": time.time()}
    _write_all(data)


def note_boot_ok(project_dir: str) -> None:
    """Called by start_live_tui_.start() the moment the app is actually up.
    Clearing the counter here — rather than on clean exit — is what makes the
    bound count *consecutive boot failures* instead of every restart in a
    session: a deliberate self-reload (utils/self_reload_) re-execs through
    this supervisor and increments the counter, and this clears it right back
    once the new process proves it renders. Never raises."""
    try:
        _save_entry(project_dir, attempts=0, booted=True)
    except Exception:
        pass


# Stock libs whose import name a paper-* fork now owns. pip installs both side
# by side (different distribution names), the forks refuse to import in that
# state, and a wheel has no install hook to evict the old one — so every
# install path we drive evicts it after installing.
_STOCK_TO_PAPER = {"python-pptx": "paper-pptx", "python-docx": "paper-docx", "openpyxl": "paper-xlsx"}


def _uv() -> str | None:
    return shutil.which("uv") or next(
        (p for p in (os.path.expanduser("~/.local/bin/uv"), os.path.expanduser("~/.cargo/bin/uv")) if os.path.exists(p)), None)


def install_cmd(*pkgs: str, force: bool = True, no_deps: bool = False) -> list[str]:
    """Install into the running env: pip when present, else uv (uv tool envs ship without pip)."""
    if importlib.util.find_spec("pip") or not _uv():
        return [sys.executable, "-m", "pip", "install", "--upgrade", *(["--force-reinstall"] if force else []),
                *(["--no-deps"] if no_deps else []), "--quiet", "--no-input", "--disable-pip-version-check", *pkgs]
    return [_uv(), "pip", "install", "--python", sys.executable, "--upgrade", *(["--reinstall"] if force else []),
            *(["--no-deps"] if no_deps else []), "--quiet", *pkgs]


def uninstall_cmd(*pkgs: str) -> list[str]:
    if importlib.util.find_spec("pip") or not _uv():
        return [sys.executable, "-m", "pip", "uninstall", "-y", "--quiet", "--no-input", "--disable-pip-version-check", *pkgs]
    return [_uv(), "pip", "uninstall", "--python", sys.executable, "--quiet", *pkgs]


def _installed(dist: str) -> bool:
    from importlib.metadata import PackageNotFoundError, distribution
    try:
        distribution(dist)
        return True
    except PackageNotFoundError:
        return False


def evict_stock_office_libs() -> str | None:
    """Uninstall stock libs shadowed by an installed paper fork, then
    force-reinstall that fork (the uninstall deletes files both share).
    Run only after a successful install, so an offline failure leaves the old
    working env untouched. Returns an error tail, or None."""
    stock = [s for s, p in _STOCK_TO_PAPER.items() if _installed(s) and _installed(p)]
    if not stock:
        return None
    for cmd in (uninstall_cmd(*stock), install_cmd(*[_STOCK_TO_PAPER[s] for s in stock], no_deps=True)):
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            return "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-5:])
    return None


def _reinstall() -> bool:
    """The /update install, verbatim (see screen_cmds_._run_update) — force a
    fresh copy of the published wheel over the local tree. Returns True on
    success. On a source checkout this deliberately clobbers the editable
    install with the released package: that is the whole point of the recovery
    path (get back to something that starts), and the recovery message says so
    so nobody is surprised their dev tree stopped being the loaded code."""
    cmd = install_cmd("micro-cc")
    sys.stderr.write("\n[ micro-cc ] boot failed — reinstalling the published wheel…\n")
    sys.stderr.flush()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        sys.stderr.write(f"[ micro-cc ] reinstall could not start: {e}\n")
        return False
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-5:])
        sys.stderr.write(f"[ micro-cc ] reinstall failed:\n{tail}\n")
        return False
    err = evict_stock_office_libs()
    if err:
        sys.stderr.write(f"[ micro-cc ] removing stock python-pptx/openpyxl/python-docx failed:\n{err}\n")
        return False
    sys.stderr.write("[ micro-cc ] reinstall ok — relaunching…\n")
    sys.stderr.flush()
    return True


def _log_crash(tb: str) -> None:
    try:
        os.makedirs(os.path.dirname(_CRASH_LOG), exist_ok=True)
        with open(_CRASH_LOG, "a") as f:
            f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] pid={os.getpid()}\n{tb}\n")
    except OSError:
        pass


def _relaunch() -> None:
    os.execv(sys.executable, [sys.executable, "-m", _ENTRY_MODULE] + sys.argv[1:])


def _give_up_message(project_dir: str) -> str:
    return (
        f"[ micro-cc ] couldn't reach a running UI after {MAX_BOOT_ATTEMPTS} "
        "reinstalls.\n"
        f"  crash log: {_CRASH_LOG}\n"
        "  recover manually:\n"
        "    pip install --force-reinstall micro-cc\n"
        f"  (or fix the local source under {os.path.dirname(os.path.abspath(__file__))} "
        "if you're running a checkout)\n"
    )


def main() -> None:
    project_dir = _project_dir_from_argv()
    entry = _entry(project_dir)
    attempts = int(entry.get("attempts", 0))

    # Budget exhausted: don't reinstall again, but still try to start once —
    # if the user has since fixed the tree this succeeds and clears the
    # counter, so a fixed checkout always recovers; only an actually-broken
    # one lands on the give-up message. No reinstall here, so no loop.
    if attempts >= MAX_BOOT_ATTEMPTS:
        sys.stderr.write(_give_up_message(project_dir))
        try:
            from micro_cc.start_live_tui_ import start_
            start_()
        except BaseException:
            tb = traceback.format_exc()
            _log_crash(tb)
            sys.stderr.write(tb)
            sys.exit(1)
        note_boot_ok(project_dir)
        sys.exit(0)

    # Assume the worst until the app says otherwise; note_boot_ok clears this.
    _save_entry(project_dir, attempts=attempts + 1, booted=False)

    try:
        from micro_cc.start_live_tui_ import start_
        start_()
    except BaseException:
        # KeyboardInterrupt (Ctrl+C) is a normal way to leave the app, not a
        # boot failure — treat it as a clean exit so it can't trigger a
        # reinstall. start_live_tui_ already swallows its own Ctrl+C; this
        # guards the path where it propagates.
        if isinstance(sys.exc_info()[1], KeyboardInterrupt):
            sys.exit(0)

        tb = traceback.format_exc()
        _log_crash(tb)

        if _entry(project_dir).get("booted"):
            # The app reached a running UI and crashed later — a real bug,
            # but not a failure to boot. Report it; don't reinstall over a
            # tree that demonstrably starts.
            sys.stderr.write(tb)
            sys.exit(1)

        # Never got a UI up — the tree is broken. Reinstall and relaunch.
        if _reinstall():
            _relaunch()  # does not return on success
        # Reinstall failed (offline, permissions…): report, don't loop.
        sys.stderr.write(tb)
        sys.exit(1)
    else:
        note_boot_ok(project_dir)  # clean exit — reset the counter
        sys.exit(0)


if __name__ == "__main__":
    main()
