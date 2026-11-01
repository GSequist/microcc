"""keep_awake_ really blocks idle sleep on this OS, as the OS reports it, and lets go on release.

Run directly:  python tests/test_keep_awake.py
macOS: pmset assertions · Linux: systemd-inhibit --list · WSL: Windows powercfg /requests (needs admin).
Set MICROCC_KEEP_AWAKE_REQUIRED=1 (CI) to fail instead of skip when the OS check is unavailable.
"""

import os
import platform
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from micro_cc.utils import keep_awake_ as ka  # noqa: E402

REQUIRED = os.environ.get("MICROCC_KEEP_AWAKE_REQUIRED") == "1"


def _os_report() -> tuple[str, str] | None:
    """(report text, marker that proves our inhibitor is in it), or None if this OS can't be checked."""
    if platform.system() == "Darwin":
        out = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True).stdout
        return out, f"pid {ka.status()['pid']}(caffeinate)"
    if platform.system() == "Linux" and ka._is_wsl():
        ps = ka._powershell()
        if not ps:
            return None
        out = subprocess.run([ps, "-NoProfile", "-Command", "powercfg /requests"],
                             capture_output=True, text=True, timeout=60).stdout
        # The SYSTEM section lists our powershell.exe; other sections can't hold it.
        system = out.split("SYSTEM:", 1)[-1].split("AWAYMODE:", 1)[0] if "SYSTEM:" in out else out  # raw = error text
        return system, "powershell.exe"
    if platform.system() == "Linux":
        try:
            out = subprocess.run(["systemd-inhibit", "--list", "--no-pager"], capture_output=True, text=True).stdout
        except FileNotFoundError:
            return None
        return out, "microcc"
    return None


def _wait_for(cond, timeout=10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.3)
    return cond()


class KeepAwake(unittest.TestCase):
    def tearDown(self):
        ka.release_all()

    def test_holders_share_one_inhibitor(self):
        ka.hold("turn")
        st = ka.status()
        if st["method"] is None:
            if REQUIRED:
                self.fail(f"no inhibitor on this OS: {st['error']}")
            self.skipTest(st["error"])
        first = st["pid"]
        ka.hold("background")
        self.assertEqual(ka.status()["pid"], first)  # no second process
        ka.hold("turn", False)
        self.assertTrue(ka.status()["held"])  # background still holds
        ka.hold("background", False)
        self.assertFalse(ka.status()["held"])
        self.assertEqual(ka.status()["pid"], first)  # lingers for the next turn
        ka.hold("turn")
        self.assertEqual(ka.status()["pid"], first)  # reused, no respawn per turn
        ka.release_all()
        self.assertIsNone(ka.status()["pid"])

    def test_release_never_blocks(self):
        """Turn end runs on the TUI event loop: stopping must not wait on the process."""
        ka.hold("turn")
        if ka.status()["method"] is None:
            self.skipTest("no inhibitor on this OS")
        t = time.monotonic()
        ka.release_all()
        self.assertLess(time.monotonic() - t, 0.2)

    def test_broken_inhibitor_is_not_respawned(self):
        """Locked-down Windows (no Add-Type), no logind, ...: one attempt, a reason, no spawn loop."""
        spawned = []
        real_popen = subprocess.Popen

        def counting(*a, **k):
            spawned.append(1)
            return real_popen(*a, **k)
        with patch.object(ka, "_command", lambda: ("fake", ["sh", "-c", "echo blocked by policy >&2; exit 1"])), \
             patch.object(ka.subprocess, "Popen", counting), patch.object(ka, "LINGER_S", 0.2):
            ka.hold("background")
            self.assertTrue(_wait_for(lambda: ka.status()["error"], 5))
            for _ in range(5):
                ka.hold("background")
                ka.hold("turn")
                ka.hold("turn", False)
            self.assertEqual(len(spawned), 1)
            self.assertIn("blocked by policy", ka.status()["error"])
            ka.hold("background", False)  # everyone left: after the linger it may try again
            time.sleep(0.5)
            ka.hold("turn")
            self.assertEqual(len(spawned), 2)

    def test_os_sees_the_inhibitor(self):
        ka.hold("turn")
        st = ka.status()
        if st["method"] is None:
            if REQUIRED:
                self.fail(f"no inhibitor on this OS: {st['error']}")
            self.skipTest(st["error"])
        self.assertTrue(st["held"], st)
        rep = _os_report()
        if rep is None:
            if REQUIRED:
                self.fail("cannot query the OS for sleep inhibitors here")
            self.skipTest("cannot query the OS for sleep inhibitors here")
        self.assertTrue(_wait_for(lambda: (r := _os_report()) and r[1] in r[0]),
                        f"inhibitor not reported by the OS, status {ka.status()}:\n{_os_report()[0]}")
        print(f"\nkeep_awake: {st['method']} visible to the OS")
        marker_report = _os_report()
        ka.release_all()
        if st["method"] != "powershell":  # other Windows processes may hold SYSTEM requests too
            self.assertTrue(_wait_for(lambda: (r := _os_report()) and r[1] not in r[0]),
                            f"inhibitor still reported after release:\n{marker_report[0]}")
        else:
            self.assertTrue(_wait_for(lambda: "powershell.exe" not in _os_report()[0]),
                            f"powershell still holds SYSTEM after release:\n{_os_report()[0]}")

    def test_inhibitor_dies_with_parent(self):
        """A crashed microcc must not leave the machine awake forever."""
        if platform.system() == "Linux" and ka._is_wsl() and not ka._powershell():
            self.skipTest("no powershell.exe")
        code = (f"import sys,time; sys.path.insert(0,{os.path.join(REPO, 'src')!r}); "
                "from micro_cc.utils import keep_awake_ as ka; ka.hold('t'); "
                "print(ka.status()['pid'], flush=True); time.sleep(60)")
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        line = child.stdout.readline().strip()
        if line in ("", "None"):
            child.kill()
            child.wait()
            child.stdout.close()
            if REQUIRED:
                self.fail("no inhibitor started in child")
            self.skipTest("no inhibitor on this OS")
        inhibitor = int(line)
        child.kill()
        child.wait()
        child.stdout.close()

        def gone():
            try:
                os.kill(inhibitor, 0)
            except ProcessLookupError:
                return True
            # a zombie (parent died, reaped by init later) counts as gone
            st = subprocess.run(["ps", "-o", "stat=", "-p", str(inhibitor)], capture_output=True, text=True).stdout
            return st.strip().startswith("Z") or not st.strip()
        self.assertTrue(_wait_for(gone, 40), f"inhibitor {inhibitor} outlived its parent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
