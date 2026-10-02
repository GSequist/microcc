from micro_cc.tools.use_tool_ import resolve_call


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
            if isinstance(content, str):
                history_msgs.append({"type": "text", "content": content})
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
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
    