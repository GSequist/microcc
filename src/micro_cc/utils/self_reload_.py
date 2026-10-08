"""Detect source changes on disk and re-exec interpreter into fresh process.
Conversation state is durable on disk, so restart is cheap and safe."""

import os
import sys

# Resolved package path (works in both editable and pip-install modes).
_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Relaunch target: supervisor ensures crash recovery on startup.
_ENTRY_MODULE = "micro_cc.self_heal_"

_SUFFIXES = (".py",)
_SKIP_DIRS = {"__pycache__"}


def is_source_checkout() -> bool:
    """True if running package is a source tree (editable install), not site-packages."""
    from micro_cc.tui_native.self_update_ import _is_source_checkout
    return _is_source_checkout()


def _mods_manifest() -> dict:
    """Manifest of every non-hidden file under ~/.micro-cc/mods/ (statusline.sh needs no restart)."""
    from micro_cc.mods_ import mods_dir

    out = {}
    for dirpath, dirnames, filenames in os.walk(mods_dir()):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            path = os.path.join(dirpath, fn)
            try:
                st = os.stat(path)
            except OSError:
                continue
            out[path] = (st.st_mtime_ns, st.st_size)
    return out


def build_manifest(root: str | None = None) -> dict:
    """{abs_path: (mtime_ns, size)} for every .py file under package; cheap to run frequently."""
    with_user = root is None
    root = root or _PKG_DIR
    manifest: dict[str, tuple] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            if not fn.endswith(_SUFFIXES):
                continue
            path = os.path.join(dirpath, fn)
            try:
                st = os.stat(path)
            except OSError:
                continue
            manifest[path] = (st.st_mtime_ns, st.st_size)
    if with_user:
        try:
            manifest.update(_mods_manifest())
        except Exception:
            pass
    return manifest


def diff_manifest(old: dict, new: dict) -> set:
    """Paths added, removed, or changed between manifests (mtime_ns, size)."""
    changed = set()
    for path, sig in new.items():
        if old.get(path) != sig:
            changed.add(path)
    for path in old:
        if path not in new:
            changed.add(path)
    return changed


# Process-wide reload state shared by the TUI poll and the loop; the model decides when to restart.
_state = {"live": False, "manifest": None, "changed": set(), "requested": False}


def init_baseline() -> None:
    """Snapshot the source at startup and mark a TUI present (only it can restart the process)."""
    _state["manifest"] = build_manifest()
    _state["live"] = True


def is_live() -> bool:
    return _state["live"]


def refresh_changes() -> set:
    """Diff disk against the baseline, record new changes and advance the baseline; no-op without a TUI."""
    if _state["manifest"] is None:
        return set()
    current = build_manifest()
    changed = diff_manifest(_state["manifest"], current)
    if changed:
        _state["manifest"] = current
        _state["changed"] |= changed
    return changed


def changed_files() -> list:
    return sorted(_state["changed"])


def request_reload() -> None:
    _state["requested"] = True


def cancel_request() -> None:
    _state["requested"] = False


def reload_requested() -> bool:
    return _state["requested"]


# Unsent prompts are the only live state a restart would eat; persisted in storage dir.

def _prompt_sidecar(project_dir: str):
    from micro_cc.utils.msg_store_ import _get_storage_dir
    return _get_storage_dir(project_dir) / "self_reload_prompt.txt"


def save_pending_prompt(project_dir: str, text: str) -> None:
    if not text or not text.strip():
        return
    try:
        _prompt_sidecar(project_dir).write_text(text)
    except OSError:
        # Don't abort restart if save fails.
        pass


def take_pending_prompt(project_dir: str) -> str:
    """Read-and-delete: restore stashed prompt once, then discard."""
    path = _prompt_sidecar(project_dir)
    try:
        if path.exists():
            text = path.read_text()
            path.unlink()
            return text
    except OSError:
        return ""
    return ""


def exec_relaunch(extra_argv: list | None = None) -> None:
    """Replace process image with fresh supervisor; caller must tear down terminal first."""
    argv = list(sys.argv[1:] if extra_argv is None else extra_argv)
    os.execv(sys.executable, [sys.executable, "-m", _ENTRY_MODULE] + argv)
