"""One-time release note per version, shown until the first message (TUI and GUI)."""

from importlib.metadata import version

from micro_cc.utils import settings_store_

# Key = the version the next publish ships: run `./publish.sh --next`. Versions without an entry show nothing.
NOTES = {
    "0.2.132": [
        "/keys now replaces a key you enter again (e.g. a fresh GRAPH_API_KEY) instead of appending a stale duplicate to .env",
        "Coming back from /gui on a long conversation no longer leaves a garbled status line: the screen fully redraws on return",
        "GUI server errors and warnings go to ~/.micro-cc/gui.log instead of spilling into the terminal",
        "Pressing esc to leave /gui no longer freezes the terminal while the server shuts down",
    ],
    "0.2.131": [
        "Decks on your own template: drop in a .pptx/.potx and microcc fills its layouts with real titles, text and native charts",
        "The browser tool can drive your own running Chrome (with your logins) once remote debugging is on",
    ],
    "0.2.130": [
        "Say \"point 2 is wrong\" and microcc fixes its last answer in place instead of writing a new one",
        "Thinking now shows when you reopen or rewind a conversation, and is kept for the model on the same provider",
        "LiteLLM: thinking is now sent back to the model correctly, for both Claude and GPT",
    ],
    "0.2.129": [
        "Ask \"keep checking my emails\" (or files, or any API): microcc checks quietly and only wakes on new items",
        "Your computer stays awake while microcc has work running, on macOS, Linux and Windows (WSL)",
    ],
    "0.2.112": [
        "Auto gate: a model check can allow gated calls it judges safe (turn on in /dangerous)",
        "Approval prompts now explain each call in plain language and show a risk badge",
        "Deleting files, sudo, uploads, system settings and git push always ask, even in Auto",
    ],
}


def pending() -> dict | None:
    """Note for the installed version if the user hasn't seen it yet."""
    v = version("micro-cc")
    lines = NOTES.get(v)
    if not lines or settings_store_.get_setting("seen_version") == v:
        return None
    return {"version": v, "lines": lines}


def mark_seen() -> None:
    settings_store_.edit_setting("seen_version", version("micro-cc"))
