"""Project file listing shared by the GUI's @-mention picker (webui/server.py)
and the TUI's mirror of it (start_live_.py)."""

import os
from pathlib import Path

# Directories that would swamp the @ dropdown with build output rather than
# the user's own files.
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "env", "venv", "dist",
    "build", ".next", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea",
    ".browser_screenshots", ".computer_screenshots",
}
MAX_FILES = 3000


def list_project_files(project_dir: str) -> list[str]:
    """Relative paths of every file under project_dir, skipping SKIP_DIRS and
    dotfiles, capped at MAX_FILES."""
    root = Path(project_dir)
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            found.append(str(Path(dirpath, name).relative_to(root)))
            if len(found) >= MAX_FILES:
                return sorted(found)
    return sorted(found)


def rank_file_matches(files: list[str], query: str, limit: int = 50) -> list[str]:
    """Order @-picker candidates so an obvious match (typing "config" for
    src/config.py) doesn't get buried under every unrelated path that
    happens to contain the substring somewhere in a directory name. Three
    tiers — basename starts with query, basename contains query, full path
    contains query — each shallow-path-first."""
    query_lc = query.lower()
    depth_then_name = lambda f: (f.count("/"), f)  # noqa: E731

    if not query_lc:
        return sorted(files, key=depth_then_name)[:limit]

    tiers: tuple[list[str], list[str], list[str]] = ([], [], [])
    for f in files:
        base = f.rsplit("/", 1)[-1].lower()
        if base.startswith(query_lc):
            tiers[0].append(f)
        elif query_lc in base:
            tiers[1].append(f)
        elif query_lc in f.lower():
            tiers[2].append(f)

    ranked = []
    for tier in tiers:
        ranked.extend(sorted(tier, key=depth_then_name))
    return ranked[:limit]
