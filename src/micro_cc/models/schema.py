from typing import (
    Any,
    Dict,
    get_type_hints,
    get_origin,
    get_args,
    Union,
    Literal,
    is_typeddict,
)
import inspect
import json
import re


INTERNAL_PARAMS = ["project_dir", "model"]


def python_type_to_json_schema(py_type) -> Dict[str, Any]:
    """Convert Python type annotation to JSON Schema."""
    if py_type is None or py_type is type(None):
        return {"type": "null"}

    type_map = {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
        dict: {"type": "object"},
        list: {"type": "array"},
        bytes: {"type": "string"},
    }

    if py_type in type_map:
        return type_map[py_type]

    if is_typeddict(py_type):
        hints = get_type_hints(py_type)
        # NotRequired is honoured via __required_keys__; default to all-required.
        required_keys = getattr(py_type, "__required_keys__", set(hints))
        properties = {}
        required = []
        for field_name, field_type in hints.items():
            properties[field_name] = python_type_to_json_schema(field_type)
            if field_name in required_keys:
                required.append(field_name)
        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    origin = get_origin(py_type)
    args = get_args(py_type)

    if origin is Literal:
        return {"type": "string", "enum": list(args)}

    if origin is Union:
        non_none_args = [a for a in args if a is not type(None)]
        if len(non_none_args) == 1:
            return python_type_to_json_schema(non_none_args[0])
        else:
            return {"oneOf": [python_type_to_json_schema(a) for a in non_none_args]}

    if origin is list:
        if args:
            return {"type": "array", "items": python_type_to_json_schema(args[0])}
        return {"type": "array"}

    if origin is dict:
        schema = {"type": "object"}
        if len(args) >= 2:
            schema["additionalProperties"] = python_type_to_json_schema(args[1])
        return schema

    if origin is tuple:
        if args:
            return {
                "type": "array",
                "items": [python_type_to_json_schema(a) for a in args],
            }
        return {"type": "array"}

    return {"type": "string"}


def parse_google_docstring(docstring: str) -> tuple[str, Dict[str, str]]:
    """Split a Google-style docstring into (description, {param: description})."""
    if not docstring:
        return "", {}

    lines = docstring.split("\n")

    description_lines = []
    param_descriptions = {}

    in_args_section = False
    in_returns_section = False
    current_param = None
    current_desc_lines = []
    base_indent = None

    for line in lines:
        stripped = line.strip()

        # Detect section headers
        if stripped in ("Args:", "Arguments:", "Parameters:"):
            in_args_section = True
            in_returns_section = False
            continue
        elif stripped in (
            "Returns:",
            "Return:",
            "Yields:",
            "Raises:",
            "Examples:",
            "Example:",
            "Note:",
            "Notes:",
        ):
            if current_param and current_desc_lines:
                param_descriptions[current_param] = " ".join(current_desc_lines).strip()
            in_args_section = False
            in_returns_section = stripped.startswith("Return")
            current_param = None
            current_desc_lines = []
            continue

        if in_args_section:
            if not stripped:
                continue

            indent = len(line) - len(line.lstrip())

            param_match = re.match(r"^(\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)$", stripped)

            if param_match and (base_indent is None or indent == base_indent):
                if current_param and current_desc_lines:
                    param_descriptions[current_param] = " ".join(
                        current_desc_lines
                    ).strip()

                current_param = param_match.group(1)
                first_desc = param_match.group(2).strip()
                current_desc_lines = [first_desc] if first_desc else []

                if base_indent is None:
                    base_indent = indent
            elif current_param:
                current_desc_lines.append(stripped)

        elif not in_returns_section and not in_args_section:
            description_lines.append(stripped)

    # Save last param
    if current_param and current_desc_lines:
        param_descriptions[current_param] = " ".join(current_desc_lines).strip()

    description = " ".join(line for line in description_lines if line).strip()

    return description, param_descriptions


def parse_legacy_docstring(docstring: str) -> tuple[str, Dict[str, Any]]:
    """Split the legacy `#parameters:` docstring format into (description, {param: description_or_schema})."""
    if not docstring or "#parameters:" not in docstring:
        return docstring.strip() if docstring else "", {}

    doc_parts = docstring.split("#parameters:")
    main_description = doc_parts[0].strip()
    param_section = doc_parts[1].strip() if len(doc_parts) > 1 else ""

    param_data = {}
    current_param = None
    current_content = []

    for line in param_section.split("\n"):
        line = line.strip()
        if not line:
            continue

        if ":" in line and not line.startswith(" "):
            if current_param and current_content:
                content = " ".join(current_content).strip()
                if content.startswith("{") and content.endswith("}"):
                    try:
                        param_data[current_param] = json.loads(content)
                    except json.JSONDecodeError:
                        param_data[current_param] = content
                else:
                    param_data[current_param] = content

            current_param = line.split(":", 1)[0].strip()
            current_content = [line.split(":", 1)[1].strip()]
        elif current_param:
            current_content.append(line)

    if current_param and current_content:
        content = " ".join(current_content).strip()
        if content.startswith("{") and content.endswith("}"):
            try:
                param_data[current_param] = json.loads(content)
            except json.JSONDecodeError:
                param_data[current_param] = content
        else:
            param_data[current_param] = content

    return main_description, param_data


def function_to_schema(func: callable) -> Dict[str, Any]:
    """Convert a Python function to an Anthropic tool schema (Google Args section or legacy #parameters:)."""
    if hasattr(func, "func"):
        actual_func = func.func
        func_name = actual_func.__name__
    else:
        actual_func = func
        func_name = func.__name__

    docstring = inspect.getdoc(actual_func) or ""
    sig = inspect.signature(actual_func)

    try:
        type_hints = get_type_hints(actual_func)
    except Exception:
        type_hints = {}

    if "#parameters:" in docstring:
        description, param_data = parse_legacy_docstring(docstring)
        use_legacy = True
    else:
        description, param_data = parse_google_docstring(docstring)
        use_legacy = False

    properties = {}
    required_params = []

    for param_name, param in sig.parameters.items():
        if param_name in INTERNAL_PARAMS:
            continue

        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue

        py_type = type_hints.get(param_name)

        if use_legacy and param_name in param_data:
            data = param_data[param_name]
            if isinstance(data, dict):
                properties[param_name] = data
            else:
                if py_type:
                    prop = python_type_to_json_schema(py_type)
                else:
                    prop = {"type": "string"}
                prop["description"] = data
                properties[param_name] = prop
        else:
            if py_type:
                prop = python_type_to_json_schema(py_type)
            else:
                prop = {"type": "string"}

            if param_name in param_data:
                prop["description"] = param_data[param_name]

            properties[param_name] = prop

        if param.default == inspect.Parameter.empty:
            if py_type:
                origin = get_origin(py_type)
                args = get_args(py_type)
                is_optional = origin is Union and type(None) in args
            else:
                is_optional = False

            if not is_optional:
                required_params.append(param_name)

    return {
        "name": func_name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required_params,
        },
    }


def tools_to_chat(tools: list[dict]) -> list[dict] | None:
    """Application tool schemas -> Chat Completions function tools."""
    return [
        {"type": "function", "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {}),
        }}
        for tool in tools
    ] or None


def tools_to_responses(tools: list[dict]) -> list[dict] | None:
    """Application tool schemas -> Responses API flat function tools."""
    return [
        {"type": "function", "name": tool["name"],
         "description": tool.get("description", ""),
         "parameters": tool.get("input_schema", {})}
        for tool in tools
    ] or None
