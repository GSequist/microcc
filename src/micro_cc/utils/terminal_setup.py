"""Platform-specific terminal keybinding setup for Shift+Enter and Alt+Left/Right."""
import json
import os
import platform
import subprocess
from pathlib import Path

_STATE_PATH = Path.home() / ".micro-cc" / "terminal_setup.json"

# Terminals that speak the Kitty keyboard protocol natively — nothing to do.
_NATIVE_TERMINALS = {"iTerm.app", "ghostty", "kitty", "WezTerm", "WarpTerminal"}


def detect_terminal() -> str:
    """Best-effort terminal identifier from environment variables."""
    if os.environ.get("WT_SESSION"):
        return "windows_terminal"
    term_program = os.environ.get("TERM_PROGRAM", "")
    if term_program:
        return term_program
    if os.environ.get("KITTY_WINDOW_ID"):
        return "kitty"
    if os.environ.get("TERM") == "xterm-ghostty":
        return "ghostty"
    if os.environ.get("KONSOLE_VERSION"):
        return "konsole"
    if os.environ.get("ALACRITTY_WINDOW_ID") or os.environ.get("TERM") == "alacritty":
        return "alacritty"
    if os.environ.get("VTE_VERSION"):
        return "vte"  # GNOME Terminal, xfce4-terminal, Tilix, etc. — no send-string keybinding API
    if platform.system() == "Windows":
        return "powershell"
    if platform.system() == "Linux" and os.environ.get("DISPLAY") and os.environ.get("TERM", "").startswith("xterm"):
        return "xterm"
    return "unknown"


def _load_state() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(state))
    except Exception:
        pass  # best-effort — a failed write just means we ask again next launch


def _append_snippet_if_absent(path: Path, marker: str, snippet: str) -> bool | None:
    """Append snippet if marker absent (idempotent); True on write, None if present, False on failure."""
    try:
        existing = path.read_text() if path.exists() else ""
    except Exception:
        return False
    if marker in existing:
        return None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(snippet)
        return True
    except Exception:
        return False


# --- macOS: Terminal.app -----------------------------------------------

def _plistbuddy(cmd: str, plist: str) -> bool:
    return subprocess.run(
        ["/usr/libexec/PlistBuddy", "-c", cmd, plist],
        capture_output=True,
    ).returncode == 0


def setup_apple_terminal() -> str:
    plist = str(Path.home() / "Library/Preferences/com.apple.Terminal.plist")
    backup = plist + ".micro-cc.bak"
    try:
        profile = subprocess.run(
            ["defaults", "read", "com.apple.Terminal", "Default Window Settings"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        return "Could not read Terminal.app's default profile — set 'Use Option as Meta key' manually in Preferences > Profiles > Keyboard."

    # Back up once so re-runs don't overwrite prior backup; reverts with `defaults import`.
    if not os.path.exists(backup):
        subprocess.run(["defaults", "export", "com.apple.Terminal", backup], capture_output=True)

    ok = _plistbuddy(f"Add :'Window Settings':'{profile}':useOptionAsMetaKey bool true", plist)
    if not ok:
        ok = _plistbuddy(f"Set :'Window Settings':'{profile}':useOptionAsMetaKey true", plist)
    if not ok:
        return "Could not enable 'Use Option as Meta key' in Terminal.app — set it manually in Preferences > Profiles > Keyboard."

    subprocess.run(["killall", "cfprefsd"], capture_output=True)
    return (
        "Configured Terminal.app: enabled 'Use Option as Meta key' so "
        "Option+Left/Right jump words (backed up prior settings to "
        f"{backup} — revert with `defaults import com.apple.Terminal {backup}`). "
        "Terminal.app doesn't send Option+Enter as a newline — type a "
        "literal backslash then Enter, or use Ctrl+J. Restart Terminal.app "
        "for the meta-key change to take effect."
    )


# --- Windows Terminal -----------------------------------------------------

def _windows_terminal_settings_path():
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if not local_appdata:
        return None
    packaged = Path(local_appdata) / "Packages"
    if packaged.is_dir():
        for entry in sorted(packaged.glob("Microsoft.WindowsTerminal_*")):
            candidate = entry / "LocalState" / "settings.json"
            if candidate.exists():
                return candidate
    unpackaged = Path(local_appdata) / "Microsoft" / "Windows Terminal" / "settings.json"
    return unpackaged if unpackaged.exists() else None


def setup_windows_terminal() -> str:
    settings_path = _windows_terminal_settings_path()
    if settings_path is None:
        return "Could not find Windows Terminal's settings.json — add a Shift+Enter keybinding manually (Settings > Actions > New) sending input '\\u001b\\r'."

    try:
        raw = settings_path.read_text(encoding="utf-8")
        settings = json.loads(raw)
    except Exception:
        return f"Could not parse {settings_path} — skipping terminal setup."

    keybindings = settings.setdefault("keybindings", [])
    if any(kb.get("keys") == "shift+enter" for kb in keybindings):
        return "Windows Terminal already has a Shift+Enter keybinding — leaving it as-is."

    keybindings.append({
        "command": {"action": "sendInput", "input": "\r"},
        "keys": "shift+enter",
    })

    try:
        settings_path.with_suffix(settings_path.suffix + ".bak").write_text(raw, encoding="utf-8")
        settings_path.write_text(json.dumps(settings, indent=4), encoding="utf-8")
    except Exception:
        return f"Could not write {settings_path} — add a Shift+Enter keybinding manually (Settings > Actions > New) sending input '\\u001b\\r'."

    return f"Installed a Shift+Enter keybinding in Windows Terminal ({settings_path}). Restart it to take effect."


# --- tmux (orthogonal to the outer terminal — needs its own passthrough) --

def setup_tmux() -> str:
    conf = Path.home() / ".tmux.conf"
    marker = "# micro-cc: Shift+Enter -> newline"
    snippet = f"{marker}\nbind-key -n S-Enter send-keys Escape Enter\n"
    result = _append_snippet_if_absent(conf, marker, snippet)
    if result is None:
        return None
    if not result:
        return (
            f"Could not update {conf} — inside tmux, add manually: "
            "`bind-key -n S-Enter send-keys Escape Enter`, then "
            "`tmux source ~/.tmux.conf`."
        )
    return (
        f"Added a Shift+Enter binding to {conf} for tmux. "
        "Reload with `tmux source ~/.tmux.conf` (or restart tmux) for it to take effect."
    )


# --- VS Code integrated terminal -------------------------------------------

def _vscode_keybindings_path():
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library/Application Support/Code/User/keybindings.json"
    if system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "Code" / "User" / "keybindings.json" if appdata else None
    return Path.home() / ".config/Code/User/keybindings.json"


def setup_vscode_terminal() -> str:
    manual = (
        'Add a keybinding in VS Code (Preferences: Open Keyboard Shortcuts (JSON)): '
        '`{"key": "shift+enter", "command": "workbench.action.terminal.sendSequence", '
        '"args": {"text": "\\u001b\\r"}, "when": "terminalFocus"}`.'
    )
    path = _vscode_keybindings_path()
    if path is None or not path.parent.parent.exists():
        return manual

    try:
        raw = path.read_text(encoding="utf-8") if path.exists() else "[]"
        keybindings = json.loads(raw) if raw.strip() else []
    except Exception:
        return f"Could not parse {path} (it may contain comments) — {manual}"

    already = any(
        kb.get("key") == "shift+enter"
        and kb.get("command") == "workbench.action.terminal.sendSequence"
        for kb in keybindings
    )
    if already:
        return None

    keybindings.append({
        "key": "shift+enter",
        "command": "workbench.action.terminal.sendSequence",
        "args": {"text": "\x1b\r"},
        "when": "terminalFocus",
    })

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.with_suffix(path.suffix + ".bak").write_text(raw, encoding="utf-8")
        path.write_text(json.dumps(keybindings, indent=4), encoding="utf-8")
    except Exception:
        return f"Could not write {path} — {manual}"

    return f"Added a Shift+Enter keybinding to VS Code's integrated terminal ({path}). Reload the VS Code window for it to take effect."


# --- xterm ------------------------------------------------------------

def setup_xterm() -> str:
    xres = Path.home() / ".Xresources"
    marker = "! micro-cc: Shift+Enter -> newline"
    snippet = f'{marker}\nXTerm*VT100.translations: #override Shift <Key>Return: string("\\033\\r")\n'
    result = _append_snippet_if_absent(xres, marker, snippet)
    if result is None:
        return None
    if not result:
        return (
            f"Could not update {xres} — add manually: "
            '`XTerm*VT100.translations: #override Shift <Key>Return: string("\\033\\r")`, '
            "then `xrdb -merge ~/.Xresources` and restart xterm."
        )
    merged = subprocess.run(["xrdb", "-merge", str(xres)], capture_output=True).returncode == 0
    if merged:
        return f"Configured xterm for Shift+Enter via {xres} and merged with xrdb. Restart xterm for it to fully take effect."
    return f"Configured xterm for Shift+Enter via {xres} — run `xrdb -merge ~/.Xresources` and restart xterm for it to take effect."


# --- Alacritty ----------------------------------------------------------

def setup_alacritty() -> str:
    cfg_dir = Path.home() / ".config" / "alacritty"
    toml_cfg = cfg_dir / "alacritty.toml"
    yml_cfg = cfg_dir / "alacritty.yml"
    if yml_cfg.exists() and not toml_cfg.exists():
        return (
            f"{yml_cfg} uses Alacritty's legacy YAML format — add under `key_bindings:`: "
            '`- {key: Return, mods: Shift, chars: "\\x1b\\r"}`, then restart Alacritty.'
        )

    marker = "# micro-cc: Shift+Enter -> newline"
    snippet = f'{marker}\n[[keyboard.bindings]]\nkey = "Enter"\nmods = "Shift"\nchars = "\\u001b\\r"\n'
    result = _append_snippet_if_absent(toml_cfg, marker, snippet)
    if result is None:
        return None
    if not result:
        return (
            f"Could not update {toml_cfg} — add manually: `[[keyboard.bindings]]` / "
            '`key = "Enter"` / `mods = "Shift"` / `chars = "\\u001b\\r"`.'
        )
    return f"Added a Shift+Enter binding to {toml_cfg}. Restart Alacritty for it to take effect."


# --- newline hint (for the static hint bar in start_live_.py) -----------

def newline_hint_text() -> str | None:
    """Hint text for newline shortcuts, or None if user already knows them."""
    if _load_state().get("used_multiline"):
        return None
    if detect_terminal() in _NATIVE_TERMINALS:
        return "shift+⏎ for a newline"
    return "type \\ then ⏎ for a newline (or ctrl+j)"


def mark_multiline_used() -> None:
    state = _load_state()
    if not state.get("used_multiline"):
        state["used_multiline"] = True
        _save_state(state)


# --- orchestrator -----------------------------------------------------

def ensure_terminal_setup() -> str | None:
    """Run keybinding setup once per terminal; returns status or None if already done."""
    terminal_id = detect_terminal()
    in_tmux = bool(os.environ.get("TMUX"))
    state = _load_state()
    if state.get("terminal_id") == terminal_id and state.get("tmux") == in_tmux and state.get("done"):
        return None

    messages = []

    if in_tmux:
        tmux_message = setup_tmux()
        if tmux_message:
            messages.append(tmux_message)

    if terminal_id in _NATIVE_TERMINALS:
        pass  # Kitty keyboard protocol negotiated automatically — nothing more to do
    elif terminal_id == "vscode":
        vscode_message = setup_vscode_terminal()
        if vscode_message:
            messages.append(vscode_message)
    elif platform.system() == "Darwin" and terminal_id == "Apple_Terminal":
        messages.append(setup_apple_terminal())
    elif terminal_id == "windows_terminal":
        messages.append(setup_windows_terminal())
    elif terminal_id == "xterm":
        messages.append(setup_xterm())
    elif terminal_id == "alacritty":
        messages.append(setup_alacritty())
    elif terminal_id in ("konsole", "vte"):
        # Konsole keytabs and VTE have no send-string hook.
        name = "Konsole" if terminal_id == "konsole" else "your terminal (GNOME Terminal/VTE-based)"
        messages.append(
            f"Shift+Enter can't be auto-configured in {name} — "
            "use Ctrl+J or Alt+Enter for a newline instead."
        )
    else:
        # No reliable keybinding hook; fallback shortcuts work everywhere.
        messages.append(
            f"Shift+Enter may not work in {terminal_id} — "
            "use Ctrl+J or Alt+Enter for a newline instead."
        )

    _save_state({"terminal_id": terminal_id, "tmux": in_tmux, "done": True})
    return " ".join(messages) if messages else None
