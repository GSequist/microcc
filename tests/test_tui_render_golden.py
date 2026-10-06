"""Golden render of MessageRow across msg types; regenerate with GOLDEN_UPDATE=1."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="mcc_golden_")
os.environ["HOME"] = _HOME
os.environ.pop("MICRO_CC_POSTGRES_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from micro_cc.tui_native.message_row_ import MessageRow  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "tui_render_golden.json"
_LONG = "\n".join(f"line {i} of a long result" for i in range(120))
_BIG = "x" * 6000

CASES = {
    "user": {"type": "user", "content": "hello there\nsecond line"},
    "user_queued": {"type": "user_queued", "content": "queued msg"},
    "text": {"type": "text", "content": "# Title\n\nsome **bold** and `code`\n\n- a\n- b\n\n```py\nprint(1)\n```"},
    "thinking": {"type": "thinking", "content": "pondering *hard*"},
    "tool_pending": {"type": "tool_call", "name": "read_", "input": {"file_path": "/a"}, "result": None},
    "tool_collapsed": {"type": "tool_call", "name": "read_", "input": {"file_path": "/a"}, "result": "first line\nsecond"},
    "tool_collapsed_short": {"type": "tool_call", "name": "read_", "input": {}, "result": "ok"},
    "tool_expanded": {"type": "tool_call", "name": "read_", "input": {"file_path": "/a"}, "result": "first line\nsecond", "expanded": True},
    "tool_expanded_long": {"type": "tool_call", "name": "grep_", "input": {"pattern": "x"}, "result": _LONG, "expanded": True},
    "tool_expanded_big": {"type": "tool_call", "name": "grep_", "input": {"pattern": "x"}, "result": _BIG, "expanded": True},
    "bash_collapsed": {"type": "tool_call", "name": "bash_", "input": {"command": "ls"}, "result": "\x1b[31mred\x1b[0m out\nmore"},
    "bash_expanded": {"type": "tool_call", "name": "bash_", "input": {"command": "ls"}, "result": "\x1b[31mred\x1b[0m out\n\x1b[1mbold\x1b[0m", "expanded": True},
    "edit_collapsed": {"type": "tool_call", "name": "edit_", "input": {"file_path": "/f.py", "old_string": "a\nb", "new_string": "a\nc"}, "result": "done"},
    "edit_expanded": {"type": "tool_call", "name": "edit_", "input": {"file_path": "/f.py", "old_string": "a\nb\nc", "new_string": "a\nB\nc\nd"}, "result": "done", "expanded": True},
    "edit_expanded_long": {"type": "tool_call", "name": "edit_", "input": {"file_path": "/f.py", "old_string": _LONG, "new_string": _LONG.replace("line", "LINE")}, "result": "done", "expanded": True},
    "error": {"type": "error", "content": "something broke"},
    "approval_bash": {"type": "approval", "name": "bash_", "input": {"command": "rm -rf x\nls"}},
    "approval_bash_exp": {"type": "approval", "name": "bash_", "input": {"command": "rm -rf x\nls"}, "expanded": True},
    "approval_edit": {"type": "approval", "name": "edit_", "input": {"file_path": "/f.py", "old_string": "a", "new_string": "b"}},
    "approval_edit_exp": {"type": "approval", "name": "edit_", "input": {"file_path": "/f.py", "old_string": "a", "new_string": "b"}, "expanded": True},
    "approval_write": {"type": "approval", "name": "write_", "input": {"file_path": "/f.py", "content": "hi"}},
    "approval_write_exp": {"type": "approval", "name": "write_", "input": {"file_path": "/f.py", "content": "y" * 700}, "expanded": True},
    "approval_other": {"type": "approval", "name": "web_", "input": {"url": "http://x"}},
    "approval_other_exp": {"type": "approval", "name": "web_", "input": {"url": "http://x"}, "expanded": True},
    "unknown": {"type": "mystery"},
}


def render_all() -> dict:
    out = {}
    for key, msg in CASES.items():
        out[key] = {str(w): MessageRow(dict(msg)).render(w) for w in (80, 40)}
    return out


class TestGolden(unittest.TestCase):
    def test_golden(self):
        got = render_all()
        if os.environ.get("GOLDEN_UPDATE"):
            FIXTURE.write_text(json.dumps(got, indent=1, ensure_ascii=False))
        want = json.loads(FIXTURE.read_text())
        for key in CASES:
            self.assertEqual(want[key], got[key], key)


if __name__ == "__main__":
    unittest.main()
