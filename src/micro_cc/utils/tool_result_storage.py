"""Persist oversized tool results to /tmp instead of truncating them.

/tmp (not ~/.micro-cc or project_dir) is deliberate: results here are
scratch, not durable state. No home-directory clutter, and nothing to clean
up ourselves — the OS clears /tmp on reboot/container restart. If the model
never reads the file back, it's gone on its own; no discipline required.
"""
import tempfile
from pathlib import Path

from micro_cc.utils.helpers import project_hash
from micro_cc.utils.tool_limits import PREVIEW_CHARS


def _tool_results_dir(project_dir: str) -> Path:
    d = Path(tempfile.gettempdir()) / "micro-cc" / project_hash(project_dir) / "tool-results"
    d.mkdir(parents=True, exist_ok=True)
    return d


def persist_and_preview(content: str, tool_use_id: str, project_dir: str, cap: float) -> str:
    """Caller has already confirmed len(content) > cap. Writes the full
    content to /tmp and returns a preview + pointer to use as the
    tool_result content in place of the original."""
    path = _tool_results_dir(project_dir) / f"{tool_use_id}.txt"
    path.write_text(content)
    preview = content[:PREVIEW_CHARS]
    return (
        f"{preview}\n...[truncated — {len(content)} chars total]\n\n"
        "<system-reminder>\n"
        f"Output exceeded the {int(cap) if cap != float('inf') else 'per-tool'} char cap "
        f"and was persisted. Full result: {path}\n"
        "Read it directly (read_, absolute path) if the preview above isn't enough.\n"
        "</system-reminder>"
    )
