"""Executable observations for the self-reload / self-heal guarantees.

Plain script, no pytest needed — run directly:
    python tests/test_self_reload.py
Exit 0 = every property held for the exercised case, 1 = a failure.

Covers the three declared contracts (design/architecture.toml):
  * boot-failure recovery is bounded (self_heal_ never reinstalls forever);
  * the manifest/diff detects a real source change and ignores noise;
  * a restart stashes+restores exactly one unsent prompt.
Nothing here touches the network, the real ~/.micro-cc state, or execv — the
supervisor's decision logic is exercised with start_/reinstall/execv faked.
"""

import contextlib
import io
import os
import sys
import tempfile
import time
import types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from micro_cc.utils import self_reload_ as sr  # noqa: E402
import micro_cc.self_heal_ as sh  # noqa: E402


def _fail(msg):
    sys.stderr.write(f"FAIL: {msg}\n")
    sys.exit(1)


# --------------------------------------------------------------- manifest/diff
def test_manifest_diff():
    d = tempfile.mkdtemp()
    pkg = os.path.join(d, "pkg")
    os.makedirs(os.path.join(pkg, "__pycache__"))
    open(os.path.join(pkg, "a.py"), "w").write("x=1")
    open(os.path.join(pkg, "b.py"), "w").write("y=2")
    open(os.path.join(pkg, "__pycache__", "a.pyc"), "w").write("noise")
    open(os.path.join(pkg, "note.txt"), "w").write("not python")

    m1 = sr.build_manifest(pkg)
    if sorted(os.path.basename(p) for p in m1) != ["a.py", "b.py"]:
        _fail(f"manifest included non-source files: {sorted(m1)}")

    # unchanged -> empty diff
    if sr.diff_manifest(m1, sr.build_manifest(pkg)) != set():
        _fail("identical tree produced a non-empty diff")

    # edit -> exactly the edited file
    time.sleep(0.01)
    open(os.path.join(pkg, "a.py"), "w").write("x=1  # edited")
    changed = sr.diff_manifest(m1, sr.build_manifest(pkg))
    if len(changed) != 1 or not next(iter(changed)).endswith("a.py"):
        _fail(f"edit not detected as a single-file change: {changed}")

    # add and remove are both changes
    m2 = sr.build_manifest(pkg)
    open(os.path.join(pkg, "c.py"), "w").write("z=3")
    m3 = sr.build_manifest(pkg)
    if os.path.join(pkg, "c.py") not in sr.diff_manifest(m2, m3):
        _fail("added file not detected")
    os.remove(os.path.join(pkg, "b.py"))
    if os.path.join(pkg, "b.py") not in sr.diff_manifest(m3, sr.build_manifest(pkg)):
        _fail("removed file not detected")

    # size change with an unchanged-looking edit is still caught
    open(os.path.join(pkg, "a.py"), "w").write("x=1" + " " * 200)
    m4 = sr.build_manifest(pkg)
    if os.path.join(pkg, "a.py") not in sr.diff_manifest(m3, m4):
        _fail("size-only change not detected")


# --------------------------------------------------------------- prompt stash
def test_prompt_stash():
    d = tempfile.mkdtemp()
    from pathlib import Path
    orig = sr._prompt_sidecar
    sr._prompt_sidecar = lambda pd: Path(d) / "self_reload_prompt.txt"
    try:
        sr.save_pending_prompt("/x", "half typed prompt")
        if sr.take_pending_prompt("/x") != "half typed prompt":
            _fail("stashed prompt not restored")
        if sr._prompt_sidecar("/x").exists():
            _fail("prompt sidecar must be read-and-delete")
        # a second take yields nothing (landed exactly once)
        if sr.take_pending_prompt("/x") != "":
            _fail("prompt resurfaced on a second take")
        # blank text writes nothing
        sr.save_pending_prompt("/x", "   ")
        if sr._prompt_sidecar("/x").exists():
            _fail("blank prompt should not be stashed")
    finally:
        sr._prompt_sidecar = orig


# --------------------------------------------------------------- boot recovery
def _fake_supervisor_env(monkeypatch_root):
    """Point self_heal_ at a throwaway state/crash path and fake the three
    side-effecting boundaries (start_, _reinstall, os.execv). Returns a small
    record of what was called."""
    sh._STATE_PATH = os.path.join(monkeypatch_root, "state.json")
    sh._CRASH_LOG = os.path.join(monkeypatch_root, "crash.log")
    rec = {"reinstalls": 0, "execs": 0}

    sh._reinstall = lambda: (rec.__setitem__("reinstalls", rec["reinstalls"] + 1), True)[1]

    def fake_execv(path, argv):
        rec["execs"] += 1
        raise SystemExit(0)  # emulate "does not return"
    sh.os.execv = fake_execv

    mod = types.ModuleType("micro_cc.start_live_tui_")
    sys.modules["micro_cc.start_live_tui_"] = mod
    return rec, mod


def test_boot_recovery_bounded():
    d = tempfile.mkdtemp()
    rec, mod = _fake_supervisor_env(d)
    pd = "/tmp/sr_test_project"

    # A tree that never boots: one reinstall+relaunch per attempt, capped.
    def always_fail():
        raise RuntimeError("broken tree")
    mod.start_ = always_fail
    sys.argv = ["microcc", pd]

    for _ in range(sh.MAX_BOOT_ATTEMPTS):
        with contextlib.redirect_stderr(io.StringIO()):  # expected tracebacks
            try:
                sh.main()
            except SystemExit:
                pass
    if sh._entry(pd)["attempts"] != sh.MAX_BOOT_ATTEMPTS:
        _fail(f"attempt counter wrong: {sh._entry(pd)}")
    if rec["reinstalls"] != sh.MAX_BOOT_ATTEMPTS:
        _fail(f"expected {sh.MAX_BOOT_ATTEMPTS} reinstalls, got {rec['reinstalls']}")

    # Budget exhausted: NO further reinstall (no infinite loop), even though
    # it still tries to start once.
    before = rec["reinstalls"]
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            sh.main()
        except SystemExit:
            pass
    if rec["reinstalls"] != before:
        _fail("reinstalled after the boot budget was exhausted")

    # A boot that succeeds clears the counter (so a later, unrelated crash is
    # not treated as a failure-to-boot).
    pd2 = "/tmp/sr_test_project2"
    sh._save_entry(pd2, attempts=2, booted=False)

    def boots():
        sh.note_boot_ok(pd2)
    mod.start_ = boots
    sys.argv = ["microcc", pd2]
    try:
        sh.main()
    except SystemExit as e:
        if e.code != 0:
            _fail(f"clean exit expected, got {e.code}")
    if sh._entry(pd2)["attempts"] != 0 or not sh._entry(pd2)["booted"]:
        _fail(f"successful boot did not clear the counter: {sh._entry(pd2)}")

    # Booted-then-crashed is NOT a boot failure: no reinstall.
    pd3 = "/tmp/sr_test_project3"
    sh._save_entry(pd3, attempts=1, booted=False)

    def boot_then_crash():
        sh.note_boot_ok(pd3)
        raise RuntimeError("crashed after boot")
    mod.start_ = boot_then_crash
    sys.argv = ["microcc", pd3]
    before = rec["reinstalls"]
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            sh.main()
        except SystemExit as e:
            if e.code != 1:
                _fail(f"post-boot crash should exit 1, got {e.code}")
    if rec["reinstalls"] != before:
        _fail("a post-boot crash must not trigger a reinstall")


def main():
    test_manifest_diff()
    test_prompt_stash()
    test_boot_recovery_bounded()
    sys.stderr.write("OK: self-reload / self-heal properties held\n")
    sys.exit(0)


if __name__ == "__main__":
    main()
