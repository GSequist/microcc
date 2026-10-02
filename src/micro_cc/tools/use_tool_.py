from types import SimpleNamespace

DISPATCHER = "use_tool_"


def use_tool_(name: str, args: dict):
    """Call a tool you loaded with search_tools(action="add"). Pass the tool's exact name and an args object that matches the schema search_tools returned for it. If the args are wrong, the error result carries the schema so you can retry.

    Args:
        name: exact tool name, e.g. "browser" or an MCP tool name.
        args: the tool's arguments as an object, matching its schema.
    """
    return "Error: use_tool_ is resolved by the loop and cannot run directly."


def _field(block, key):
    return block.get(key) if isinstance(block, dict) else getattr(block, key, None)


def resolve_call(block) -> tuple[str, dict]:
    name, args = _field(block, "name"), _field(block, "input") or {}
    if name != DISPATCHER:
        return name, args
    inner = args.get("name") if isinstance(args, dict) else None
    inner_args = args.get("args") if isinstance(args, dict) else None
    if isinstance(inner, str) and inner and isinstance(inner_args, dict):
        return inner, inner_args
    return DISPATCHER, {}


def check_wrapper_shape(block) -> str | None:
    if _field(block, "name") != DISPATCHER:
        return None
    inner, _ = resolve_call(block)
    if inner == DISPATCHER:
        return "use_tool_ needs name (a tool name string, not use_tool_ itself) and args (an object)."
    return None


def inner_block(block):
    name, args = resolve_call(block)
    return SimpleNamespace(name=name, input=args, id=_field(block, "id"))
