"""Layout lab: the real VStack / HSplit / ScrollView / TuiAltScreen drawing coloured boxes, with an x-ray pane.

    source env/bin/activate
    python tests/manual/layout_lab_.py

The tree is the one start_live_tui_.py builds: banner, conversation (scrolling) beside a pane, bottom bar.
Every layer is a component whose render(width) returns a list of strings; the stacks only decide how many
rows each list gets. Change an entry's numbers and watch the frame redistribute.

tab/shift+tab select  + / -  rows (or messages)  g/G grow  s/S shrink  b basis auto/6  space hide/show
p pane side  x pane on/off  j/k scroll  q quit
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

os.environ["HOME"] = tempfile.mkdtemp(prefix="microcc-lab-")  # message_row_ touches settings at import
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from micro_cc.tui_native.alt_screen_ import ENTER_ALT_SCREEN, EXIT_ALT_SCREEN, TuiAltScreen  # noqa: E402
from micro_cc.tui_native.keys_ import parse_key  # noqa: E402
from micro_cc.tui_native.message_row_ import HRule  # noqa: E402
from micro_cc.tui_native.scroll_view_ import ScrollView  # noqa: E402
from micro_cc.tui_native.stack_ import HSplit, VStack  # noqa: E402
from micro_cc.tui_native.stdin_buffer_ import Paste, StdinBuffer  # noqa: E402
from micro_cc.tui_native.terminal_ import ProcessTerminal  # noqa: E402

RESET, DIM, BOLD, INV = "\x1b[0m", "\x1b[2m", "\x1b[1m", "\x1b[7m"


class Box:
    """A leaf layer: draws `rows` lines of its own colour; hidden = renders [] like a closed panel."""

    def __init__(self, name: str, rows: int, rgb: tuple):
        self.name, self.rows, self.rgb, self.shown = name, rows, rgb, True

    def render(self, width: int) -> list[str]:
        if not self.shown:
            return []
        bg = "\x1b[48;2;%d;%d;%dm" % self.rgb
        out = []
        for i in range(self.rows):
            if i == 0:
                text = f"┌ {self.name}   width {width}   wants {self.rows} row{'s' if self.rows != 1 else ''}"
            elif i == self.rows - 1:
                text = "└ end"  # clipped away when the stack gives this layer fewer rows
            else:
                text = f"│ row {i + 1}/{self.rows}"
            out.append(f"{bg}{text[:width].ljust(width)}{RESET}")
        return out

    def invalidate(self) -> None:
        pass


NAMES: dict[int, str] = {}


def add(stack: VStack, name: str, comp, **sizing):
    NAMES[id(comp)] = name
    stack.add(comp, **sizing)
    return comp


def build() -> dict:
    messages = VStack()
    for i in range(14):
        add(messages, f"message {i + 1}", Box(f"message {i + 1}", 2 + i % 3, (24, 36 + i % 4 * 6, 60 + i % 3 * 8)))
    scroll = ScrollView(messages, follow="end", primary=True)
    split = HSplit(scroll)

    bottom = VStack()
    add(bottom, "working", Box("working", 1, (60, 44, 20)))
    add(bottom, "live_panel", Box("live_panel (closed panel = 0 rows)", 3, (70, 40, 70)))
    add(bottom, "top_rule", HRule())
    add(bottom, "prompt", Box("prompt", 3, (28, 28, 28)))
    add(bottom, "bottom_rule", HRule())
    add(bottom, "statusbar", Box("statusbar", 1, (30, 50, 40)))
    add(bottom, "hintbar", Box("hintbar", 1, (50, 50, 30)))
    add(bottom, "subagents", Box("subagents", 2, (50, 30, 40)))
    add(bottom, "bgprocs", Box("bgprocs", 1, (30, 40, 56)))
    bottom.entries[1].component.shown = False

    root = VStack()
    add(root, "banner", Box("banner · tab select · + - rows · g G grow · s S shrink · b basis · space hide · p pane side · x pane · j k scroll · q quit", 2, (70, 30, 50)), shrink=0)
    add(root, "main_split (conversation | pane)", split, grow=1)
    add(root, "bottom_bar (VStack)", bottom, shrink=0)
    return {"root": root, "split": split, "scroll": scroll, "messages": messages, "bottom": bottom}


# --- x-ray -------------------------------------------------------------------

def rows_of(lab: dict) -> list:
    """Selectable entries: root's, then the bottom bar's. (stack, index) pairs."""
    return [(lab["root"], i) for i in range(len(lab["root"].entries))] + [(lab["bottom"], i) for i in range(len(lab["bottom"].entries))]


def wants(comp, width: int) -> int:
    try:
        return len(comp.render(width))
    except Exception:
        return 0


def table(lab: dict, stack: VStack, title: str, width: int, height, sel: tuple, pane_w: int) -> list[str]:
    total = sum(wants(e.component, width) for e in stack.entries)
    note = f"Σ wants {total}, have {height}" + (" → shrink" if total > height else " → grow") if height else f"Σ wants {total}, no height: sizes = wants"
    out = [f"{BOLD}{title}{RESET} {DIM}{note}{RESET}", f"{DIM}  {'layer':<17}{'wants':>5}{'basis':>6}{'grow':>5}{'shr':>4}{'got':>5}{RESET}"]
    for i, e in enumerate(stack.entries):
        name = NAMES.get(id(e.component), "?").split(" (")[0].split(" · ")[0][:17]
        got = stack.last_sizes[i] if i < len(stack.last_sizes) else 0
        line = f"{'▶' if sel == (stack, i) else ' '} {name:<17}{wants(e.component, width):>5}{str(e.basis):>6}{e.grow:>5}{e.shrink:>4}{got:>5}"
        out.append(f"{INV}{line}{RESET}" if sel == (stack, i) else line)
    return out


def xray(lab: dict, state: dict):
    def draw(w: int, h: int) -> list[str]:
        tui, root, scroll = state["tui"], lab["root"], lab["scroll"]
        cols, rows = tui._terminal_size()
        sel = rows_of(lab)[state["sel"]]
        lines = [
            f"{BOLD}HOW A FRAME IS BUILT{RESET}",
            f"{DIM}1 render   every child.render(width) → list[str]{RESET}",
            f"{DIM}2 size     basis, grow, shrink choose each row count{RESET}",
            f"{DIM}3 compose  pad or clip each list, join them{RESET}",
            f"{DIM}4 paint    write only the rows that changed{RESET}",
            "",
            f"terminal {cols}x{rows}   frame {state['frame']}   painted {state['painted']}/{rows} rows",
            "",
            *table(lab, root, "root VStack", cols, rows, sel, w),
            "",
            *table(lab, lab["bottom"], "bottom_bar VStack", cols, None, sel, w),
            "",
            f"{BOLD}ScrollView{RESET} content {scroll.content_height}  view {scroll.viewport_height}  top {scroll.scroll_top}"
            + (" ⤓" if scroll.is_following_end else ""),
            f"{DIM}HSplit hands it the rows left after the pane{RESET}",
        ]
        return lines[:h]
    return draw


# --- keys --------------------------------------------------------------------

def on_key(lab: dict, state: dict, data: str) -> None:
    k = parse_key(data) or data
    k = {"shift+g": "G", "shift+s": "S"}.get(k, k)
    pairs = rows_of(lab)
    stack, i = pairs[state["sel"]]
    entry, comp = stack.entries[i], stack.entries[i].component
    if k == "q" or k == "ctrl+c":
        state["running"] = False
    elif k in ("tab", "shift+tab"):
        state["sel"] = (state["sel"] + (1 if k == "tab" else -1)) % len(pairs)
    elif k in ("+", "=", "-"):
        step = -1 if k == "-" else 1
        if isinstance(comp, Box):
            comp.rows = max(1, comp.rows + step)
        elif comp is lab["split"]:
            n = len(lab["messages"].entries)
            if step > 0:
                add(lab["messages"], f"message {n + 1}", Box(f"message {n + 1}", 2 + n % 3, (24, 36 + n % 4 * 6, 60 + n % 3 * 8)))
            elif n > 1:
                lab["messages"].entries.pop()
    elif k in ("g", "G"):
        entry.grow = max(0, entry.grow + (1 if k == "g" else -1))
    elif k in ("s", "S"):
        entry.shrink = max(0, entry.shrink + (1 if k == "s" else -1))
    elif k == "b":
        entry.basis = 6 if entry.basis == "auto" else "auto"
    elif k == "space":
        if isinstance(comp, Box):
            comp.shown = not comp.shown
    elif k == "x":
        split = lab["split"]
        split.pane = None if split.pane else state["draw"]
    elif k == "p":
        split = lab["split"]
        split.side = {"right": "bottom", "bottom": "top", "top": "right"}[split.side]
        split.pane_width = 56 if split.side == "right" else 14
    elif k in ("j", "k", "down", "up"):
        lab["scroll"].scroll_by(1 if k in ("j", "down") else -1)
    state["dirty"] = True


async def amain() -> None:
    lab = build()
    tui = TuiAltScreen(lab["root"])
    tui.set_primary_scroll_view(lab["scroll"])
    state = {"tui": tui, "sel": 1, "frame": 0, "painted": 0, "dirty": True, "running": True}
    state["draw"] = xray(lab, state)
    lab["split"].pane, lab["split"].pane_width = state["draw"], 56

    terminal, buf = ProcessTerminal(), StdinBuffer()
    loop = asyncio.get_running_loop()

    def on_stdin() -> None:
        try:
            data = os.read(sys.stdin.fileno(), 4096)
        except OSError:
            return
        buf.feed(data)
        for item in buf.complete_sequences():
            if not isinstance(item, Paste):
                terminal.feed_stdin(item)

    def on_resize(*_a) -> None:
        state["dirty"] = True

    sys.stdout.write(ENTER_ALT_SCREEN)
    sys.stdout.flush()
    terminal.start(lambda d: on_key(lab, state, d), on_resize)
    loop.add_reader(sys.stdin.fileno(), on_stdin)
    try:
        while state["running"]:
            if state["dirty"]:
                state["dirty"] = False
                before = list(tui.previous_screen)
                tui.do_render()
                after = tui.previous_screen
                state["frame"] += 1
                state["painted"] = len(after) if len(before) != len(after) else sum(a != b for a, b in zip(before, after))
            await asyncio.sleep(0.05)
    finally:
        loop.remove_reader(sys.stdin.fileno())
        terminal.stop()
        sys.stdout.write(EXIT_ALT_SCREEN)
        sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(amain())
