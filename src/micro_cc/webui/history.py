from micro_cc.tools.use_tool_ import resolve_call
from micro_cc.utils.amend_ import AMENDED_KEY, fold_spans, text_of

MAX_OUTPUT_CHARS = 4000


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content)


def to_ui_messages(msgs: list) -> list:
    outputs = {}
    for msg in msgs:
        content = msg.get("content")
        if msg.get("role") == "user" and isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    outputs[block.get("tool_use_id")] = str(block.get("content", ""))[:MAX_OUTPUT_CHARS]

    ui = []
    spans = fold_spans(msgs)
    skip_to = -1
    for i, msg in enumerate(msgs):
        if i <= skip_to:
            continue
        role = msg.get("role")
        content = msg.get("content", "")

        if i in spans:
            # Correction turn: one line under the reply it amended.
            skip_to = spans[i]
            ack = text_of(msgs[skip_to].get("content"))
            ui.append({
                "id": f"msg-{i}",
                "role": "user",
                "parts": [{"type": "data-folded", "data": {"note": content.strip(), "ack": ack.strip()}}],
            })
            continue

        if role == "system":
            continue

        if role == "user":
            if not isinstance(content, str):
                continue
            if content.strip().startswith("<system-reminder>"):
                continue
            ui.append({
                "id": f"msg-{i}",
                "role": "user",
                "parts": [{"type": "text", "text": content}],
            })
            continue

        if role != "assistant":
            continue

        parts = [{"type": "reasoning", "text": b.get("thinking", "")}
                 for b in msg.get("_thinking") or [] if isinstance(b, dict) and b.get("thinking")]
        if isinstance(content, str):
            if content.strip():
                parts.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    if block.get("text", "").strip():
                        parts.append({"type": "text", "text": block["text"]})
                elif btype == "thinking":
                    parts.append({"type": "reasoning", "text": block.get("thinking", "")})
                elif btype == "tool_use":
                    tool_id = block.get("id")
                    tool_name, tool_input = resolve_call(block)
                    parts.append({
                        "type": "dynamic-tool",
                        "toolName": tool_name or "tool",
                        "toolCallId": tool_id,
                        # No stored result = interrupted mid-turn; don't render it as a tool that quietly succeeded.
                        "state": "output-available" if tool_id in outputs else "output-error",
                        "input": tool_input,
                        **(
                            {"output": outputs[tool_id]}
                            if tool_id in outputs
                            else {"errorText": "interrupted — no result recorded"}
                        ),
                    })

        if parts:
            row = {"id": f"msg-{i}", "role": "assistant", "parts": parts}
            if msg.get(AMENDED_KEY):
                row["metadata"] = {"amended": msg[AMENDED_KEY]}
            ui.append(row)

    return ui
