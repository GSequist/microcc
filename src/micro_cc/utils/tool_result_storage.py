"""Persist oversized tool results to /tmp (auto-cleaned on reboot)."""
import tempfile
from pathlib import Path

from micro_cc.utils.helpers import project_hash
from micro_cc.utils.tool_limits import PREVIEW_CHARS


def _tool_results_dir(project_dir: str) -> Path:
    d = Path(tempfile.gettempdir()) / "micro-cc" / project_hash(project_dir) / "tool-results"
    d.mkdir(parents=True, exist_ok=True)
    return d


def persist_and_preview(content: str, tool_use_id: str, project_dir: str, cap: float) -> str:
    """Persist content to /tmp; return preview and path pointer."""
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
