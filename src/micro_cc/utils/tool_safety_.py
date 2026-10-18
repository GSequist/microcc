"""Read-only tool classification, plus plain-language explain/risk for the approval gate."""

import os
import re

READ_ONLY_TOOLS = {
    "read_", "glob_", "grep_",
    "read_skill", "list_skills", "list_mcps",
}


def read_only_by_input(name: str, tool_input: dict) -> bool:
    """Check if tool is read-only based on specific action parameter."""
    if name == "memory_":
        return tool_input.get("action") in ("get", "list")
    if name == "search_tools":
        return tool_input.get("action") == "discover"
    return False


def is_read_only(name: str, tool_input: dict) -> bool:
    return name in READ_ONLY_TOOLS or read_only_by_input(name, tool_input)


def _file(a: dict) -> str:
    return os.path.basename(a.get("file_path") or "") or "a file"


# Fallback lines for gated tools when the model gave no `description`.
_EXPLAIN = {
    "bash_": lambda a: "Run a command on your computer",
    "write_": lambda a: f"Create or overwrite {_file(a)}",
    "edit_": lambda a: f"Change text in {_file(a)}",
    "read_": lambda a: f"Read {_file(a)}",
    "glob_": lambda a: "Look for files by name",
    "grep_": lambda a: "Search inside files",
    "browser": lambda a: "Control a web browser",
    "computer": lambda a: "Control your mouse and keyboard",
}

# Always ask (Auto mode never waves these through): (plain reason shown in the badge, pattern).
_BASH_HIGH = [(reason, re.compile(rx)) for reason, rx in [
    ("deletes files", r"\b(rm|rmdir|shred|srm|unlink)\s|\bfind\b[^;&|]*\s-(delete|exec\s+rm)\b"),
    ("needs admin rights", r"\b(sudo|su|doas)\b"),
    ("sends data off your computer",
     r"\bcurl\b[^;&|]*\s(-d|--data[\w-]*|-F|--form|-T|--upload-file|-X\s*(POST|PUT|PATCH|DELETE))\b"
     r"|\b(scp|sftp|ssh|ftp|nc|ncat|telnet|mail|mailx|sendmail)\b|\brsync\b[^;&|]*\s\S+:"),
    ("runs code from the internet", r"\b(curl|wget)\b[^;&]*\|\s*(sudo\s+)?\w*sh\b|\bsh\s+<\(\s*(curl|wget)"),
    ("controls other apps (Mail, Messages, Finder…)", r"\bosascript\b|\bautomator\b|\bshortcuts\s+run\b"),
    ("changes system settings",
     r"\bdefaults\s+(write|delete)\b|\b(systemsetup|networksetup|pmset|scutil|csrutil|spctl|nvram|tmutil|fdesetup)\b"),
    ("touches passwords or keys", r"\bsecurity\s+\w|\.(ssh|aws|gnupg|kube)\b|Keychains|\.env\b"),
    ("sets something to run automatically later",
     r"\b(crontab|launchctl)\b|LaunchAgents|LaunchDaemons|\.(zshrc|bashrc|bash_profile|zprofile|profile)\b"),
    ("installs software on the whole computer",
     r"\bbrew\s+(install|uninstall|reinstall|upgrade|tap)\b|\bnpm\s+(i|install)\s+(-g|--global)\b"
     r"|\bpip3?\s+install\b[^;&|]*(--user|--break-system-packages)|\binstaller\s+-pkg\b|\bsoftwareupdate\b"),
    ("stops running programs", r"\b(kill|killall|pkill)\b"),
    ("changes who can open files", r"\bchmod\s+(-R\b|\S*777)|\bchown\b|\bxattr\s+-[dc]"),
    ("could wipe a disk or shut down", r"\bmkfs|\bdd\s+if=|\bdiskutil\s+(erase|partition|reformat)|\b(shutdown|reboot|halt)\b"),
    ("can't be undone (git)", r"\bgit\s+(push|reset\s+--hard|clean|branch\s+-D)\b"),
]]
# Commands that only look; a segment starting with anything else isn't low risk.
_BASH_READ = {"ls", "cd", "cat", "head", "tail", "wc", "grep", "rg", "find", "pwd", "echo", "printf",
              "file", "stat", "du", "df", "which", "date", "cal", "tree", "sort", "uniq", "cut", "diff",
              "basename", "dirname", "realpath", "mdfind", "mdls", "pdfinfo", "whoami", "uname", "sw_vers"}
# Writes hiding inside "read" commands: redirects, substitutions, find actions, sort -o.
_WRITE_HINTS = re.compile(r"[`>]|\$\(|\s-(exec|execdir|delete|ok|fprint)\b|\bsort\b[^;&|]*\s-o\b")
_HARMLESS_REDIRECT = re.compile(r"\d?>\s*/dev/null|2>&1")


def explain_gated(name: str, tool_input) -> str:
    """One plain sentence for the approval prompt; the model's description wins."""
    a = tool_input if isinstance(tool_input, dict) else {}
    desc = a.get("description")
    if isinstance(desc, str) and desc.strip():
        return desc.strip()
    fn = _EXPLAIN.get(name)
    return fn(a) if fn else f"Use the {name} tool"


def _inside(path: str, project_dir: str) -> bool:
    path = os.path.expanduser(path)
    full = os.path.realpath(path if os.path.isabs(path) else os.path.join(project_dir, path))
    root = os.path.realpath(project_dir)
    return full == root or full.startswith(root + os.sep)


def _bash_risk(cmd: str) -> tuple[str, str]:
    for reason, rx in _BASH_HIGH:
        if rx.search(cmd):
            return "high", reason
    probe = _HARMLESS_REDIRECT.sub("", cmd)
    segs = [seg.split() for seg in re.split(r"&&|\|\||[;|]", probe) if seg.strip()]
    if segs and not _WRITE_HINTS.search(probe) and all(seg[0] in _BASH_READ for seg in segs):
        return "low", "only looks at files"
    return "medium", ""


def gate_risk(name: str, tool_input, project_dir: str) -> tuple[str, str]:
    """(risk, plain reason) for the badge and Auto mode; risk is 'low' | 'medium' | 'high'."""
    a = tool_input if isinstance(tool_input, dict) else {}
    if is_read_only(name, a):
        return "low", "only looks at files"
    if name in ("write_", "edit_"):
        if _inside(a.get("file_path") or "", project_dir):
            return "medium", "changes a file in your project folder"
        return "high", "changes a file outside your project folder"
    if name == "bash_":
        return _bash_risk(a.get("command") or "")
    if name == "computer":
        return "high", "controls your mouse and keyboard"
    return "medium", ""
