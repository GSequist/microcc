"""Terminal charts drawn with braille dots and block glyphs: line, area, scatter, bar, hbar, histogram, heatmap, share."""

import math

from rich.text import Text

KINDS = ("line", "area", "scatter", "bar", "hbar", "histogram", "heatmap", "share")
_BRAILLE = ((1, 2, 4, 64), (8, 16, 32, 128))  # bit for dot (dx, dy) inside a 2x4 cell
_V = " ▁▂▃▄▅▆▇█"
_H = " ▏▎▍▌▋▊▉█"
_MAX_POINTS = 200_000


def fmt(v: float) -> str:
    if v == int(v) and abs(v) < 1e15:
        return f"{int(v):,}"
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.4g}"


def validate(spec: dict) -> str | None:
    """Error string if `spec` can't be drawn, else None."""
    kind, series, labels = spec.get("kind"), spec.get("series"), spec.get("labels")
    if kind not in KINDS:
        return f"kind must be one of {', '.join(KINDS)}"
    if not isinstance(series, list) or not series:
        return "series must be a non-empty list of {name, values}"
    total = 0
    for i, s in enumerate(series):
        vals = s.get("values") if isinstance(s, dict) else None
        if not isinstance(s, dict) or not isinstance(s.get("name"), str) or not isinstance(vals, list) or not vals:
            return f"series[{i}] needs a string name and a non-empty values list"
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in vals):
            return f"series[{i}].values must be finite numbers"
        total += len(vals)
        if kind == "scatter" and len(s.get("x") or []) != len(vals):
            return f"series[{i}] needs x with the same length as values for scatter"
        if kind in ("bar", "hbar", "share") and min(vals) < 0:
            return f"{kind} values must be >= 0"
        if kind in ("bar", "hbar", "share", "heatmap", "line", "area") and labels is not None and len(labels) != len(vals):
            return f"labels ({len(labels)}) must match series[{i}].values ({len(vals)})"
        if kind in ("bar", "hbar", "share", "heatmap") and labels is None and kind != "heatmap":
            return f"{kind} needs labels (one per value)"
        if kind in ("bar", "hbar", "heatmap") and len(vals) != len(series[0]["values"]):
            return "all series must have the same number of values"
    if kind == "histogram" and len(series[0]["values"]) < 2:
        return "histogram needs at least 2 values"
    if total > _MAX_POINTS:
        return f"too many points ({total} > {_MAX_POINTS}); aggregate first"
    return None


def stats(s: dict, labels: list | None) -> dict:
    v = s["values"]
    i = v.index(max(v))
    return {"name": s["name"], "max": max(v), "at": labels[i] if labels and len(labels) == len(v) else None,
            "last": v[-1], "min": min(v), "mean": sum(v) / len(v), "n": len(v)}


def _nice(lo: float, hi: float, n: int = 4) -> tuple[float, float, float]:
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return math.floor(lo / step) * step, math.ceil(hi / step) * step, step


def _header(spec: dict, palette: list[str]) -> list[Text]:
    labels = spec.get("labels")
    rows = [stats(s, labels) for s in spec["series"]]
    if spec["kind"] == "histogram":
        rows = rows[:1]
    cols = {k: max(len(fmt(r[k])) for r in rows) for k in ("max", "last", "min", "mean")}
    nw = max(len(r["name"]) for r in rows)
    out = []
    for i, r in enumerate(rows):
        c = palette[i % len(palette)]
        t = Text.assemble((f"{'●' if spec['kind'] != 'histogram' else 'n'} ", c), (r["name"].ljust(nw), "bold"))
        t.append("  max ", "dim"); t.append(fmt(r["max"]).rjust(cols["max"]), c)
        if r["at"] is not None:
            t.append(f" at {r['at']}", "dim")
        for key in ("last", "min", "mean"):
            t.append(f"  {key} ", "dim"); t.append(fmt(r[key]).rjust(cols[key]), c)
        t.no_wrap, t.overflow = True, "ellipsis"
        out.append(t)
    return out


def _xlabels(items: list[tuple[int, str]], pw: int) -> str:
    """One axis row with each label centred on its column; overlapping labels are skipped."""
    row, end = [" "] * pw, -2
    for col, text in sorted(items):
        text = text[:pw]
        start = max(0, min(col - len(text) // 2, pw - len(text)))
        if start <= end + 1:
            continue
        row[start:start + len(text)] = text
        end = start + len(text) - 1
    return "".join(row)


def _frame(cells: list[Text], ticks: dict[int, str], xlabels: list[tuple[int, str]], pw: int) -> list[Text]:
    """Prefix plot rows with y-axis labels, add the x axis and its labels."""
    lw = max((len(t) for t in ticks.values()), default=0)
    out = []
    for i, row in enumerate(cells):
        out.append(Text.assemble((ticks.get(i, "").rjust(lw), "dim"), (" │", "dim")) + row)
    out.append(Text(" " * lw + " └" + "─" * pw, style="dim"))
    if xlabels:
        out.append(Text(" " * (lw + 2) + _xlabels(xlabels, pw), style="dim"))
    return out


def _xy(spec: dict, W: int, H: int, palette: list[str]) -> list[Text]:
    kind, labels = spec["kind"], spec.get("labels")
    series = spec["series"]
    groups = [[s] for s in series] if spec.get("split") else [series]
    out = []
    for g in groups:
        vals = [v for s in g for v in s["values"]]
        lo, hi = min(vals), max(vals)
        if kind == "area" or (lo >= 0 and lo < hi * 0.5):
            lo = 0
        ylo, yhi, step = _nice(lo, hi)
        ticks = {}
        t = ylo
        while t <= yhi + step / 2:
            ticks[min(int((yhi - t) / (yhi - ylo) * (4 * H - 1)) // 4, H - 1)] = fmt(t)
            t += step
        pw = W - max(len(v) for v in ticks.values()) - 2
        bits, owner = [[0] * pw for _ in range(H)], [[0] * pw for _ in range(H)]

        def dot(c, r, k):
            if 0 <= c < 2 * pw and 0 <= r < 4 * H:
                bits[r // 4][c // 2] |= _BRAILLE[c % 2][r % 4]
                owner[r // 4][c // 2] = k

        xs = [x for s in g for x in s.get("x", [])]
        xlo, xhi = (min(xs), max(xs)) if xs else (0, 1)
        for s in g:
            k = series.index(s)
            top = {}
            n = len(s["values"])
            pts = [((round(((s["x"][i] - xlo) / ((xhi - xlo) or 1)) * (2 * pw - 1)) if kind == "scatter"
                     else round(i * (2 * pw - 1) / max(n - 1, 1))),
                    round((yhi - v) / (yhi - ylo) * (4 * H - 1))) for i, v in enumerate(s["values"])]
            if kind == "scatter":
                for c, r in pts:
                    dot(c, r, k)
                continue
            for (c0, r0), (c1, r1) in zip(pts, pts[1:] or pts):  # Bresenham between consecutive points
                dx, dy, sx, sy = abs(c1 - c0), -abs(r1 - r0), 1 if c0 < c1 else -1, 1 if r0 < r1 else -1
                err, c, r = dx + dy, c0, r0
                while True:
                    dot(c, r, k)
                    top[c] = min(top.get(c, r), r)
                    if c == c1 and r == r1:
                        break
                    e2 = 2 * err
                    if e2 >= dy:
                        err, c = err + dy, c + sx
                    if e2 <= dx:
                        err, r = err + dx, r + sy
            if kind == "area":
                for c, r0 in top.items():
                    for r in range(r0 + 1, 4 * H):
                        if c % 2 == 0 and r % 2 == 0:
                            dot(c, r, k)
        cells = []
        for i in range(H):
            row = Text()
            for j in range(pw):
                b = bits[i][j]
                if b:
                    row.append(chr(0x2800 + b), palette[owner[i][j] % len(palette)])
                else:
                    row.append("┈" if i in ticks else " ", "dim")
            cells.append(row)
        if kind == "scatter":
            xl = [(round(j / 4 * (pw - 1)), fmt(xlo + (xhi - xlo) * j / 4)) for j in range(5)]
        else:
            n = len(g[0]["values"])
            pos = labels if labels and len(labels) == n else [str(i) for i in range(n)]
            m = min(n, max(2, pw // (max(len(p) for p in pos[:200]) + 3)))
            xl = [(round(round(j * (n - 1) / max(m - 1, 1)) * (pw - 1) / max(n - 1, 1)), pos[round(j * (n - 1) / max(m - 1, 1))]) for j in range(m)]
        out += _frame(cells, ticks, xl, pw)
    return out


def _vbars(cats: list[str], series: list[list[float]], palette: list[str], W: int, H: int) -> list[Text]:
    n, k = len(cats), len(series)
    n = min(n, max(1, (W - 6) // 2))
    yhi = _nice(0, max(max(v[:n]) for v in series), 5)[1]
    ticks = {}
    for t in (0, yhi / 2, yhi):
        ticks[min(H - round(t / yhi * H), H - 1)] = fmt(t)
    pw = W - max(len(v) for v in ticks.values()) - 2
    cw = pw // n
    bw = max(1, (cw - 1) // k)
    off = max(0, (cw - bw * k) // 2)
    cells = []
    for r in range(H):
        row = Text()
        for c in range(n):
            row.append(" " * off)
            for j in range(k):
                v = series[j][c]
                e = round(v / yhi * H * 8) or (1 if v > 0 else 0)
                row.append(_V[max(0, min(8, e - (H - 1 - r) * 8))] * bw, palette[j % len(palette)])
            row.append(" " * (cw - off - bw * k))
        row.append(" " * (pw - cw * n))
        cells.append(row)
    return _frame(cells, ticks, [(c * cw + cw // 2, cats[c]) for c in range(n)], pw)


def _hbar(spec: dict, W: int, palette: list[str]) -> list[Text]:
    series, labels = spec["series"], [str(x) for x in spec["labels"]]
    cap = 40
    lw = min(max(len(x) for x in labels), W // 3)
    vw = max(len(fmt(v)) for s in series for v in s["values"])
    bw = max(4, W - lw - vw - 2)
    mx = max(max(s["values"]) for s in series) or 1
    out = []
    if len(series) > 1:
        out = [Text.assemble(*[(f"● {s['name']}  ", palette[i % len(palette)]) for i, s in enumerate(series)])]
    for i, lab in enumerate(labels[:cap]):
        for j, s in enumerate(series):
            e = round(s["values"][i] / mx * bw * 8)
            bar = "█" * (e // 8) + (_H[e % 8] if e % 8 else "")
            out.append(Text.assemble((lab[:lw].ljust(lw) if j == 0 else " " * lw, "dim"), " ",
                                     (bar, palette[j % len(palette)]), " ", (fmt(s["values"][i]), "dim")))
    if len(labels) > cap:
        out.append(Text(f"… +{len(labels) - cap} more", style="dim italic"))
    return out


def _heatmap(spec: dict, W: int, palette: list[str], bg: str) -> list[Text]:
    series, labels = spec["series"], spec.get("labels")
    rw = min(16, max(len(s["name"]) for s in series))
    avail = W - rw - 1
    n = len(series[0]["values"])
    m = min(n, avail)
    rows = [[sum(s["values"][i * n // m:(i + 1) * n // m]) / len(s["values"][i * n // m:(i + 1) * n // m]) for i in range(m)] for s in series]
    cw = max(1, min(4, avail // m))
    lo, hi = min(min(r) for r in rows), max(max(r) for r in rows)
    a, b = [int(palette[0][i:i + 2], 16) for i in (1, 3, 5)], [int(bg[i:i + 2], 16) for i in (1, 3, 5)]

    def shade(v: float) -> str:
        t = (v - lo) / ((hi - lo) or 1)
        return "#" + "".join(f"{round(y + t * (x - y)):02x}" for x, y in zip(a, b))

    out = [Text.assemble((s["name"][:rw].ljust(rw), "dim"), " ", *[("█" * cw, shade(v)) for v in r]) for s, r in zip(series, rows)]
    pos = labels if labels else [str(i) for i in range(n)]
    out.append(Text(" " * (rw + 1) + _xlabels([(i * cw + cw // 2, str(pos[i * n // m])) for i in range(m)], m * cw), style="dim"))
    out.append(Text.assemble((f"{fmt(lo)} ", "dim"), *[("█", shade(lo + (hi - lo) * i / 7)) for i in range(8)], (f" {fmt(hi)}", "dim")))
    return out


def _share(spec: dict, W: int, palette: list[str]) -> list[Text]:
    vals, labels = spec["series"][0]["values"], [str(x) for x in spec["labels"]]
    total = sum(vals) or 1
    widths = [int(v / total * W) for v in vals]
    for i in sorted(range(len(vals)), key=lambda i: vals[i] / total * W - widths[i], reverse=True)[:W - sum(widths)]:
        widths[i] += 1
    bar = Text.assemble(*[("█" * w, palette[i % len(palette)]) for i, w in enumerate(widths)])
    nw, vw = max(map(len, labels)), max(len(fmt(v)) for v in vals)
    return [bar] + [Text.assemble(("■ ", palette[i % len(palette)]), labels[i].ljust(nw), "  ", (fmt(v).rjust(vw), "dim"),
                                  (f"  {v / total:5.1%}", "dim")) for i, v in enumerate(vals)]


def draw(spec: dict, width: int, palette: list[str], bg: str) -> list[Text]:
    """Chart lines for an already-validated spec; palette/bg are '#rrggbb' strings."""
    kind, W = spec["kind"], max(24, width)
    H = max(4, min(30, int(spec.get("height") or 10)))
    out = [Text(spec["title"], style="bold")] if spec.get("title") else []
    if kind not in ("hbar", "heatmap", "share"):
        out += _header(spec, palette)
    if kind in ("line", "area", "scatter"):
        out += _xy(spec, W, H, palette)
    elif kind == "bar":
        out += _vbars([str(x) for x in spec["labels"]], [s["values"] for s in spec["series"]], palette, W, H)
    elif kind == "histogram":
        v = spec["series"][0]["values"]
        nb = spec.get("bins") or max(5, min(40, math.ceil(math.sqrt(len(v)))))
        lo, hi = min(v), max(v)
        w = (hi - lo) / nb or 1
        counts = [0] * nb
        for x in v:
            counts[min(int((x - lo) / w), nb - 1)] += 1
        out += _vbars([fmt(lo + i * w) for i in range(nb)], [counts], palette, W, H)
    elif kind == "hbar":
        out += _hbar(spec, W, palette)
    elif kind == "heatmap":
        out += _heatmap(spec, W, palette, bg)
    else:
        out += _share(spec, W, palette)
    return out
