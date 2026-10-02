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

    # Handle None/NoneType
    if py_type is None or py_type is type(None):
        return {"type": "null"}

    # Handle basic types
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
        # __required_keys__ reflects each field's own NotRequired[...]
        # marker; falls back to "everything required" for a TypedDict that
        # doesn't set it (shouldn't happen on 3.12+, but cheap to guard).
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

    # Handle Literal["a", "b", "c"] -> enum
    if origin is Literal:
        return {"type": "string", "enum": list(args)}

    # Handle Optional[T] which is Union[T, None]
    if origin is Union:
        # Filter out NoneType
        non_none_args = [a for a in args if a is not type(None)]
        if len(non_none_args) == 1:
            # Optional[T] -> just return T's schema
            return python_type_to_json_schema(non_none_args[0])
        else:
            # Union of multiple types
            return {"oneOf": [python_type_to_json_schema(a) for a in non_none_args]}

    # Handle list[T]
    if origin is list:
        if args:
            return {"type": "array", "items": python_type_to_json_schema(args[0])}
        return {"type": "array"}

    # Handle dict[K, V]
    if origin is dict:
        schema = {"type": "object"}
        if len(args) >= 2:
            schema["additionalProperties"] = python_type_to_json_schema(args[1])
        return schema

    # Handle tuple
    if origin is tuple:
        if args:
            return {
                "type": "array",
                "items": [python_type_to_json_schema(a) for a in args],
            }
        return {"type": "array"}

    # Fallback for unknown types
    return {"type": "string"}


def parse_google_docstring(docstring: str) -> tuple[str, Dict[str, str]]:
    """
    Parse Google-style docstring.

    Returns:
        (description, {param_name: param_description})
    """
    if not docstring:
        return "", {}

    lines = docstring.split("\n")

    # Find Args: section
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
            # Save last param if any
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

            # Calculate indentation
            indent = len(line) - len(line.lstrip())

            # Check if this is a new parameter (name: description pattern)
            param_match = re.match(r"^(\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)$", stripped)

            if param_match and (base_indent is None or indent == base_indent):
                # Save previous param
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
                # Continuation of current param description
                current_desc_lines.append(stripped)

        elif not in_returns_section and not in_args_section:
            # Part of main description
            description_lines.append(stripped)

    # Save last param
    if current_param and current_desc_lines:
        param_descriptions[current_param] = " ".join(current_desc_lines).strip()

    # Clean up description - join and remove empty lines at start/end
    description = " ".join(line for line in description_lines if line).strip()

    return description, param_descriptions


def parse_legacy_docstring(docstring: str) -> tuple[str, Dict[str, Any]]:
    """
    Parse legacy #parameters: format for backward compatibility.

    Returns:
        (description, {param_name: description_or_schema})
    """
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
            # Save previous param
            if current_param and current_content:
                content = " ".join(current_content).strip()
                # Try parsing as JSON schema
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

    # Save last param
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
    """
    Convert a Python function to Anthropic tool schema.

    Supports:
    - Google-style docstrings with Args: section
    - Legacy #parameters: format (backward compatible)
    - Type hints for automatic type inference
    - Optional parameters via = None or Optional[T]
    """
    # Handle partial functions
    if hasattr(func, "func"):
        actual_func = func.func
        func_name = actual_func.__name__
    else:
        actual_func = func
        func_name = func.__name__

    docstring = inspect.getdoc(actual_func) or ""
    sig = inspect.signature(actual_func)

    # Try to get type hints (may fail for some edge cases)
    try:
        type_hints = get_type_hints(actual_func)
    except Exception:
        type_hints = {}

    # Detect format and parse
    if "#parameters:" in docstring:
        # Legacy format
        description, param_data = parse_legacy_docstring(docstring)
        use_legacy = True
    else:
        # Google-style
        description, param_data = parse_google_docstring(docstring)
        use_legacy = False

    properties = {}
    required_params = []

    for param_name, param in sig.parameters.items():
        # Skip internal params
        if param_name in INTERNAL_PARAMS:
            continue

        # Skip *args and **kwargs
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue

        # Get type from hints
        py_type = type_hints.get(param_name)

        # Build property schema
        if use_legacy and param_name in param_data:
            # Legacy: might be full schema or just description
            data = param_data[param_name]
            if isinstance(data, dict):
                properties[param_name] = data
            else:
                # Just a description, infer type
                if py_type:
                    prop = python_type_to_json_schema(py_type)
                else:
                    prop = {"type": "string"}
                prop["description"] = data
                properties[param_name] = prop
        else:
            # Google-style or no docs
            if py_type:
                prop = python_type_to_json_schema(py_type)
            else:
                prop = {"type": "string"}

            if param_name in param_data:
                prop["description"] = param_data[param_name]

            properties[param_name] = prop

        # Determine if required
        if param.default == inspect.Parameter.empty:
            # Check if Optional type (has None in union)
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
