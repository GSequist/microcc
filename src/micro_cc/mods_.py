"""Mods: ~/.micro-cc/mods/<name>/mod.py, register(on) adds middleware handlers to one bus."""

import importlib
import inspect
import re
import shutil
import signal
import sys
import threading
import time
import types
from contextlib import contextmanager
from pathlib import Path

from rich.text import Text

from micro_cc.tui_native.keys_ import matches_key, parse_key_id

API_VERSION = 2
EVENTS = ("render", "glyphs", "command", "key")
LEGACY = ("commands", "renderers", "panels", "keys.json", "glyphs.json", "banner.txt")  # 0.2.103 seams
SLOW_S = 0.1  # sync handler self-time that disables its mod after it returns
HANG_S = 0.5  # sync handler self-time at which the watchdog interrupts it
LOAD_HANG_S = 5.0  # import + register(on) of one mod
_PKG = "micro_cc_mods"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_handlers: dict[str, list[tuple[dict, object, str]]] = {}
_disabled: set[str] = set()
_errors: list[str] = []
_generation = 0
_api_factory = lambda: None


def user_dir() -> Path:
    return Path.home() / ".micro-cc"


def mods_dir() -> Path:
    return user_dir() / "mods"


# --- errors ----------------------------------------------------------------

def record_error(source: str, msg: str) -> None:
    """Record a user-layer failure once."""
    line = f"{source}: {msg}"
    if line not in _errors:
        _errors.append(line)


def errors() -> list[str]:
    return list(_errors)


def clear_errors() -> None:
    _errors.clear()


def fail(mod: str, msg: str) -> None:
    """Disable a mod for the rest of the process."""
    _disabled.add(mod)
    record_error(f"mods/{mod}", f"{msg}, disabled")
    bump()


# --- watchdog ----------------------------------------------------------------

class ModHung(BaseException):
    """Raised inside a stuck mod by SIGALRM; BaseException so `except Exception` can't swallow it."""


def _can_alarm() -> bool:
    return hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()


@contextmanager
def _guard(mod: str, seconds: float):
    """Interrupt mod code after seconds; restores any enclosing guard's handler and deadline."""
    if not _can_alarm():
        yield
        return

    def fire(signum, frame):
        raise ModHung(mod)

    outer = signal.setitimer(signal.ITIMER_REAL, 0)[0]
    prev = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prev)
        if outer > 0:
            signal.setitimer(signal.ITIMER_REAL, outer)


@contextmanager
def _paused():
    """Stop the enclosing guard's clock while inner code (next mod or builtin) runs."""
    if not _can_alarm():
        yield
        return
    left = signal.setitimer(signal.ITIMER_REAL, 0)[0]
    try:
        yield
    finally:
        if left > 0:
            signal.setitimer(signal.ITIMER_REAL, left)


# --- loading ---------------------------------------------------------------

def bind(api_factory) -> None:
    """Set the zero-arg callable that returns the api handed to handlers."""
    global _api_factory
    _api_factory = api_factory


def load() -> list[str]:
    """Import every enabled mod and collect its handlers; returns loaded names."""
    _handlers.clear()
    _disabled.clear()
    for k in [k for k in sys.modules if k == _PKG or k.startswith(_PKG + ".")]:
        del sys.modules[k]
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = [str(mods_dir())]
    sys.modules[_PKG] = pkg
    importlib.invalidate_caches()  # FileFinder may hold a stale listing of a just-written mod
    try:
        dirs = sorted(p.name for p in mods_dir().iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))
    except OSError:
        dirs = []
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # keep __pycache__ out of the mod folders
    try:
        loaded = [n for n in dirs if _load_one(n)]
    finally:
        sys.dont_write_bytecode = old
    bump()
    return loaded


def _check(event: str, filters: dict) -> dict:
    """Validate and normalize one registration; raises ValueError."""
    if event not in EVENTS:
        raise ValueError(f"unknown event {event!r}, expected one of {EVENTS}")
    if event == "command":
        name = str(filters.get("name", "")).lstrip("/").lower()
        if not _NAME_RE.match(name):
            raise ValueError(f"command needs name= of lowercase letters, digits, - or _ (got {name!r})")
        filters = {**filters, "name": name}
    if event == "key":
        key = filters.get("key")
        if not isinstance(key, str) or is_reserved(key):
            raise ValueError(f"key needs key= and cannot bind ctrl+c, escape or enter (got {key!r})")
    return filters


def _load_one(name: str) -> bool:
    if not (mods_dir() / name / "mod.py").is_file():
        record_error(f"mods/{name}", "no mod.py, skipped")
        return False
    added = []

    def on(event, **filters):
        def deco(fn):
            added.append((event, _check(event, filters), fn))
            return fn
        return deco

    try:
        with _guard(name, LOAD_HANG_S):
            mod = importlib.import_module(f"{_PKG}.{name}.mod")
            ver = getattr(mod, "API_VERSION", API_VERSION)
            if ver != API_VERSION:
                raise ValueError(f"API_VERSION {ver!r} != {API_VERSION}")
            if not callable(getattr(mod, "register", None)):
                raise ValueError("no register(on)")
            mod.register(on)
    except ModHung:
        record_error(f"mods/{name}", f"loading took over {LOAD_HANG_S}s, disabled")
        return False
    except (Exception, SystemExit) as e:
        record_error(f"mods/{name}", f"{type(e).__name__}: {e}, disabled")
        return False
    for event, filters, fn in added:
        _handlers.setdefault(event, []).append((filters, fn, name))
    return True


def is_reserved(key_id: str) -> bool:
    """ctrl+c, escape and enter (any modifiers) are never handed to mods."""
    p = parse_key_id(key_id)
    if p is None:
        return True
    if p["key"] in ("escape", "esc", "enter"):
        return True
    return p["key"] == "c" and p["ctrl"] and not (p["shift"] or p["alt"] or p["super"])


# --- dispatch --------------------------------------------------------------

def bump() -> None:
    """Invalidate cached mod renders."""
    global _generation
    _generation += 1


def generation() -> int:
    return _generation


def _matches(filters: dict, ctx: dict) -> bool:
    return all(ctx.get(k) == v for k, v in filters.items() if k != "hint")


def _links(event: str, ctx: dict, match) -> list:
    return [h for h in _handlers.get(event, ()) if h[2] not in _disabled and match(h[0], ctx)]


def has(event: str, **ctx) -> bool:
    return bool(_links(event, ctx, _matches))


def dispatch(event: str, e: dict, builtin, match=None, norm=None, **ctx):
    """Run matching handlers as a chain around builtin; a raising, slow or hung handler disables its mod."""
    links = _links(event, ctx, match or _matches)
    norm = norm or (lambda x: x)
    if not links:
        return norm(builtin(e))
    api = _api_factory()

    def run(i, e):
        if i == len(links):
            return norm(builtin(e))
        _, fn, mod = links[i]
        if mod in _disabled:
            return run(i + 1, e)
        inner = [0.0]

        def nxt(e2=None):
            t = time.perf_counter()
            try:
                with _paused():
                    return run(i + 1, e if e2 is None else e2)
            finally:
                inner[0] += time.perf_counter() - t

        t0 = time.perf_counter()
        try:
            with _guard(mod, HANG_S):
                out = norm(fn(api, e, nxt))
        except (Exception, ModHung) as err:
            why = f"hung over {HANG_S}s, interrupted" if isinstance(err, ModHung) else f"{type(err).__name__}: {err}"
            fail(mod, f"{event} handler {getattr(fn, '__name__', '?')}: {why}")
            return run(i + 1, e)
        if time.perf_counter() - t0 - inner[0] > SLOW_S:
            fail(mod, f"{event} handler {getattr(fn, '__name__', '?')} took over {int(SLOW_S * 1000)}ms")
        return out

    return run(0, e)


async def _maybe(x):
    return await x if inspect.isawaitable(x) else x


async def adispatch(event: str, e: dict, builtin, **ctx):
    """Async chain for commands; a raising handler disables its mod and re-raises."""
    links = _links(event, ctx, _matches)
    api = _api_factory()

    async def run(i, e):
        if i == len(links):
            return await _maybe(builtin(e))
        _, fn, mod = links[i]
        if mod in _disabled:
            return await run(i + 1, e)
        try:
            # Guard covers a sync handler's body; an async body runs later on the loop, unguarded.
            with _guard(mod, HANG_S):
                out = fn(api, e, lambda e2=None: run(i + 1, e if e2 is None else e2))
            return await _maybe(out)
        except ModHung:
            fail(mod, f"{event} handler {getattr(fn, '__name__', '?')}: hung over {HANG_S}s, interrupted")
            raise RuntimeError(f"mod {mod} hung and was disabled") from None
        except Exception as err:
            fail(mod, f"{event} handler {getattr(fn, '__name__', '?')}: {type(err).__name__}: {err}")
            raise

    return await run(0, e)


# --- event helpers used by core ----------------------------------------------

def _lines(x) -> list:
    if x is None:
        return []
    return [x] if isinstance(x, str) else [str(s) for s in x]


def render_markup(component: str, markup: str, width: int) -> str:
    """Component markup through the render chain; invalid output falls back to builtin."""
    builtin = lambda e: e["markup"].split("\n") if e["markup"] else []
    out = "\n".join(dispatch("render", {"component": component, "markup": markup, "width": width},
                             builtin, norm=_lines, component=component))
    try:
        Text.from_markup(out)
    except Exception as err:
        record_error(f"render {component}", f"invalid markup ({type(err).__name__}), builtin shown")
        return markup
    return out


def glyphs(defaults: dict) -> dict:
    """Builtin glyph table through the glyphs chain; bad entries are ignored."""
    out = dispatch("glyphs", {}, lambda e: dict(defaults))
    if not isinstance(out, dict):
        record_error("glyphs", "handler must return a dict, builtin used")
        return dict(defaults)
    merged = dict(defaults)
    for k, v in out.items():
        if k in defaults and isinstance(v, str) and v:
            merged[k] = v
        else:
            record_error("glyphs", f"ignored entry {k!r}")
    return merged


def handle_key(data: str) -> bool:
    """True if a mod consumed this raw terminal input."""
    match = lambda f, ctx: matches_key(ctx["data"], f["key"])
    return bool(dispatch("key", {"data": data}, lambda e: False, match=match, data=data))


def commands() -> dict:
    """{"/name": hint} for every active mod command; first registration's hint wins."""
    out = {}
    for f, _, mod in _handlers.get("command", ()):
        if mod not in _disabled:
            out.setdefault("/" + f["name"], str(f.get("hint", "")))
    return out


def slash_command_info() -> list[tuple[str, str]]:
    """Builtins + mod commands as (name, hint); a mod command shadowing a builtin is marked."""
    from micro_cc.utils import command_registry

    mine = commands()
    builtin = [(c["name"], c["hint"]) for c in command_registry.COMMANDS if c["name"] not in mine]
    names = {c["name"] for c in command_registry.COMMANDS}
    return builtin + [(n, ("(mod) " if n in names else "") + h) for n, h in mine.items()]


def match_command(query: str) -> tuple[str, str] | None:
    """(name without slash, arg) if query starts with a mod command."""
    lower = query.lower()
    for name in commands():
        if lower == name or lower.startswith(name + " "):
            return name[1:], query[len(name):].strip()
    return None


# --- legacy sweep ------------------------------------------------------------

def sweep_legacy() -> list[str]:
    """Move 0.2.103 seam files to ~/.micro-cc/legacy/; nothing is deleted."""
    base, moved = user_dir(), []
    for name in LEGACY:
        src = base / name
        if not src.exists():
            continue
        try:
            (base / "legacy").mkdir(exist_ok=True)
            dest = base / "legacy" / name
            if dest.exists():
                dest = dest.with_name(f"{name}.{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.move(str(src), str(dest))
            moved.append(name)
        except OSError as err:
            record_error(f"legacy {name}", f"{type(err).__name__}: {err}")
    return moved
