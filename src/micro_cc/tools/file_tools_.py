import fnmatch
import os
import re
import stat
import time
from collections import deque
from typing import Optional

from micro_cc.utils.helpers import _IMAGE_EXTENSIONS, sanitize_and_encode_image_

# Self-scoped external-change tracking: mirrors bash_tool.py's
# _background_procs — only paths WE'VE read/written via these tools ever
# land here, so format_external_changes (consumed directly by
# claude_loop_.py's per-loop status block) only ever flags changes to
# files Claude itself is already tracking, never ambient noise from the
# rest of the filesystem (this replaced a recursive watchdog Observer over
# the whole project_dir, which was unusable when project_dir was $HOME).
_touched_files: dict[str, float] = {}


def _touch(file_path: str) -> None:
    """Record/refresh the mtime we last saw for a file we just read or wrote."""
    try:
        _touched_files[file_path] = os.path.getmtime(file_path)
    except OSError:
        _touched_files.pop(file_path, None)


def format_external_changes() -> str | None:
    """Diff tracked files against disk; flag ones changed or deleted since
    we last touched them ourselves. Refreshes state after reporting so each
    change is only surfaced once."""
    lines = []
    for path in list(_touched_files):
        try:
            current = os.path.getmtime(path)
        except OSError:
            lines.append(f"  deleted: {path}")
            del _touched_files[path]
            continue
        if current != _touched_files[path]:
            lines.append(f"  modified externally: {path}")
            _touched_files[path] = current
    if not lines:
        return None
    return "\n".join(lines)


def _resolve_whitespace_lookalike(file_path: str) -> str | None:
    """When file_path doesn't exist, check whether a sibling in the same
    directory matches it once every whitespace run is collapsed to a plain
    ASCII space. Handles the model retyping a path it only ever *saw* in a
    prior tool result — e.g. a macOS screenshot filename has a narrow
    no-break space (U+202F) between the time and AM/PM ("12.55.41 AM.png"),
    which is visually identical to a regular space in rendered text, so the
    model composes the next call with an ordinary space and gets a false
    File-not-found. Only fires as a fallback after the exact path already
    failed, and only resolves when exactly one directory entry matches, so
    it can't silently pick the wrong file among several near-duplicates."""
    dirname, basename = os.path.split(file_path)
    dirname = dirname or "."
    try:
        entries = os.listdir(dirname)
    except OSError:
        return None
    target = re.sub(r"\s+", " ", basename)
    matches = [e for e in entries if re.sub(r"\s+", " ", e) == target]
    if len(matches) == 1:
        return os.path.join(dirname, matches[0])
    return None


def read_(
    file_path: str,
    *,
    project_dir: str,
    offset: int = 0,
    limit: int = 2000
) -> str | dict:
    """Read file contents with optional line range. Also handles image files
    (png/jpg/gif/webp/bmp) — shows the image directly, offset/limit are
    ignored for those.

    Args:
        file_path: Absolute or relative path to file
        offset: Line number to start from (0-indexed)
        limit: Max lines to read (default 2000)
    """
    offset, limit = int(offset), int(limit)

    # Resolve path
    if not os.path.isabs(file_path):
        file_path = os.path.join(project_dir, file_path)

    if not os.path.exists(file_path):
        fallback = _resolve_whitespace_lookalike(file_path)
        if fallback is None:
            return f"[File not found: {file_path}] — use glob_ to find the exact filename"
        file_path = fallback

    if os.path.isdir(file_path):
        return f"[Path is a directory, not a file: {file_path}]"

    if file_path.lower().endswith(_IMAGE_EXTENSIONS):
        # claude_loop_.py's tool-result loop special-cases this exact shape
        # (isinstance(result, dict) and result["type"] == "image") — builds
        # a real image tool_result block from it instead of the generic
        # str(result) path, so the image lands directly in THIS
        # conversation, no separate vision-style tool or isolated sub-call
        # needed. Read is multimodal by extension — images are embedded
        # directly without a dedicated image tool.
        encoded = sanitize_and_encode_image_(file_path)
        if encoded is None:
            return f"[Could not read/encode image: {file_path}]"
        _touch(file_path)
        return {"type": "image", "data": encoded}

    # open() on a FIFO/socket/device blocks or never hits EOF — say so instead
    # of hanging the turn; bash_ is the right tool if that's really intended.
    try:
        mode = os.stat(file_path).st_mode
    except OSError as e:
        return f"[Read error: {type(e).__name__}: {e}]"
    if not stat.S_ISREG(mode):
        return (
            f"[Not a regular file (FIFO, socket or device): {file_path} — read_ won't open it "
            f"because it can block forever. If you really mean to read it, use bash_ with a "
            f"bound, e.g. `timeout 10 head -c 65536 {file_path}`]"
        )

    try:
        # Stream instead of readlines(): only the requested window is kept in
        # memory; the rest is just counted for the "more lines" footer.
        output_lines = []
        total_lines = 0
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for idx, line in enumerate(f):
                total_lines = idx + 1
                if offset <= idx < offset + limit:
                    if len(line) > 2000:
                        line = line[:2000] + "... [truncated]"
                    output_lines.append(f"{idx + 1:6d}\t{line.rstrip()}")

        output = "\n".join(output_lines)

        if offset + limit < total_lines:
            output += f"\n\n[... {total_lines - offset - limit} more lines]"

        _touch(file_path)
        return output

    except Exception as e:
        return f"[Read error: {type(e).__name__}: {e}]"


def write_(
    file_path: str,
    content: str,
    *,
    project_dir: str
) -> str:
    """Write content to file (creates or overwrites).

    Args:
        file_path: Absolute or relative path
        content: Content to write
    """
    if not os.path.isabs(file_path):
        file_path = os.path.join(project_dir, file_path)

    try:
        # Create parent directories if needed
        parent = os.path.dirname(file_path)
        if parent and not os.path.exists(parent):
            os.makedirs(parent)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)

        lines = content.count("\n") + 1
        _touch(file_path)
        return f"Wrote {lines} lines to {file_path}"

    except Exception as e:
        return f"[Write error: {type(e).__name__}: {e}]"


def edit_(
    file_path: str,
    old_string: str,
    new_string: str,
    *,
    project_dir: str,
    replace_all: bool = False
) -> str:
    """Surgical string replacement in file.

    Finds exact match of old_string and replaces with new_string.
    Fails if old_string not found or not unique (unless replace_all=True).

    Args:
        file_path: Absolute or relative path
        old_string: Exact text to find and replace
        new_string: Replacement text
        replace_all: If True, replace all occurrences (default False)
    """
    if not os.path.isabs(file_path):
        file_path = os.path.join(project_dir, file_path)

    if not os.path.exists(file_path):
        return f"[File not found: {file_path}] — use glob_ to find the exact filename"

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        count = content.count(old_string)

        if count == 0:
            return f"[String not found in {file_path}]"

        if count > 1 and not replace_all:
            return f"[String found {count} times - use replace_all=True or provide more context]"

        # Find line number(s) before replacement
        if not replace_all:
            pos = content.find(old_string)
            start_line = content[:pos].count('\n') + 1
            end_line = start_line + old_string.count('\n')

        # Perform replacement
        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        _touch(file_path)

        # Build return message
        if replace_all:
            return f"Replaced {count} occurrence(s) in {file_path}"
        else:
            line_info = f"line {start_line}" if start_line == end_line else f"lines {start_line}-{end_line}"
            return f"Replaced at {line_info} in {file_path}"

    except Exception as e:
        return f"[Edit error: {type(e).__name__}: {e}]"


# --- bounded, interruptible glob walk (shared by glob_ and grep_) -----------
# glob.glob can't be interrupted: it only returns (or, for iglob, yields) on a
# match, so a deadline check never runs while it walks a huge tree finding
# nothing. This is the same **-aware algorithm over os.scandir, checking the
# deadline once per directory. Semantics match glob.glob(recursive=True):
# `*`/`**` skip dot-entries unless the segment itself starts with ".".
# Symlinked dirs are followed, but each real dir is entered once per `**`
# expansion, so a symlink cycle can't loop.
_GLOB_MAGIC = re.compile(r"[*?[]")
GLOB_DEADLINE_S = 30
GLOB_SCAN_CAP = 2000   # matches collected before stopping early
GLOB_SHOW = 100        # newest-first results shown
GREP_DEADLINE_S = 45
GREP_MATCH_CAP = 500
GREP_OUTPUT_CAP = 50_000
_GREP_MAX_LINE = 1 << 20  # read very long (e.g. binary) lines in 1MB chunks


class _Deadline(Exception):
    pass


class _WalkStats:
    def __init__(self, deadline: float):
        self.deadline = deadline
        self.unreadable_dirs = 0


def _scandir(path: str, stats: _WalkStats) -> list:
    if time.monotonic() > stats.deadline:
        raise _Deadline
    try:
        with os.scandir(path) as it:
            return list(it)
    except OSError:
        stats.unreadable_dirs += 1
        return []


def _is_dir(entry) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def _match_segs(path: str, segs: list, stats: _WalkStats, visited: frozenset, in_star: bool = False):
    seg, tail = segs[0], segs[1:]
    if seg == "**":
        if tail:
            yield from _match_segs(path, tail, stats, visited)
        elif not in_star:
            yield os.path.join(path, "")  # `**` matches zero dirs: the start dir itself
        for entry in _scandir(path, stats):
            if entry.name.startswith(".") or not _is_dir(entry):
                if not tail and not entry.name.startswith("."):
                    yield entry.path
                continue
            if not tail:
                yield entry.path
            try:
                st = os.stat(entry.path)
                key = (st.st_dev, st.st_ino)
            except OSError:
                continue
            if key in visited:
                continue
            yield from _match_segs(entry.path, segs, stats, visited | {key}, in_star=True)
        return
    if not _GLOB_MAGIC.search(seg):
        nxt = os.path.join(path, seg)
        if tail:
            if os.path.isdir(nxt):
                yield from _match_segs(nxt, tail, stats, visited)
        elif os.path.lexists(nxt):
            yield nxt
        return
    for entry in _scandir(path, stats):
        if entry.name.startswith(".") and not seg.startswith("."):
            continue
        if not fnmatch.fnmatch(entry.name, seg):
            continue
        if tail:
            if _is_dir(entry):
                yield from _match_segs(entry.path, tail, stats, visited)
        else:
            yield entry.path


def _iter_glob(full_pattern: str, stats: _WalkStats):
    """Yield paths matching full_pattern; raises _Deadline past stats.deadline."""
    parts = full_pattern.split(os.sep)
    i = 0
    while i < len(parts) and not _GLOB_MAGIC.search(parts[i]):
        i += 1
    root = os.sep.join(parts[:i]) or os.sep
    segs = [p for p in parts[i:] if p]
    if not segs:
        if os.path.lexists(root):
            yield root
        return
    if not os.path.isdir(root):
        return
    if not full_pattern.endswith(os.sep):
        yield from _match_segs(root, segs, stats, frozenset())
        return
    for m in _match_segs(root, segs, stats, frozenset()):  # trailing "/": dirs only
        if os.path.isdir(m):
            yield os.path.join(m, "")


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def glob_(
    pattern: str,
    *,
    project_dir: str,
    path: str = None
) -> str:
    """Find files matching glob pattern, newest first. Walks at most ~30s /
    2000 matches — if it stops early it says so, and tells you how to get
    the complete list with bash_.

    Args:
        pattern: Glob pattern (e.g., "**/*.py", "src/*.ts")
        path: Directory to search in (default: project_dir)
    """
    base_path = path if path and os.path.isabs(path) else project_dir
    if path and not os.path.isabs(path):
        base_path = os.path.join(project_dir, path)

    if not os.path.isdir(base_path):
        return f"[Directory not found: {base_path}]"

    stats = _WalkStats(time.monotonic() + GLOB_DEADLINE_S)
    matches = []
    stopped = None
    try:
        for m in _iter_glob(os.path.join(base_path, pattern), stats):
            matches.append(m)
            if len(matches) >= GLOB_SCAN_CAP:
                stopped = f"after collecting {GLOB_SCAN_CAP} matches"
                break
    except _Deadline:
        stopped = f"after {GLOB_DEADLINE_S}s of walking"
    except Exception as e:
        return f"[Glob error: {type(e).__name__}: {e}]"

    notes = []
    if stopped:
        notes.append(
            f"[stopped early {stopped} — the tree was NOT fully walked, so this is not the "
            f"complete set and 'newest first' only ranks the {len(matches)} matches found so far. "
            f"For a complete listing use bash_, e.g. `find {base_path} -name '<name-pattern>' "
            f"| head -500` (optionally `-newermt` / `-prune` to narrow).]"
        )
    if stats.unreadable_dirs:
        notes.append(f"[{stats.unreadable_dirs} director(ies) could not be read (permissions/errors) and were not searched]")

    if not matches:
        return "\n\n".join([f"No files matching '{pattern}' in {base_path}"] + notes)

    matches.sort(key=_mtime, reverse=True)
    output = "\n".join(matches[:GLOB_SHOW])
    if len(matches) > GLOB_SHOW:
        notes.insert(0, f"[showing the {GLOB_SHOW} newest of {len(matches)} matches found — narrow the pattern or use bash_ find for the rest]")
    return "\n\n".join([output] + notes)


def grep_(
    pattern: str,
    *,
    project_dir: str,
    path: str = None,
    file_pattern: str = None,
    ignore_case: bool = False,
    context_lines: int = 0
) -> str:
    """Search file contents with regex pattern. Searches every file (binary
    and large ones included) but stops at ~45s / 500 matches / 50k chars of
    output — when it stops early it says exactly where and tells you how to
    run the complete search with bash_ (rg/grep).

    Args:
        pattern: Regex pattern to search for
        path: File or directory to search (default: project_dir)
        file_pattern: Glob pattern to filter files (e.g., "*.py")
        ignore_case: Case-insensitive search
        context_lines: Lines of context before/after match
    """
    base_path = path if path and os.path.isabs(path) else project_dir
    if path and not os.path.isabs(path):
        base_path = os.path.join(project_dir, path)

    if not os.path.exists(base_path):
        return f"[Path not found: {base_path}]"

    context_lines = int(context_lines)

    try:
        flags = re.IGNORECASE if ignore_case else 0
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"[Invalid regex: {e}]"

    stats = _WalkStats(time.monotonic() + GREP_DEADLINE_S)
    results = []
    files_searched = 0
    unreadable_files = 0
    special_files = 0
    matches_found = 0
    stopped = None
    current_file = None

    def _fmt(prefix: str, num: int, line: str) -> str:
        line = line.rstrip("\n")
        if len(line) > 2000:
            line = line[:2000] + "... [truncated]"
        return f"{prefix}{num:6d}: {line}"

    def search_file(filepath):
        """Stream the file so a huge/binary file never sits in memory whole;
        before-context comes from a small deque, after-context from groups
        still waiting on their trailing lines."""
        nonlocal matches_found, unreadable_files
        groups = []      # finished context groups for this file
        pending = []     # [lines, after_remaining]
        before = deque(maxlen=context_lines)
        lineno = 1
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                for chunk in iter(lambda: f.readline(_GREP_MAX_LINE), ""):
                    if time.monotonic() > stats.deadline:
                        raise _Deadline
                    for g in pending:
                        g[0].append(_fmt(" ", lineno, chunk))
                        g[1] -= 1
                    groups.extend("\n".join(g[0]) for g in pending if g[1] <= 0)
                    pending = [g for g in pending if g[1] > 0]
                    if matches_found < GREP_MATCH_CAP and regex.search(chunk):
                        matches_found += 1
                        lines = [_fmt(" ", n, l) for n, l in before] + [_fmt(">", lineno, chunk)]
                        if context_lines:
                            pending.append([lines, context_lines])
                        else:
                            groups.append("\n".join(lines))
                    if context_lines:
                        before.append((lineno, chunk))
                    if chunk.endswith("\n"):
                        lineno += 1
                    if matches_found >= GREP_MATCH_CAP and not pending:
                        break
        except _Deadline:
            groups.extend("\n".join(g[0]) for g in pending)
            if groups:
                results.append(f"\n{filepath}:\n" + "\n---\n".join(groups))
            raise
        except OSError:
            unreadable_files += 1
            return
        groups.extend("\n".join(g[0]) for g in pending)
        if groups:
            results.append(f"\n{filepath}:\n" + "\n---\n".join(groups))

    try:
        if os.path.isfile(base_path):
            files_searched = 1
            current_file = base_path
            search_file(base_path)
            if matches_found >= GREP_MATCH_CAP:
                stopped = f"at the {GREP_MATCH_CAP}-match cap"
        else:
            glob_pat = file_pattern or "**/*"
            for filepath in _iter_glob(os.path.join(base_path, glob_pat), stats):
                if matches_found >= GREP_MATCH_CAP:
                    stopped = f"at the {GREP_MATCH_CAP}-match cap"
                    break
                try:
                    mode = os.stat(filepath).st_mode
                except OSError:
                    unreadable_files += 1
                    continue
                if stat.S_ISDIR(mode):
                    continue
                if not stat.S_ISREG(mode):
                    special_files += 1  # FIFO/socket/device: open() could block forever
                    continue
                files_searched += 1
                current_file = filepath
                search_file(filepath)
    except _Deadline:
        stopped = f"after {GREP_DEADLINE_S}s" + (f", while in {current_file}" if current_file else "")

    notes = []
    if stopped:
        notes.append(
            f"[partial results: search stopped {stopped} — {files_searched} file(s) searched, "
            f"{matches_found} match(es) so far; the rest of {base_path} was NOT searched. For a "
            f"complete search use bash_, e.g. `rg -n '{pattern}' {base_path}` (or `grep -rn`), "
            f"narrowed with a path/--glob if it's big.]"
        )
    if unreadable_files or stats.unreadable_dirs:
        notes.append(
            f"[{unreadable_files} file(s) and {stats.unreadable_dirs} director(ies) could not be "
            f"read (permissions/errors) and were not searched]"
        )

    if special_files:
        notes.append(
            f"[{special_files} FIFO/socket/device file(s) were not opened (reading them can block "
            f"forever) — inspect with bash_ if they matter]"
        )

    if not results:
        return "\n\n".join([f"No matches for '{pattern}' in {base_path}"] + notes)

    output = "\n".join(results)
    if len(output) > GREP_OUTPUT_CAP:
        full_len = len(output)
        output = output[:GREP_OUTPUT_CAP]
        notes.insert(0, f"[output truncated at {GREP_OUTPUT_CAP} of {full_len} chars — narrow path/file_pattern, or use bash_ rg for the full list]")

    return "\n\n".join([output] + notes)
