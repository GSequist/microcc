from micro_cc.tools.use_tool_ import resolve_call
from micro_cc.utils.msg_normalize_ import REPLY_THINKING_KEY


def _thinking_row(rows, block):
    text = block.get("thinking") if isinstance(block, dict) else None
    if text and text.strip():  # encrypted-only reasoning has nothing to show
        rows.append({"type": "thinking", "content": text.strip()})


def history_mount_(messages):
    history_msgs = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content", "")
        if role == "system":
            continue
        if role == "user":
            if isinstance(content, str) and not content.startswith("<system-reminder>"):
                history_msgs.append({"type": "user", "content": content.strip()})
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        out = str(block.get("content", ""))[:200]
                        history_msgs.append({"type": "tool_call", "name": "tool", "result": out.strip()})
        elif role == "assistant":
            for block in msg.get(REPLY_THINKING_KEY) or []:  # a final reply's thinking, stored beside its text
                _thinking_row(history_msgs, block)
            if isinstance(content, str):
                history_msgs.append({"type": "text", "content": content})
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "thinking":
                            _thinking_row(history_msgs, block)
                        elif block.get("type") == "text":
                            history_msgs.append({"type": "text", "content": block["text"]})
                        elif block.get("type") == "tool_use":
                            name, inp = resolve_call(block)
                            history_msgs.append({
                                "type": "tool_call",
                                "name": name,
                                "input": inp,
                                "result": "·",
                            })

    return history_msgs
