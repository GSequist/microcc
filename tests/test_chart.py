import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from micro_cc.tools.chart_tool_ import chart_
from micro_cc.tools.search_tool_ import TOOL_CATALOG
from micro_cc.tui_native.message_row_ import MessageRow
from micro_cc.utils import chart_ as draw_mod

PAL = ["#e07fb0", "#6cbcc8", "#8fa8d9", "#c9b36a"]
SPECS = {
    "line": {"labels": ["a", "b", "c", "d"], "series": [{"name": "s", "values": [1, 3, 2, 5]}]},
    "area": {"series": [{"name": "s", "values": [1, 3, 2, 5]}]},
    "scatter": {"series": [{"name": "s", "x": [1, 2, 3], "values": [3, 1, 2]}]},
    "bar": {"labels": ["a", "b"], "series": [{"name": "s", "values": [1, 3]}, {"name": "t", "values": [2, 2]}]},
    "hbar": {"labels": ["a", "b"], "series": [{"name": "s", "values": [1, 3]}]},
    "histogram": {"series": [{"name": "s", "values": [1, 2, 2, 3, 3, 3, 9]}]},
    "heatmap": {"labels": ["x", "y", "z"], "series": [{"name": "r1", "values": [1, 2, 3]}, {"name": "r2", "values": [3, 2, 1]}]},
    "share": {"labels": ["a", "b"], "series": [{"name": "s", "values": [1, 3]}]},
}


class ChartTests(unittest.TestCase):
    def test_every_kind_draws_within_width(self):
        for kind, extra in SPECS.items():
            spec = {"kind": kind, **extra}
            self.assertIsNone(draw_mod.validate(spec), kind)
            for width in (30, 80, 140):
                lines = draw_mod.draw(spec, width, PAL, "#101014")
                self.assertTrue(lines, kind)
                if width >= 60:
                    self.assertTrue(all(t.cell_len <= width for t in lines), (kind, width))

    def test_validation_errors(self):
        self.assertIn("kind", draw_mod.validate({"kind": "pie", "series": []}))
        self.assertIn("labels", draw_mod.validate({"kind": "bar", "series": [{"name": "s", "values": [1]}]}))
        self.assertIn(">= 0", draw_mod.validate({"kind": "bar", "labels": ["a"], "series": [{"name": "s", "values": [-1]}]}))
        self.assertIn("same length", draw_mod.validate({"kind": "scatter", "series": [{"name": "s", "x": [1], "values": [1, 2]}]}))

    def test_tool_returns_stats_only(self):
        out = chart_(kind="line", labels=["a", "b", "c"], series=[{"name": "s", "values": [1, 9, 4]}])
        self.assertIn("max 9 at b", out)
        self.assertTrue(chart_(kind="bar", series=[{"name": "s", "values": [1]}]).startswith("Error"))

    def test_registered_with_enum_schema(self):
        schema = TOOL_CATALOG["chart_"]["schema"]
        self.assertIn("histogram", str(schema))

    def test_row_renders_chart_and_falls_back_on_error(self):
        msg = {"type": "tool_call", "name": "chart_", "input": {"kind": "line", "series": [{"name": "s", "values": [1, 2, 3]}]},
               "result": "Chart (line) shown to the user.", "expanded": False}
        self.assertGreater(len(MessageRow(msg).render(80)), 6)
        msg = {**msg, "result": "Error: nope"}
        self.assertLess(len(MessageRow(msg).render(80)), 4)


if __name__ == "__main__":
    unittest.main()
