"""Boot supervisor: restart TUI with wheel reinstall on boot failure (max 3 attempts; never on a source checkout)."""

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


def _is_source_checkout() -> bool:
    """True when running from a source tree; self-contained so a broken tree can't break it."""
    here = os.path.abspath(__file__)
    return "site-packages" not in here and "dist-packages" not in here


def _project_dir_from_argv() -> str:
    """Get project dir from argv (matches start_live_tui_ resolution)."""
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
    """Mark boot successful; clears consecutive-failure counter."""
    try:
        _save_entry(project_dir, attempts=0, booted=True)
    except Exception:
        pass


# Evict stock libs shadowed by paper-* forks after install
_STOCK_TO_PAPER = {"python-pptx": "paper-pptx", "python-docx": "paper-docx", "openpyxl": "paper-xlsx"}


def _uv() -> str | None:
    return shutil.which("uv") or next(
        (p for p in (os.path.expanduser("~/.local/bin/uv"), os.path.expanduser("~/.cargo/bin/uv")) if os.path.exists(p)), None)


def install_cmd(*pkgs: str, force: bool = True, no_deps: bool = False) -> list[str]:
    """Build install command (pip or uv, preferring pip)."""
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
    """Uninstall stock office libs shadowed by paper-* forks; return error tail or None."""
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


def _start_catalog_refresh() -> None:
    """Start background thread to refresh model catalog cache."""
    try:
        import threading

        from micro_cc.models.catalog_ import refresh_remote_catalog

        threading.Thread(target=refresh_remote_catalog, name="catalog-refresh", daemon=True).start()
    except Exception:
        pass


def main() -> None:
    project_dir = _project_dir_from_argv()
    entry = _entry(project_dir)
    attempts = int(entry.get("attempts", 0))
    source = _is_source_checkout()

    if attempts >= MAX_BOOT_ATTEMPTS and not source:  # Budget exhausted; try start once more
        sys.stderr.write(_give_up_message(project_dir))
        _start_catalog_refresh()
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

    _save_entry(project_dir, attempts=attempts + 1, booted=False)  # Assume worst, clear on success
    _start_catalog_refresh()

    try:
        from micro_cc.start_live_tui_ import start_
        start_()
    except BaseException:
        if isinstance(sys.exc_info()[1], KeyboardInterrupt):  # User quit is not a boot failure
            sys.exit(0)

        tb = traceback.format_exc()
        _log_crash(tb)

        if _entry(project_dir).get("booted"):  # Reached UI, crashed after; don't reinstall
            sys.stderr.write(tb)
            sys.exit(1)

        if source:  # A wheel would shadow the dev tree in site-packages; leave the fix to the developer
            sys.stderr.write(tb)
            sys.stderr.write(f"[ micro-cc ] boot failed on a source checkout, not reinstalling. Crash log: {_CRASH_LOG}\n")
            sys.exit(1)

        if _reinstall():  # UI never started; tree is broken
            _relaunch()
        sys.stderr.write(tb)
        sys.exit(1)
    else:
        note_boot_ok(project_dir)  # clean exit — reset the counter
        sys.exit(0)


if __name__ == "__main__":
    main()
