"""Animated banner: typed + shimmering tagline and a seasonal particle layer over the (mod-hooked) banner."""

import math
import random
import time
from typing import TypedDict

from rich.text import Span, Text

from micro_cc import mods_
from micro_cc.tui_native.message_row_ import _render_to_lines
from micro_cc.tui_native.theme_ import _mix
from micro_cc.utils import settings_store_, theme_store_

FRAME_S = 0.1  # fx tick; the render loop only repaints the rows that changed
TAGLINE = "Knowledge work is, by extension, a coding problem."
TYPE_S = 0.035  # per-char typing speed of the tagline intro
BAND = 6  # shimmer band half-width, chars
PAUSE = 50  # ticks of calm between shimmer sweeps


class Season(TypedDict):
    shapes: list[str]  # each particle cycles through one shape's chars (a tumbling leaf)
    colors: list[str]  # "" = derived from theme fg/bg
    count: int
    fall: tuple[float, float]  # rows per tick, min/max
    sway: float  # horizontal swing amplitude, cols
    wind: float  # constant drift, cols per tick
    twinkle: bool  # fireflies: blink instead of a steady glyph


class Particle(TypedDict):
    x0: float
    y: float
    vy: float
    sway: float
    freq: float
    phase: float
    shape: str
    color: str


SEASONS: dict[str, Season] = {
    "winter": {"shapes": ["*", "·", "∗", "·", "+"], "colors": [""], "count": 26,
               "fall": (0.10, 0.28), "sway": 1.0, "wind": 0.02, "twinkle": False},
    "spring": {"shapes": ["✿", "❀", "·", "·"], "colors": ["#e8a3c0", "#f2c6d8", "#c99bd9", "#9bc0a4"], "count": 12,
               "fall": (0.05, 0.12), "sway": 2.0, "wind": 0.05, "twinkle": False},
    "summer": {"shapes": ["·", "•"], "colors": ["#f2d16b", "#e8c547", "#fff1a8"], "count": 10,
               "fall": (-0.03, 0.03), "sway": 3.0, "wind": 0.0, "twinkle": True},
    "autumn": {"shapes": ["❦❧", "❧❦", ",'`'", "❦"], "colors": ["#d9822b", "#c4502a", "#e0a33a", "#a0522d", "#cf6f3a"],
               "count": 14, "fall": (0.08, 0.20), "sway": 2.5, "wind": 0.08, "twinkle": False},
}
_MONTH_SEASON = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
                 6: "summer", 7: "summer", 8: "summer"}


def pick_season(setting: str | None) -> str | None:
    """settings `banner_season`: "auto" (by month), "off", or a season name."""
    if setting == "off":
        return None
    if setting in SEASONS:
        return setting
    return _MONTH_SEASON.get(time.localtime().tm_mon, "autumn")


def _spawn(s: Season, w: int, h: int, y: float | None = None) -> Particle:
    color = random.choice(s["colors"]) or _mix(theme_store_.get("bg"), theme_store_.get("fg"), random.uniform(0.45, 0.9))
    return {"x0": random.uniform(0, w), "y": random.uniform(-2, 0) if y is None else y,
            "vy": random.uniform(*s["fall"]), "sway": random.uniform(0.3, 1.0) * s["sway"],
            "freq": random.uniform(0.4, 1.2), "phase": random.uniform(0, 2 * math.pi),
            "shape": random.choice(s["shapes"]), "color": color}


class AnimatedBanner:
    """Drop-in for RichStatic(name="banner"); a mod hooking render(component="banner") owns it and turns fx off."""

    def __init__(self, content: str, hue: str):
        self._content = content
        self._hue = hue
        self._season = pick_season(settings_store_.get_setting("banner_season"))
        self._particles: list[Particle] = []
        self._start = time.monotonic()
        self._tick = 0
        self._size = (0, 0)  # (width, rows) seen at the last render; particles live in this box
        self._base: list[Text] = []
        self._base_key: tuple[int, int] | None = None
        self._hooked = False
        self._key: tuple[int, int, int] | None = None
        self._lines: list[str] = []

    def update(self, content: str, hue: str | None = None) -> None:
        self._content = content
        self._hue = hue or self._hue
        self._particles = []  # respawn with new theme colors
        self.invalidate()

    def invalidate(self) -> None:
        self._base_key = None
        self._key = None

    def step(self) -> bool:
        """Advance one fx tick; True if the banner looks different now."""
        if mods_.has("render", component="banner"):
            return False  # a mod owns the banner: no fx, no repaints
        self._tick += 1
        w, h = self._size
        s = SEASONS.get(self._season) if self._season else None
        if s and w and h:
            if not self._particles:
                self._particles = [_spawn(s, w, h, random.uniform(0, h)) for _ in range(s["count"])]
            for i, p in enumerate(self._particles):
                p["y"] += p["vy"]
                p["x0"] = (p["x0"] + s["wind"]) % w
                if s["twinkle"] and not 0 <= p["y"] < h:
                    p["vy"] = -p["vy"]
                elif p["y"] >= h:
                    self._particles[i] = _spawn(s, w, h)
            return True
        return self._tagline_moving()

    def _tagline_moving(self) -> bool:
        if time.monotonic() - self._start < len(TAGLINE) * TYPE_S + 2.5:
            return True  # typing + cursor blink
        return self._tick % (len(TAGLINE) + 2 * BAND + PAUSE) < len(TAGLINE) + 2 * BAND + 1

    def render(self, width: int) -> list[str]:
        gen = mods_.generation()
        if self._key == (width, gen, self._tick):
            return self._lines
        if self._base_key != (width, gen):
            hooked = mods_.has("render", component="banner")
            markup = mods_.render_markup("banner", self._content, width) if hooked else self._content
            self._base = Text.from_markup(markup).split("\n", allow_blank=True) if markup else []
            self._base_key = (width, gen)
            self._hooked = hooked
        if not self._base:
            self._lines = []
        elif self._hooked:
            self._lines = _render_to_lines(Text("\n").join(self._base), width)  # mod output, untouched
        else:
            if self._size != (width, len(self._base)):
                self._size = (width, len(self._base))
                self._particles = []
            rows = self._paint(width)
            self._lines = _render_to_lines(Text("\n").join(rows), width)
        self._key = (width, gen, self._tick)
        return self._lines

    def _paint(self, width: int) -> list[Text]:
        """Copy base lines, pad to width, then overlay tagline fx and particles onto blank cells."""
        cells: dict[tuple[int, int], tuple[str, str]] = {}
        t = self._tick * FRAME_S
        twinkle = bool(self._season and SEASONS[self._season]["twinkle"])
        for p in self._particles:
            x, y = int(p["x0"] + p["sway"] * math.sin(t * p["freq"] + p["phase"])) % max(1, width - 1), int(p["y"])
            glyph = p["shape"][int(t * 1.5 + p["phase"]) % len(p["shape"])]
            color = p["color"]
            if twinkle:
                glow = math.sin(t * 2.2 + p["phase"])
                if glow <= 0:
                    continue
                color = _mix(theme_store_.get("bg"), color, 0.3 + 0.7 * glow)
            cells[(y, x)] = (glyph, color)

        out = []
        for row, line in enumerate(self._base):
            plain = line.plain
            if len(plain) < width - 1:
                plain += " " * (width - 1 - len(plain))
            chars = list(plain)
            spans = list(line.spans)
            at = plain.find(TAGLINE)
            if at >= 0:
                self._tagline(chars, spans, at)
            for (y, x), (glyph, color) in cells.items():
                # Only blank cells with blank neighbours: particles pass behind text, never glue onto words.
                if y == row and 0 < x < len(chars) - 1 and chars[x - 1] == chars[x] == chars[x + 1] == " ":
                    chars[x] = glyph
                    spans.append(Span(x, x + 1, color))
            out.append(Text("".join(chars), style=line.style, spans=spans))
        return out

    def _tagline(self, chars: list[str], spans: list[Span], at: int) -> None:
        """Typewriter intro with a blinking cursor, then a hue band sweeps across every so often."""
        n = len(TAGLINE)
        elapsed = time.monotonic() - self._start
        typed = min(n, int(elapsed / TYPE_S))
        for i in range(typed, n):
            chars[at + i] = " "
        cur = at + typed
        if elapsed < n * TYPE_S + 2.5 and cur < len(chars) and chars[cur] == " " and (typed < n or self._tick % 6 < 3):
            chars[cur] = "▍"
            spans.append(Span(cur, cur + 1, self._hue))
        if typed < n:
            return
        base = theme_store_.get("banner_dim")
        pos = self._tick % (n + 2 * BAND + PAUSE) - BAND
        for i in range(n):
            k = max(0.0, 1 - abs(i - pos) / BAND)
            if k > 0:
                spans.append(Span(at + i, at + i + 1, _mix(base, self._hue, k)))
