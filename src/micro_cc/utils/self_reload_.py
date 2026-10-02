"""Restart-on-self-change: detect that the running harness's own source
changed on disk, then re-exec the interpreter into a fresh process.

micro-cc is plain .py files loaded straight from a source tree (an editable
install points sys.path at src/), so "the running code" IS the files on disk.
When the model (or the user) edits those files, the running process keeps
executing the code objects it already imported — a change to claude_loop_.py
or a tool body is invisible until a new process imports them again. This
module supplies the two halves of the fix:

  1. a cheap manifest/diff over the package's own .py files, so a caller can
     tell *that* something changed and *which* files (mtime_ns + size — no
     content hashing; we only need change detection, and the fresh process
     reads the current bytes anyway, so the restart IS the diff-apply);
  2. exec_relaunch(), the re-exec itself.

Why restart and not importlib.reload(): reload() rebinds a module's
namespace, but every already-imported `from x import y` binding, every live
closure, and every instantiated class keeps pointing at the OLD code objects.
The two files you most want to rewrite — the agent loop and the TUI that owns
the event loop — are exactly the ones whose frames are currently running, so
there is no safe partial swap. A fresh process is the only honest mechanism,
and it is cheap here because the conversation is already durable on disk
(msg_store_ writes every message to messages.jsonl; start_() rebuilds from
it) — see pi's ctx.reload()/deepseek's "process restart is the adoption
boundary" for the same conclusion reached from the other direction.

The relaunch targets micro_cc.self_heal_ (not start_live_tui_ directly) so the
supervisor's crash-recovery still guards every relaunched process: if the new
code fails to boot, self_heal_ reinstalls the published wheel over the broken
local tree and tries again, bounded.
"""

import os
import sys

# This file lives directly in the package dir, so its own dirname IS the
# resolved package path — editable checkout or real pip install alike (same
# fact claude_loop_.py relies on for package_dir).
_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Relaunch through the supervisor, never straight into the TUI — see module
# docstring. Kept as a constant so screen_cmds_._run_update and the TUI's own
# self-reload share one definition of "where a restart lands".
_ENTRY_MODULE = "micro_cc.self_heal_"

_SUFFIXES = (".py",)
_SKIP_DIRS = {"__pycache__"}


def is_source_checkout() -> bool:
    """True when the running package is a source tree rather than a
    site-packages copy. Only a source checkout has local files worth
    watching — a normal pip install's files don't change under the running
    process, so the poll loop is a no-op there. Reuses self_update_'s own
    detection rather than re-deriving it (that function exists for exactly
    this source-vs-installed question)."""
    from micro_cc.tui_native.self_update_ import _is_source_checkout
    return _is_source_checkout()


def build_manifest(root: str | None = None) -> dict:
    """{abs_path: (mtime_ns, size)} for every .py file under the package dir.

    Deliberately the whole package tree (tools/, tui_native/, utils/, skills/
    scripts) — "self" is the harness's own source, and any of it can change
    behavior. Cheap enough to run every few seconds: a few hundred stat()
    calls, no file reads.
    """
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
    return manifest


def diff_manifest(old: dict, new: dict) -> set:
    """Paths added, removed, or whose (mtime_ns, size) changed between two
    manifests. mtime_ns is nanosecond-resolution on macOS/Linux, so two edits
    within the same second are still distinguished."""
    changed = set()
    for path, sig in new.items():
        if old.get(path) != sig:
            changed.add(path)
    for path in old:
        if path not in new:
            changed.add(path)
    return changed


# ---------------------------------------------------------------- prompt stash
# The unsent prompt is the one piece of genuinely-live state a restart would
# otherwise eat (todos live in state_store, also in-memory, but they're the
# model's scratchpad and re-derived on the next turn's tool call; a half-typed
# prompt the user is looking at is not). Sidecar lives beside messages.jsonl
# in the project's own storage dir, same home as everything else durable.

def _prompt_sidecar(project_dir: str):
    from micro_cc.utils.msg_store_ import _get_storage_dir
    return _get_storage_dir(project_dir) / "self_reload_prompt.txt"


def save_pending_prompt(project_dir: str, text: str) -> None:
    if not text or not text.strip():
        return
    try:
        _prompt_sidecar(project_dir).write_text(text)
    except OSError:
        # Bookkeeping, not the job — losing a half-typed prompt must never
        # abort the restart itself.
        pass


def take_pending_prompt(project_dir: str) -> str:
    """Read-and-delete: a stashed prompt is restored exactly once, by the
    process that comes up right after the restart. If that process then
    crashes before the user sees it, the text is gone rather than resurfacing
    on every subsequent boot."""
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
    """Replace this process image with a fresh interpreter running the
    supervisor. Does not return on success.

    `-m` with the explicit module name (not sys.argv[0]) is load-bearing —
    argv[0] is a stale snapshot (a console-script shim path, or an absolute
    file path) and blindly re-running it can reload pre-change code instead of
    forcing the fresh sys.path lookup. Same reasoning screen_cmds_._run_update
    already documented for its own re-exec.

    Callers MUST have torn the terminal down first (MicroTui.stop() restores
    termios, exits the alt screen, pops the Kitty keyboard protocol) — execv
    does not unwind anything for you.
    """
    argv = list(sys.argv[1:] if extra_argv is None else extra_argv)
    os.execv(sys.executable, [sys.executable, "-m", _ENTRY_MODULE] + argv)
