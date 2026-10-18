"""Live subagent and background-process snapshots for the GUI (endpoints and the data- stream)."""

import os

from micro_cc.utils import subagent_tracker_

_DONE = {"DONE", "FAILED"}
_poll: dict = {}  # target -> (stat_key, last info)


def subagents(project_dir: str) -> list:
    """Tracked headless subagents, same poll the TUI's status line runs."""
    from micro_cc.tools.monitor_ import poll_status

    out = []
    for target, entry in subagent_tracker_.read_tracked(project_dir)["subagents"].items():
        stat, last = _poll.get(target, (None, None))
        info, stat = poll_status(target, stat)
        last = info or last
        _poll[target] = (stat, last)
        # The TUI poller writes terminal states (incl. pid-death FAILED) to the tracker.
        status = entry["status"] if entry["status"] in _DONE else (last["status"] if last else entry["status"])
        if status in _DONE and entry["notified"]:
            continue
        out.append({
            "target": target,
            "name": last["name"] if last else (os.path.basename(target.rstrip("/")) or target),
            "status": status,
            "summary": last["summary"] if last else "",
            "pending": last["pending"] if last else 0,
            "pid": entry.get("pid"),
        })
    return out


def bgprocs(project_dir: str) -> list:
    """Background `cmd &` processes, minus the ones shown as subagents."""
    from micro_cc.tools.bash_tool import list_background_processes

    sub_pids = {e.get("pid") for e in subagent_tracker_.read_tracked(project_dir)["subagents"].values()}
    return [p for p in list_background_processes() if p["pid"] not in sub_pids]


def change_key(subs: list, procs: list) -> tuple:
    """Identity of a snapshot, ignoring ever-changing ages."""
    return (
        tuple((s["target"], s["status"], s["pending"]) for s in subs),
        tuple((p["pid"], p.get("stalled")) for p in procs),
    )
