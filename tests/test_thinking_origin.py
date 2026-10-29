from micro_cc.models.client import _origin, drop_foreign_thinking
from micro_cc.utils import system_prompt_
from micro_cc.utils.msg_normalize_ import normalize_message, reconstruct_message

SYS = [{"role": "system", "content": "prompt v1"}]
ANTH = _origin("anthropic", "claude-opus-4-7", SYS)


def _turn(*thinking):
    return [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [*thinking, {"type": "tool_use", "id": "t1", "name": "bash_", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    ]


def _thinking(msgs):
    return [b for b in msgs[1]["content"] if b["type"] == "thinking"]


def _block(origin):
    b = {"type": "thinking", "thinking": "x", "signature": "sig", "id": ""}
    return {**b, "origin": origin} if origin else b


def test_same_origin_kept():
    b = _block(ANTH)
    assert _thinking(drop_foreign_thinking(_turn(b), ANTH)) == [b]


def test_other_model_dropped_tool_use_kept():
    out = drop_foreign_thinking(_turn(_block(_origin("openai", "gpt-5.5", SYS))), ANTH)
    assert _thinking(out) == []
    assert out[1]["content"][0]["type"] == "tool_use"


def test_system_prompt_change_dropped():
    b = _block(_origin("anthropic", "claude-opus-4-7", [{"role": "system", "content": "prompt v2"}]))
    assert _thinking(drop_foreign_thinking(_turn(b), ANTH)) == []


def test_unstamped_dropped():
    assert _thinking(drop_foreign_thinking(_turn(_block("")), ANTH)) == []


def test_input_not_mutated():
    msgs = _turn(_block(""))
    drop_foreign_thinking(msgs, ANTH)
    assert len(_thinking(msgs)) == 1


def _run_turn(msgs, sections, text="q"):
    q = {"role": "user", "content": text}
    system_prompt_.sync(msgs, sections, q)
    msgs.append(q)
    msgs.append({"role": "assistant", "content": "a"})


def test_head_frozen_change_becomes_patch():
    msgs = []
    _run_turn(msgs, {"date": "Oct 22", "claude_md": "v1"})
    _run_turn(msgs, {"date": "Oct 22", "claude_md": "v2"})
    _run_turn(msgs, {"date": "Oct 22", "claude_md": "v2"})  # unchanged: no new patch
    patches = [m for m in msgs if system_prompt_.PATCH_KEY in m]
    assert len(patches) == 1
    assert patches[0]["content"].startswith("<system-reminder>")
    assert patches[0][system_prompt_.PATCH_KEY] == {"claude_md": "v2"}
    assert system_prompt_.head(msgs, msgs) == {"date": "Oct 22", "claude_md": "v1"}
    assert system_prompt_.applied(msgs) == {"date": "Oct 22", "claude_md": "v2"}


def test_removed_section_and_trimmed_patch_folds_into_head():
    msgs = []
    _run_turn(msgs, {"claude_md": "v1", "skills": "s"})
    _run_turn(msgs, {"claude_md": "v1"})
    assert [m for m in msgs if system_prompt_.PATCH_KEY in m][0][system_prompt_.PATCH_KEY] == {"skills": None}
    window = msgs[-2:]  # the patch was trimmed out of what gets sent
    assert system_prompt_.head(msgs, window) == {"claude_md": "v1"}


def test_markers_survive_storage_roundtrip():
    msgs = []
    _run_turn(msgs, {"claude_md": "v1"})
    _run_turn(msgs, {"claude_md": "v2"})
    loaded = [reconstruct_message(normalize_message(m)) for m in msgs]
    assert system_prompt_.head(loaded, loaded) == {"claude_md": "v1"}
    assert system_prompt_.applied(loaded) == {"claude_md": "v2"}
