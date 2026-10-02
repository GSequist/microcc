from micro_cc.tools.use_tool_ import resolve_call

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
    for i, msg in enumerate(msgs):
        role = msg.get("role")
        content = msg.get("content", "")

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

        parts = []
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
            ui.append({"id": f"msg-{i}", "role": "assistant", "parts": parts})

    return ui
