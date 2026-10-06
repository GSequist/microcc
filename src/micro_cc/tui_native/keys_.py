"""Parse terminal keyboard sequences: Kitty keyboard protocol and legacy escape sequences."""

from __future__ import annotations

import os
import re
from types import SimpleNamespace
from typing import NamedTuple

# --- Global Kitty Protocol State ---

_kitty_protocol_active = False


def set_kitty_protocol_active(active: bool) -> None:
    """Set after detecting Kitty protocol support (e.g. from a capability query response)."""
    global _kitty_protocol_active
    _kitty_protocol_active = active


def is_kitty_protocol_active() -> bool:
    return _kitty_protocol_active


# --- Key Identifier Helpers ---


def _mod(*mods: str):
    def make(key: str) -> str:
        return "+".join((*mods, key))

    return make


Key = SimpleNamespace(
    # Special keys
    escape="escape",
    esc="esc",
    enter="enter",
    return_="return",
    tab="tab",
    space="space",
    backspace="backspace",
    delete="delete",
    insert="insert",
    clear="clear",
    home="home",
    end="end",
    pageup="pageup",
    pagedown="pagedown",
    up="up",
    down="down",
    left="left",
    right="right",
    f1="f1", f2="f2", f3="f3", f4="f4", f5="f5", f6="f6",
    f7="f7", f8="f8", f9="f9", f10="f10", f11="f11", f12="f12",
    # Symbol keys
    backtick="`", hyphen="-", equals="=", leftbracket="[", rightbracket="]",
    backslash="\\", semicolon=";", quote="'", comma=",", period=".", slash="/",
    exclamation="!", at="@", hash="#", dollar="$", percent="%", caret="^",
    ampersand="&", asterisk="*", leftparen="(", rightparen=")", underscore="_",
    plus="+", pipe="|", tilde="~", leftbrace="{", rightbrace="}", colon=":",
    lessthan="<", greaterthan=">", question="?",
    # Single modifiers
    ctrl=_mod("ctrl"),
    shift=_mod("shift"),
    alt=_mod("alt"),
    super=_mod("super"),
    # One order per combo is enough: matches_key() parses key_id order-independently.
    ctrl_shift=_mod("ctrl", "shift"),
    ctrl_alt=_mod("ctrl", "alt"),
    ctrl_super=_mod("ctrl", "super"),
    shift_alt=_mod("shift", "alt"),
    shift_super=_mod("shift", "super"),
    alt_super=_mod("alt", "super"),
    # Triple modifiers
    ctrl_shift_alt=_mod("ctrl", "shift", "alt"),
    ctrl_shift_super=_mod("ctrl", "shift", "super"),
)

# --- Constants ---

SYMBOL_KEYS = frozenset(
    "`-=[]\\;',./!@#$%^&*()_+|~{}:<>?"
)

MODIFIERS = {"shift": 1, "alt": 2, "ctrl": 4, "super": 8}

LOCK_MASK = 64 + 128  # Caps Lock + Num Lock

CODEPOINTS = {
    "escape": 27,
    "tab": 9,
    "enter": 13,
    "space": 32,
    "backspace": 127,
    "kp_enter": 57414,  # Numpad Enter (Kitty protocol)
}

ARROW_CODEPOINTS = {"up": -1, "down": -2, "right": -3, "left": -4}

FUNCTIONAL_CODEPOINTS = {
    "delete": -10,
    "insert": -11,
    "page_up": -12,
    "page_down": -13,
    "home": -14,
    "end": -15,
}

KITTY_FUNCTIONAL_KEY_EQUIVALENTS = {
    57399: 48,  # KP_0 -> 0
    57400: 49,  # KP_1 -> 1
    57401: 50,  # KP_2 -> 2
    57402: 51,  # KP_3 -> 3
    57403: 52,  # KP_4 -> 4
    57404: 53,  # KP_5 -> 5
    57405: 54,  # KP_6 -> 6
    57406: 55,  # KP_7 -> 7
    57407: 56,  # KP_8 -> 8
    57408: 57,  # KP_9 -> 9
    57409: 46,  # KP_DECIMAL -> .
    57410: 47,  # KP_DIVIDE -> /
    57411: 42,  # KP_MULTIPLY -> *
    57412: 45,  # KP_SUBTRACT -> -
    57413: 43,  # KP_ADD -> +
    57415: 61,  # KP_EQUAL -> =
    57416: 44,  # KP_SEPARATOR -> ,
    57417: ARROW_CODEPOINTS["left"],
    57418: ARROW_CODEPOINTS["right"],
    57419: ARROW_CODEPOINTS["up"],
    57420: ARROW_CODEPOINTS["down"],
    57421: FUNCTIONAL_CODEPOINTS["page_up"],
    57422: FUNCTIONAL_CODEPOINTS["page_down"],
    57423: FUNCTIONAL_CODEPOINTS["home"],
    57424: FUNCTIONAL_CODEPOINTS["end"],
    57425: FUNCTIONAL_CODEPOINTS["insert"],
    57426: FUNCTIONAL_CODEPOINTS["delete"],
}


def normalize_kitty_functional_codepoint(codepoint: int) -> int:
    return KITTY_FUNCTIONAL_KEY_EQUIVALENTS.get(codepoint, codepoint)


def normalize_shifted_letter_identity_codepoint(codepoint: int, modifier: int) -> int:
    effective_modifier = modifier & ~LOCK_MASK
    if (effective_modifier & MODIFIERS["shift"]) != 0 and 65 <= codepoint <= 90:
        return codepoint + 32
    return codepoint


def _is_known_symbol_codepoint(cp: int) -> bool:
    """Check if codepoint is a known symbol; guard range since chr() raises on negative/OOB."""
    return 0 <= cp <= 0x10FFFF and chr(cp) in SYMBOL_KEYS


LEGACY_KEY_SEQUENCES = {
    "up": ("\x1b[A", "\x1bOA"),
    "down": ("\x1b[B", "\x1bOB"),
    "right": ("\x1b[C", "\x1bOC"),
    "left": ("\x1b[D", "\x1bOD"),
    "home": ("\x1b[H", "\x1bOH", "\x1b[1~", "\x1b[7~"),
    "end": ("\x1b[F", "\x1bOF", "\x1b[4~", "\x1b[8~"),
    "insert": ("\x1b[2~",),
    "delete": ("\x1b[3~",),
    "page_up": ("\x1b[5~", "\x1b[[5~"),
    "page_down": ("\x1b[6~", "\x1b[[6~"),
    "clear": ("\x1b[E", "\x1bOE"),
    "f1": ("\x1bOP", "\x1b[11~", "\x1b[[A"),
    "f2": ("\x1bOQ", "\x1b[12~", "\x1b[[B"),
    "f3": ("\x1bOR", "\x1b[13~", "\x1b[[C"),
    "f4": ("\x1bOS", "\x1b[14~", "\x1b[[D"),
    "f5": ("\x1b[15~", "\x1b[[E"),
    "f6": ("\x1b[17~",),
    "f7": ("\x1b[18~",),
    "f8": ("\x1b[19~",),
    "f9": ("\x1b[20~",),
    "f10": ("\x1b[21~",),
    "f11": ("\x1b[23~",),
    "f12": ("\x1b[24~",),
}

LEGACY_SHIFT_SEQUENCES = {
    "up": ("\x1b[a",),
    "down": ("\x1b[b",),
    "right": ("\x1b[c",),
    "left": ("\x1b[d",),
    "clear": ("\x1b[e",),
    "insert": ("\x1b[2$",),
    "delete": ("\x1b[3$",),
    "page_up": ("\x1b[5$",),
    "page_down": ("\x1b[6$",),
    "home": ("\x1b[7$",),
    "end": ("\x1b[8$",),
}

LEGACY_CTRL_SEQUENCES = {
    "up": ("\x1bOa",),
    "down": ("\x1bOb",),
    "right": ("\x1bOc",),
    "left": ("\x1bOd",),
    "clear": ("\x1bOe",),
    "insert": ("\x1b[2^",),
    "delete": ("\x1b[3^",),
    "page_up": ("\x1b[5^",),
    "page_down": ("\x1b[6^",),
    "home": ("\x1b[7^",),
    "end": ("\x1b[8^",),
}

# Values here are output key-ids (lowercase pageup/pagedown — see module
# docstring).
_LEGACY_SEQUENCE_KEY_IDS = {
    "\x1bOA": "up",
    "\x1bOB": "down",
    "\x1bOC": "right",
    "\x1bOD": "left",
    "\x1bOH": "home",
    "\x1bOF": "end",
    "\x1b[E": "clear",
    "\x1bOE": "clear",
    "\x1bOe": "ctrl+clear",
    "\x1b[e": "shift+clear",
    "\x1b[2~": "insert",
    "\x1b[2$": "shift+insert",
    "\x1b[2^": "ctrl+insert",
    "\x1b[3$": "shift+delete",
    "\x1b[3^": "ctrl+delete",
    "\x1b[[5~": "pageup",
    "\x1b[[6~": "pagedown",
    "\x1b[a": "shift+up",
    "\x1b[b": "shift+down",
    "\x1b[c": "shift+right",
    "\x1b[d": "shift+left",
    "\x1bOa": "ctrl+up",
    "\x1bOb": "ctrl+down",
    "\x1bOc": "ctrl+right",
    "\x1bOd": "ctrl+left",
    "\x1b[5$": "shift+pageup",
    "\x1b[6$": "shift+pagedown",
    "\x1b[7$": "shift+home",
    "\x1b[8$": "shift+end",
    "\x1b[5^": "ctrl+pageup",
    "\x1b[6^": "ctrl+pagedown",
    "\x1b[7^": "ctrl+home",
    "\x1b[8^": "ctrl+end",
    "\x1bOP": "f1",
    "\x1bOQ": "f2",
    "\x1bOR": "f3",
    "\x1bOS": "f4",
    "\x1b[11~": "f1",
    "\x1b[12~": "f2",
    "\x1b[13~": "f3",
    "\x1b[14~": "f4",
    "\x1b[[A": "f1",
    "\x1b[[B": "f2",
    "\x1b[[C": "f3",
    "\x1b[[D": "f4",
    "\x1b[[E": "f5",
    "\x1b[15~": "f5",
    "\x1b[17~": "f6",
    "\x1b[18~": "f7",
    "\x1b[19~": "f8",
    "\x1b[20~": "f9",
    "\x1b[21~": "f10",
    "\x1b[23~": "f11",
    "\x1b[24~": "f12",
    "\x1bb": "alt+left",
    "\x1bf": "alt+right",
    "\x1bp": "alt+up",
    "\x1bn": "alt+down",
}


def matches_legacy_sequence(data: str, sequences) -> bool:
    return data in sequences


def matches_legacy_modifier_sequence(data: str, key: str, modifier: int) -> bool:
    if modifier == MODIFIERS["shift"]:
        return matches_legacy_sequence(data, LEGACY_SHIFT_SEQUENCES.get(key, ()))
    if modifier == MODIFIERS["ctrl"]:
        return matches_legacy_sequence(data, LEGACY_CTRL_SEQUENCES.get(key, ()))
    return False


# --- Kitty Protocol Parsing ---

# Event types from Kitty keyboard protocol (flag 2): "press"/"repeat"/"release"


class ParsedKittySequence(NamedTuple):
    codepoint: int
    shifted_key: int | None
    base_layout_key: int | None
    modifier: int
    event_type: str


class ParsedModifyOtherKeysSequence(NamedTuple):
    codepoint: int
    modifier: int


# Written by parse_kitty_sequence() but not actively used; kept for reference.
_last_event_type = "press"


def is_key_release(data: str) -> bool:
    """Check for key release with Kitty protocol flag 2 (release events contain :3)."""
    # Bracketed paste markers prevent false positives on MAC addresses, etc.
    if "\x1b[200~" in data:
        return False
    return any(s in data for s in (":3u", ":3~", ":3A", ":3B", ":3C", ":3D", ":3H", ":3F"))


def is_key_repeat(data: str) -> bool:
    """Check for key repeat with Kitty protocol flag 2 (repeat events contain :2)."""
    if "\x1b[200~" in data:
        return False
    return any(s in data for s in (":2u", ":2~", ":2A", ":2B", ":2C", ":2D", ":2H", ":2F"))


def _parse_event_type(event_type_str: str | None) -> str:
    if not event_type_str:
        return "press"
    if event_type_str == "2":
        return "repeat"
    if event_type_str == "3":
        return "release"
    return "press"


# CSI-u: \x1b[<codepoint>[:<shifted>[:<base>]][;<mod>[:<event>]]u (flag 2: event type, flag 4: alternates)
_CSI_U_RE = re.compile(r"^\x1b\[(\d+)(?::(\d*))?(?::(\d+))?(?:;(\d+))?(?::(\d+))?u$")

# Arrow keys with modifier: \x1b[1;<mod>A/B/C/D or \x1b[1;<mod>:<event>A/B/C/D
_ARROW_RE = re.compile(r"^\x1b\[1;(\d+)(?::(\d+))?([ABCD])$")

# Functional keys: \x1b[<num>~ or \x1b[<num>;<mod>~ or \x1b[<num>;<mod>:<event>~
_FUNC_RE = re.compile(r"^\x1b\[(\d+)(?:;(\d+))?(?::(\d+))?~$")

# Home/End with modifier: \x1b[1;<mod>H/F or \x1b[1;<mod>:<event>H/F
_HOME_END_RE = re.compile(r"^\x1b\[1;(\d+)(?::(\d+))?([HF])$")

_FUNC_KEY_CODES = {
    2: FUNCTIONAL_CODEPOINTS["insert"],
    3: FUNCTIONAL_CODEPOINTS["delete"],
    5: FUNCTIONAL_CODEPOINTS["page_up"],
    6: FUNCTIONAL_CODEPOINTS["page_down"],
    7: FUNCTIONAL_CODEPOINTS["home"],
    8: FUNCTIONAL_CODEPOINTS["end"],
}

_ARROW_CODES_BY_LETTER = {"A": -1, "B": -2, "C": -3, "D": -4}


def parse_kitty_sequence(data: str) -> ParsedKittySequence | None:
    global _last_event_type

    m = _CSI_U_RE.match(data)
    if m:
        codepoint = int(m.group(1))
        shifted_key = int(m.group(2)) if m.group(2) else None
        base_layout_key = int(m.group(3)) if m.group(3) else None
        mod_value = int(m.group(4)) if m.group(4) else 1
        event_type = _parse_event_type(m.group(5))
        _last_event_type = event_type
        return ParsedKittySequence(codepoint, shifted_key, base_layout_key, mod_value - 1, event_type)

    m = _ARROW_RE.match(data)
    if m:
        mod_value = int(m.group(1))
        event_type = _parse_event_type(m.group(2))
        _last_event_type = event_type
        return ParsedKittySequence(_ARROW_CODES_BY_LETTER[m.group(3)], None, None, mod_value - 1, event_type)

    m = _FUNC_RE.match(data)
    if m:
        key_num = int(m.group(1))
        mod_value = int(m.group(2)) if m.group(2) else 1
        event_type = _parse_event_type(m.group(3))
        codepoint = _FUNC_KEY_CODES.get(key_num)
        if codepoint is not None:
            _last_event_type = event_type
            return ParsedKittySequence(codepoint, None, None, mod_value - 1, event_type)

    m = _HOME_END_RE.match(data)
    if m:
        mod_value = int(m.group(1))
        event_type = _parse_event_type(m.group(2))
        codepoint = FUNCTIONAL_CODEPOINTS["home"] if m.group(3) == "H" else FUNCTIONAL_CODEPOINTS["end"]
        _last_event_type = event_type
        return ParsedKittySequence(codepoint, None, None, mod_value - 1, event_type)

    return None


def matches_kitty_sequence(data: str, expected_codepoint: int, expected_modifier: int) -> bool:
    parsed = parse_kitty_sequence(data)
    if not parsed:
        return False
    actual_mod = parsed.modifier & ~LOCK_MASK
    expected_mod = expected_modifier & ~LOCK_MASK
    if actual_mod != expected_mod:
        return False

    normalized_codepoint = normalize_shifted_letter_identity_codepoint(
        normalize_kitty_functional_codepoint(parsed.codepoint), parsed.modifier
    )
    normalized_expected_codepoint = normalize_shifted_letter_identity_codepoint(
        normalize_kitty_functional_codepoint(expected_codepoint), expected_modifier
    )

    if normalized_codepoint == normalized_expected_codepoint:
        return True

    # Use base layout key for non-Latin layouts (e.g., Cyrillic) but only for
    # unrecognized codepoints; recognized Latin letters/symbols are authoritative.
    if parsed.base_layout_key is not None and parsed.base_layout_key == expected_codepoint:
        cp = normalized_codepoint
        is_latin_letter = 97 <= cp <= 122
        if not is_latin_letter and not _is_known_symbol_codepoint(cp):
            return True

    return False


def parse_modify_other_keys_sequence(data: str) -> ParsedModifyOtherKeysSequence | None:
    m = re.match(r"^\x1b\[27;(\d+);(\d+)~$", data)
    if not m:
        return None
    mod_value = int(m.group(1))
    codepoint = int(m.group(2))
    return ParsedModifyOtherKeysSequence(codepoint, mod_value - 1)


def matches_modify_other_keys(data: str, expected_keycode: int, expected_modifier: int) -> bool:
    """Match xterm modifyOtherKeys format (CSI 27; modifiers; keycode ~)."""
    parsed = parse_modify_other_keys_sequence(data)
    if not parsed:
        return False
    return parsed.codepoint == expected_keycode and parsed.modifier == expected_modifier


def is_windows_terminal_session() -> bool:
    env = os.environ
    return bool(env.get("WT_SESSION")) and not env.get("SSH_CONNECTION") and not env.get("SSH_CLIENT") and not env.get("SSH_TTY")


def matches_raw_backspace(data: str, expected_modifier: int) -> bool:
    """Match raw backspace; 0x08 is ambiguous (Windows Terminal uses it for Ctrl+BS)."""
    if data == "\x7f":
        return expected_modifier == 0
    if data != "\x08":
        return False
    if is_windows_terminal_session():
        return expected_modifier == MODIFIERS["ctrl"]
    return expected_modifier == 0


# --- Generic Key Matching ---


def raw_ctrl_char(key: str) -> str | None:
    """Get control char using code & 0x1f formula; handles a-z and [\\]_."""
    char = key.lower()
    code = ord(char)
    if (97 <= code <= 122) or char in ("[", "\\", "]", "_"):
        return chr(code & 0x1F)
    if char == "-":
        return chr(31)  # Same as Ctrl+_
    return None


def is_digit_key(key: str) -> bool:
    return "0" <= key <= "9"


def matches_printable_modify_other_keys(data: str, expected_keycode: int, expected_modifier: int) -> bool:
    if expected_modifier == 0:
        return False
    parsed = parse_modify_other_keys_sequence(data)
    if not parsed or parsed.modifier != expected_modifier:
        return False
    return normalize_shifted_letter_identity_codepoint(
        parsed.codepoint, parsed.modifier
    ) == normalize_shifted_letter_identity_codepoint(expected_keycode, expected_modifier)


def format_key_name_with_modifiers(key_name: str, modifier: int) -> str | None:
    effective_mod = modifier & ~LOCK_MASK
    supported_modifier_mask = MODIFIERS["shift"] | MODIFIERS["ctrl"] | MODIFIERS["alt"] | MODIFIERS["super"]
    if (effective_mod & ~supported_modifier_mask) != 0:
        return None
    mods = []
    # Alphabetical order (alt, ctrl, shift, super) — see module docstring.
    if effective_mod & MODIFIERS["alt"]:
        mods.append("alt")
    if effective_mod & MODIFIERS["ctrl"]:
        mods.append("ctrl")
    if effective_mod & MODIFIERS["shift"]:
        mods.append("shift")
    if effective_mod & MODIFIERS["super"]:
        mods.append("super")
    if mods:
        return "+".join(mods) + "+" + key_name
    return key_name


def parse_key_id(key_id: str) -> dict | None:
    parts = key_id.lower().split("+")
    key = parts[-1] if parts else ""
    if not key:
        return None
    return {
        "key": key,
        "ctrl": "ctrl" in parts,
        "shift": "shift" in parts,
        "alt": "alt" in parts,
        "super": "super" in parts,
    }


def matches_key(data: str, key_id: str) -> bool:
    """Match input data against a key identifier (case/order-insensitive)."""
    parsed = parse_key_id(key_id)
    if not parsed:
        return False

    key = parsed["key"]
    modifier = 0
    if parsed["shift"]:
        modifier |= MODIFIERS["shift"]
    if parsed["alt"]:
        modifier |= MODIFIERS["alt"]
    if parsed["ctrl"]:
        modifier |= MODIFIERS["ctrl"]
    if parsed["super"]:
        modifier |= MODIFIERS["super"]

    if key in ("escape", "esc"):
        if modifier != 0:
            return False
        return (
            data == "\x1b"
            or matches_kitty_sequence(data, CODEPOINTS["escape"], 0)
            or matches_modify_other_keys(data, CODEPOINTS["escape"], 0)
        )

    if key == "space":
        if not _kitty_protocol_active:
            if modifier == MODIFIERS["ctrl"] and data == "\x00":
                return True
            if modifier == MODIFIERS["alt"] and data == "\x1b ":
                return True
        if modifier == 0:
            return (
                data == " "
                or matches_kitty_sequence(data, CODEPOINTS["space"], 0)
                or matches_modify_other_keys(data, CODEPOINTS["space"], 0)
            )
        return matches_kitty_sequence(data, CODEPOINTS["space"], modifier) or matches_modify_other_keys(
            data, CODEPOINTS["space"], modifier
        )

    if key == "tab":
        if modifier == MODIFIERS["shift"]:
            return (
                data == "\x1b[Z"
                or matches_kitty_sequence(data, CODEPOINTS["tab"], MODIFIERS["shift"])
                or matches_modify_other_keys(data, CODEPOINTS["tab"], MODIFIERS["shift"])
            )
        if modifier == 0:
            return data == "\t" or matches_kitty_sequence(data, CODEPOINTS["tab"], 0)
        return matches_kitty_sequence(data, CODEPOINTS["tab"], modifier) or matches_modify_other_keys(
            data, CODEPOINTS["tab"], modifier
        )

    if key in ("enter", "return"):
        if modifier == MODIFIERS["shift"]:
            # CSI u sequences (standard Kitty protocol)
            if matches_kitty_sequence(data, CODEPOINTS["enter"], MODIFIERS["shift"]) or matches_kitty_sequence(
                data, CODEPOINTS["kp_enter"], MODIFIERS["shift"]
            ):
                return True
            # xterm modifyOtherKeys format (fallback when Kitty protocol not enabled)
            if matches_modify_other_keys(data, CODEPOINTS["enter"], MODIFIERS["shift"]):
                return True
            # With Kitty protocol active, legacy sequences are custom terminal mappings.
            if _kitty_protocol_active:
                return data == "\x1b\r" or data == "\n"
            return False
        if modifier == MODIFIERS["alt"]:
            if matches_kitty_sequence(data, CODEPOINTS["enter"], MODIFIERS["alt"]) or matches_kitty_sequence(
                data, CODEPOINTS["kp_enter"], MODIFIERS["alt"]
            ):
                return True
            if matches_modify_other_keys(data, CODEPOINTS["enter"], MODIFIERS["alt"]):
                return True
            # \x1b\r is alt+enter only in legacy mode (no Kitty protocol).
            # When Kitty protocol is active, alt+enter comes as CSI u sequence.
            if not _kitty_protocol_active:
                return data == "\x1b\r"
            return False
        if modifier == 0:
            return (
                data == "\r"
                or (not _kitty_protocol_active and data == "\n")
                or data == "\x1bOM"  # SS3 M (numpad enter in some terminals)
                or matches_kitty_sequence(data, CODEPOINTS["enter"], 0)
                or matches_kitty_sequence(data, CODEPOINTS["kp_enter"], 0)
            )
        return (
            matches_kitty_sequence(data, CODEPOINTS["enter"], modifier)
            or matches_kitty_sequence(data, CODEPOINTS["kp_enter"], modifier)
            or matches_modify_other_keys(data, CODEPOINTS["enter"], modifier)
        )

    if key == "backspace":
        if modifier == MODIFIERS["alt"]:
            if data == "\x1b\x7f" or data == "\x1b\b":
                return True
            return matches_kitty_sequence(data, CODEPOINTS["backspace"], MODIFIERS["alt"]) or matches_modify_other_keys(
                data, CODEPOINTS["backspace"], MODIFIERS["alt"]
            )
        if modifier == MODIFIERS["ctrl"]:
            # Raw 0x08 is ambiguous between Ctrl+Backspace and Backspace.
            if matches_raw_backspace(data, MODIFIERS["ctrl"]):
                return True
            return matches_kitty_sequence(data, CODEPOINTS["backspace"], MODIFIERS["ctrl"]) or matches_modify_other_keys(
                data, CODEPOINTS["backspace"], MODIFIERS["ctrl"]
            )
        if modifier == 0:
            return (
                matches_raw_backspace(data, 0)
                or matches_kitty_sequence(data, CODEPOINTS["backspace"], 0)
                or matches_modify_other_keys(data, CODEPOINTS["backspace"], 0)
            )
        return matches_kitty_sequence(data, CODEPOINTS["backspace"], modifier) or matches_modify_other_keys(
            data, CODEPOINTS["backspace"], modifier
        )

    if key == "insert":
        if modifier == 0:
            return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["insert"]) or matches_kitty_sequence(
                data, FUNCTIONAL_CODEPOINTS["insert"], 0
            )
        if matches_legacy_modifier_sequence(data, "insert", modifier):
            return True
        return matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS["insert"], modifier)

    if key == "delete":
        if modifier == 0:
            return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["delete"]) or matches_kitty_sequence(
                data, FUNCTIONAL_CODEPOINTS["delete"], 0
            )
        if matches_legacy_modifier_sequence(data, "delete", modifier):
            return True
        return matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS["delete"], modifier)

    if key == "clear":
        if modifier == 0:
            return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["clear"])
        return matches_legacy_modifier_sequence(data, "clear", modifier)

    if key == "home":
        if modifier == 0:
            return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["home"]) or matches_kitty_sequence(
                data, FUNCTIONAL_CODEPOINTS["home"], 0
            )
        if matches_legacy_modifier_sequence(data, "home", modifier):
            return True
        return matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS["home"], modifier)

    if key == "end":
        if modifier == 0:
            return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["end"]) or matches_kitty_sequence(
                data, FUNCTIONAL_CODEPOINTS["end"], 0
            )
        if matches_legacy_modifier_sequence(data, "end", modifier):
            return True
        return matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS["end"], modifier)

    if key == "pageup":
        if modifier == 0:
            return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["page_up"]) or matches_kitty_sequence(
                data, FUNCTIONAL_CODEPOINTS["page_up"], 0
            )
        if matches_legacy_modifier_sequence(data, "page_up", modifier):
            return True
        return matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS["page_up"], modifier)

    if key == "pagedown":
        if modifier == 0:
            return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["page_down"]) or matches_kitty_sequence(
                data, FUNCTIONAL_CODEPOINTS["page_down"], 0
            )
        if matches_legacy_modifier_sequence(data, "page_down", modifier):
            return True
        return matches_kitty_sequence(data, FUNCTIONAL_CODEPOINTS["page_down"], modifier)

    if key == "up":
        if modifier == MODIFIERS["alt"]:
            return data == "\x1bp" or matches_kitty_sequence(data, ARROW_CODEPOINTS["up"], MODIFIERS["alt"])
        if modifier == 0:
            return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["up"]) or matches_kitty_sequence(
                data, ARROW_CODEPOINTS["up"], 0
            )
        if matches_legacy_modifier_sequence(data, "up", modifier):
            return True
        return matches_kitty_sequence(data, ARROW_CODEPOINTS["up"], modifier)

    if key == "down":
        if modifier == MODIFIERS["alt"]:
            return data == "\x1bn" or matches_kitty_sequence(data, ARROW_CODEPOINTS["down"], MODIFIERS["alt"])
        if modifier == 0:
            return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["down"]) or matches_kitty_sequence(
                data, ARROW_CODEPOINTS["down"], 0
            )
        if matches_legacy_modifier_sequence(data, "down", modifier):
            return True
        return matches_kitty_sequence(data, ARROW_CODEPOINTS["down"], modifier)

    if key == "left":
        if modifier == MODIFIERS["alt"]:
            return (
                data == "\x1b[1;3D"
                or (not _kitty_protocol_active and data == "\x1bB")
                or data == "\x1bb"
                or matches_kitty_sequence(data, ARROW_CODEPOINTS["left"], MODIFIERS["alt"])
            )
        if modifier == MODIFIERS["ctrl"]:
            return (
                data == "\x1b[1;5D"
                or matches_legacy_modifier_sequence(data, "left", MODIFIERS["ctrl"])
                or matches_kitty_sequence(data, ARROW_CODEPOINTS["left"], MODIFIERS["ctrl"])
            )
        if modifier == 0:
            return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["left"]) or matches_kitty_sequence(
                data, ARROW_CODEPOINTS["left"], 0
            )
        if matches_legacy_modifier_sequence(data, "left", modifier):
            return True
        return matches_kitty_sequence(data, ARROW_CODEPOINTS["left"], modifier)

    if key == "right":
        if modifier == MODIFIERS["alt"]:
            return (
                data == "\x1b[1;3C"
                or (not _kitty_protocol_active and data == "\x1bF")
                or data == "\x1bf"
                or matches_kitty_sequence(data, ARROW_CODEPOINTS["right"], MODIFIERS["alt"])
            )
        if modifier == MODIFIERS["ctrl"]:
            return (
                data == "\x1b[1;5C"
                or matches_legacy_modifier_sequence(data, "right", MODIFIERS["ctrl"])
                or matches_kitty_sequence(data, ARROW_CODEPOINTS["right"], MODIFIERS["ctrl"])
            )
        if modifier == 0:
            return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES["right"]) or matches_kitty_sequence(
                data, ARROW_CODEPOINTS["right"], 0
            )
        if matches_legacy_modifier_sequence(data, "right", modifier):
            return True
        return matches_kitty_sequence(data, ARROW_CODEPOINTS["right"], modifier)

    if key in ("f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12"):
        if modifier != 0:
            return False
        return matches_legacy_sequence(data, LEGACY_KEY_SEQUENCES[key])

    # Handle single letter/digit keys and symbols
    if len(key) == 1 and (("a" <= key <= "z") or is_digit_key(key) or key in SYMBOL_KEYS):
        codepoint = ord(key)
        raw_ctrl = raw_ctrl_char(key)
        is_letter = "a" <= key <= "z"
        is_digit = is_digit_key(key)

        if modifier == MODIFIERS["ctrl"] + MODIFIERS["alt"] and not _kitty_protocol_active and raw_ctrl:
            # Legacy ctrl+alt+key is ESC + control char; else continue so CSI-u/modifyOtherKeys from tmux match.
            if data == f"\x1b{raw_ctrl}":
                return True

        if modifier == MODIFIERS["alt"] and not _kitty_protocol_active and (is_letter or is_digit or key in SYMBOL_KEYS):
            # Legacy: alt+printable key is ESC followed by the key
            if data == f"\x1b{key}":
                return True

        if modifier == MODIFIERS["ctrl"]:
            # Legacy: ctrl+key sends the control character
            if raw_ctrl and data == raw_ctrl:
                return True
            return matches_kitty_sequence(data, codepoint, MODIFIERS["ctrl"]) or matches_printable_modify_other_keys(
                data, codepoint, MODIFIERS["ctrl"]
            )

        if modifier == MODIFIERS["shift"] + MODIFIERS["ctrl"]:
            combo = MODIFIERS["shift"] + MODIFIERS["ctrl"]
            return matches_kitty_sequence(data, codepoint, combo) or matches_printable_modify_other_keys(
                data, codepoint, combo
            )

        if modifier == MODIFIERS["shift"]:
            # Legacy: shift+letter produces uppercase
            if is_letter and data == key.upper():
                return True
            return matches_kitty_sequence(data, codepoint, MODIFIERS["shift"]) or matches_printable_modify_other_keys(
                data, codepoint, MODIFIERS["shift"]
            )

        if modifier != 0:
            return matches_kitty_sequence(data, codepoint, modifier) or matches_printable_modify_other_keys(
                data, codepoint, modifier
            )

        # Check both raw char and Kitty sequence (needed for release events)
        return data == key or matches_kitty_sequence(data, codepoint, 0)

    return False


# --- parse_key ---


def format_parsed_key(codepoint: int, modifier: int, base_layout_key: int | None = None) -> str | None:
    normalized_codepoint = normalize_kitty_functional_codepoint(codepoint)
    identity_codepoint = normalize_shifted_letter_identity_codepoint(normalized_codepoint, modifier)

    # Use base layout key only for unrecognized codepoints; Latin letters/digits/symbols are authoritative.
    is_latin_letter = 97 <= identity_codepoint <= 122
    is_digit = 48 <= identity_codepoint <= 57
    is_known_symbol = _is_known_symbol_codepoint(identity_codepoint)
    if is_latin_letter or is_digit or is_known_symbol:
        effective_codepoint = identity_codepoint
    else:
        effective_codepoint = base_layout_key if base_layout_key is not None else identity_codepoint

    key_name: str | None = None
    if effective_codepoint == CODEPOINTS["escape"]:
        key_name = "escape"
    elif effective_codepoint == CODEPOINTS["tab"]:
        key_name = "tab"
    elif effective_codepoint in (CODEPOINTS["enter"], CODEPOINTS["kp_enter"]):
        key_name = "enter"
    elif effective_codepoint == CODEPOINTS["space"]:
        key_name = "space"
    elif effective_codepoint == CODEPOINTS["backspace"]:
        key_name = "backspace"
    elif effective_codepoint == FUNCTIONAL_CODEPOINTS["delete"]:
        key_name = "delete"
    elif effective_codepoint == FUNCTIONAL_CODEPOINTS["insert"]:
        key_name = "insert"
    elif effective_codepoint == FUNCTIONAL_CODEPOINTS["home"]:
        key_name = "home"
    elif effective_codepoint == FUNCTIONAL_CODEPOINTS["end"]:
        key_name = "end"
    elif effective_codepoint == FUNCTIONAL_CODEPOINTS["page_up"]:
        key_name = "pageup"
    elif effective_codepoint == FUNCTIONAL_CODEPOINTS["page_down"]:
        key_name = "pagedown"
    elif effective_codepoint == ARROW_CODEPOINTS["up"]:
        key_name = "up"
    elif effective_codepoint == ARROW_CODEPOINTS["down"]:
        key_name = "down"
    elif effective_codepoint == ARROW_CODEPOINTS["left"]:
        key_name = "left"
    elif effective_codepoint == ARROW_CODEPOINTS["right"]:
        key_name = "right"
    elif 48 <= effective_codepoint <= 57:
        key_name = chr(effective_codepoint)
    elif 97 <= effective_codepoint <= 122:
        key_name = chr(effective_codepoint)
    elif _is_known_symbol_codepoint(effective_codepoint):
        key_name = chr(effective_codepoint)

    if not key_name:
        return None
    return format_key_name_with_modifiers(key_name, modifier)


def parse_key(data: str) -> str | None:
    """Parse input data and return the key identifier if recognized."""
    kitty = parse_kitty_sequence(data)
    if kitty:
        return format_parsed_key(kitty.codepoint, kitty.modifier, kitty.base_layout_key)

    modify_other_keys = parse_modify_other_keys_sequence(data)
    if modify_other_keys:
        return format_parsed_key(modify_other_keys.codepoint, modify_other_keys.modifier)

    # With Kitty protocol, \x1b\r and \n are shift+enter (custom terminal mappings), not alt+enter.
    if _kitty_protocol_active:
        if data == "\x1b\r" or data == "\n":
            return "shift+enter"

    legacy_sequence_key_id = _LEGACY_SEQUENCE_KEY_IDS.get(data)
    if legacy_sequence_key_id:
        return legacy_sequence_key_id

    # Legacy sequences (used when Kitty protocol is not active, or for unambiguous sequences)
    if data == "\x1b":
        return "escape"
    if data == "\x1c":
        return "ctrl+\\"
    if data == "\x1d":
        return "ctrl+]"
    if data == "\x1f":
        return "ctrl+-"
    # Alphabetical modifier order (alt+ctrl) for consistency with format_key_name_with_modifiers().
    if data == "\x1b\x1b":
        return "alt+ctrl+["
    if data == "\x1b\x1c":
        return "alt+ctrl+\\"
    if data == "\x1b\x1d":
        return "alt+ctrl+]"
    if data == "\x1b\x1f":
        return "alt+ctrl+-"
    if data == "\t":
        return "tab"
    if data == "\r" or (not _kitty_protocol_active and data == "\n") or data == "\x1bOM":
        return "enter"
    if data == "\x00":
        return "ctrl+space"
    if data == " ":
        return "space"
    if data == "\x7f":
        return "backspace"
    if data == "\x08":
        return "ctrl+backspace" if is_windows_terminal_session() else "backspace"
    if data == "\x1b[Z":
        return "shift+tab"
    if not _kitty_protocol_active and data == "\x1b\r":
        return "alt+enter"
    if not _kitty_protocol_active and data == "\x1b ":
        return "alt+space"
    if data == "\x1b\x7f" or data == "\x1b\b":
        return "alt+backspace"
    if not _kitty_protocol_active and data == "\x1bB":
        return "alt+left"
    if not _kitty_protocol_active and data == "\x1bF":
        return "alt+right"
    if not _kitty_protocol_active and len(data) == 2 and data[0] == "\x1b":
        code = ord(data[1])
        if 1 <= code <= 26:
            return f"alt+ctrl+{chr(code + 96)}"
        # Legacy alt+letter/digit/symbol (ESC followed by the key)
        key = chr(code)
        if (97 <= code <= 122) or (48 <= code <= 57) or key in SYMBOL_KEYS:
            return f"alt+{key}"
    if data == "\x1b[A":
        return "up"
    if data == "\x1b[B":
        return "down"
    if data == "\x1b[C":
        return "right"
    if data == "\x1b[D":
        return "left"
    if data == "\x1b[H" or data == "\x1bOH":
        return "home"
    if data == "\x1b[F" or data == "\x1bOF":
        return "end"
    if data == "\x1b[3~":
        return "delete"
    if data == "\x1b[5~":
        return "pageup"
    if data == "\x1b[6~":
        return "pagedown"

    # Raw Ctrl+letter
    if len(data) == 1:
        code = ord(data)
        if 1 <= code <= 26:
            return f"ctrl+{chr(code + 96)}"
        if 32 <= code <= 126:
            return data

    return None


# --- Kitty CSI-u Printable Decoding ---

_KITTY_PRINTABLE_ALLOWED_MODIFIERS = MODIFIERS["shift"] | LOCK_MASK


def decode_kitty_printable(data: str) -> str | None:
    """Extract printable char from Kitty CSI-u sequence; only plain or Shift-modified keys."""
    m = _CSI_U_RE.match(data)
    if not m:
        return None

    # CSI-u groups: <codepoint>[:<shifted>[:<base>]];<mod>[:<event>]u
    codepoint = int(m.group(1))
    shifted_key = int(m.group(2)) if m.group(2) else None
    mod_value = int(m.group(4)) if m.group(4) else 1
    # Modifiers are 1-indexed in CSI-u; normalize to our bitmask.
    modifier = mod_value - 1

    # Accept only plain or Shift-modified text keys; reject other modifier combinations.
    if (modifier & ~_KITTY_PRINTABLE_ALLOWED_MODIFIERS) != 0:
        return None
    if modifier & (MODIFIERS["alt"] | MODIFIERS["ctrl"]):
        return None

    # Prefer the shifted keycode when Shift is held.
    effective_codepoint = codepoint
    if modifier & MODIFIERS["shift"] and shifted_key is not None:
        effective_codepoint = shifted_key
    effective_codepoint = normalize_kitty_functional_codepoint(effective_codepoint)
    # Drop control characters or invalid codepoints.
    if effective_codepoint < 32:
        return None

    try:
        return chr(effective_codepoint)
    except ValueError:
        return None


def decode_modify_other_keys_printable(data: str) -> str | None:
    parsed = parse_modify_other_keys_sequence(data)
    if not parsed:
        return None
    modifier = parsed.modifier & ~LOCK_MASK
    if (modifier & ~MODIFIERS["shift"]) != 0:
        return None
    if parsed.codepoint < 32:
        return None
    try:
        return chr(parsed.codepoint)
    except ValueError:
        return None


def decode_printable_key(data: str) -> str | None:
    return decode_kitty_printable(data) or decode_modify_other_keys_printable(data)
