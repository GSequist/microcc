from typing import List, Literal, NotRequired, Optional, TypedDict

from micro_cc.utils import chart_ as charts


class ChartSeries(TypedDict):
    name: str
    values: List[float]
    x: NotRequired[List[float]]


def chart_(
    kind: Literal["line", "area", "scatter", "bar", "hbar", "histogram", "heatmap", "share"],
    series: List[ChartSeries],
    title: str = "",
    labels: Optional[List[str]] = None,
    split: bool = False,
    bins: Optional[int] = None,
    height: int = 10,
) -> str:
    """Draw a chart for the user right in the terminal (braille/block glyphs, theme colors). Use it when numbers over time or across categories read better as a picture. The user sees the chart; you only get summary stats back, so don't restate the data. Aggregate first: thousands of points are fine, raw millions are not.

    Kinds and how to pass data:
      line/area: values per series over labels (x axis names, one per value; optional).
      scatter: each series has x and values of equal length.
      bar: grouped vertical bars; labels = categories, one series per group member, values >= 0.
      hbar: like bar but horizontal; best for long category names and rankings.
      histogram: raw numbers in series[0].values; bins optional.
      heatmap: one series per row, values across columns, labels name the columns.
      share: parts of a whole; labels + one series, values >= 0.

    Args:
        kind: chart type, see above.
        series: list of {name, values, x?}; all series of bar/hbar/heatmap must be equally long.
        title: short heading.
        labels: category or x-axis labels, one per value.
        split: line/area/scatter only: one panel per series with its own scale instead of sharing one axis.
        bins: histogram bin count (default ~sqrt(n)).
        height: plot rows, 4-30.
    """
    spec = {"kind": kind, "series": series, "title": title, "labels": labels, "split": split, "bins": bins, "height": height}
    if err := charts.validate(spec):
        return f"Error: {err}"
    lines = []
    for s in series[:1] if kind in ("histogram", "share") else series:
        st = charts.stats(s, labels)
        at = f" at {st['at']}" if st["at"] is not None else ""
        lines.append(
            f"{st['name']}: n={st['n']} max {charts.fmt(st['max'])}{at}, last {charts.fmt(st['last'])}, "
            f"min {charts.fmt(st['min'])}, mean {charts.fmt(st['mean'])}"
        )
    return f"Chart ({kind}) shown to the user.\n" + "\n".join(lines)
