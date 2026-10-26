"""Minimal JSON-Schema subset validator for tool arguments.

Supports: type (string/integer/number/boolean/array/object), required, enum,
minimum/maximum, items. Anything else is ignored on purpose; the upstream API
(WooCommerce) does the authoritative validation.
"""
from __future__ import annotations

from typing import Any

_TYPES = {
    "string": str,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _is_type(value: Any, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[expected])


def validate(value: Any, schema: dict, path: str = "arguments") -> list[str]:
    """Return a list of human-readable problems (empty list means valid)."""
    problems: list[str] = []
    expected = schema.get("type")
    if expected in ("string", "integer", "number", "boolean", "array", "object"):
        if not _is_type(value, expected):
            return [f"{path}: expected {expected}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: must be one of {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: must be <= {schema['maximum']}")
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                problems.append(f"{path}.{name}: is required")
        for name, sub in schema.get("properties", {}).items():
            if name in value:
                problems.extend(validate(value[name], sub, f"{path}.{name}"))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            problems.extend(validate(item, schema["items"], f"{path}[{i}]"))
    return problems
