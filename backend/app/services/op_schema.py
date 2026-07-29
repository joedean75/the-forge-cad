"""Turning the operation catalog into real JSON Schema, and enforcing it.

The catalog described its arguments in what its own docstring called a
"JSON-schema-ish params block": a flat ``{name: descriptor}`` map with no
``required`` list, no object wrapper, and no validation step behind it. Three
consequences mattered.

A model could not be *constrained* to it. Grammar-constrained decoding and
provider structured-output modes take JSON Schema; "schema-ish" is not something
a decoder can be pointed at, so the surface an agent is meant to be fine-tuned
against could only be described in prose and hoped for.

Required-ness was invisible and enforced by accident — by the handler indexing
``args["sketch"]`` and raising. Nothing told the caller in advance which
arguments it had to supply.

And the declared defaults were decorative: every handler re-applied its own with
``args.get(k, default)``, so the catalog could say one thing while the code did
another with nothing to catch it.

This module derives a strict schema from the same descriptors and validates
against it before a handler runs, so a malformed call is refused by the API with
a typed error rather than part-way through mutating a tree.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.services.op_errors import BadArgument, MissingArgument

# Keys that are ours, not JSON Schema's, and are stripped from the emitted schema.
_INTERNAL_KEYS = {"required"}

_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, (list, tuple)),
}


def _property_schema(descriptor: Dict[str, Any]) -> Dict[str, Any]:
    """One parameter descriptor as a JSON Schema property."""
    prop = {k: v for k, v in (descriptor or {}).items() if k not in _INTERNAL_KEYS}
    # An `enum` alone is valid JSON Schema, but stating the type as well lets a
    # constrained decoder narrow the token space before it reaches the values.
    if "enum" in prop and "type" not in prop:
        if all(isinstance(v, str) for v in prop["enum"]):
            prop["type"] = "string"
    return prop


def json_schema(params: Dict[str, Any]) -> Dict[str, Any]:
    """A strict object schema for one operation's arguments."""
    properties = {name: _property_schema(d) for name, d in (params or {}).items()}
    required = [name for name, d in (params or {}).items() if (d or {}).get("required")]
    schema: Dict[str, Any] = {
        "type": "object",
        "properties": properties,
        # Closed by design: a misspelled argument should be rejected, not
        # silently ignored until the geometry comes out wrong.
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


def validate(params: Dict[str, Any], args: Optional[Dict[str, Any]], *, op: str = "") -> None:
    """Check `args` against a catalog `params` block. Raises a typed error.

    Deliberately narrow — required-ness, declared type, and enum membership. It
    is a gate for obviously malformed calls, not a replacement for the domain
    checks a handler still makes.
    """
    args = args or {}
    declared = params or {}

    for name, descriptor in declared.items():
        if (descriptor or {}).get("required") and args.get(name) is None:
            raise MissingArgument(f"'{name}' is required by {op}" if op
                                  else f"'{name}' is required", arg=name)

    unknown = [k for k in args if k not in declared]
    if unknown and declared:
        known = ", ".join(sorted(declared)) or "none"
        raise BadArgument(
            f"unknown argument{'s' if len(unknown) > 1 else ''} "
            f"{', '.join(repr(u) for u in sorted(unknown))}; {op or 'this operation'} takes: {known}",
            arg=sorted(unknown)[0])

    for name, value in args.items():
        if value is None:
            continue
        descriptor = declared.get(name) or {}
        _check_value(name, value, descriptor)


def _check_value(name: str, value: Any, descriptor: Dict[str, Any]) -> None:
    choices = descriptor.get("enum")
    if choices is not None and value not in choices:
        raise BadArgument(
            f"'{name}' must be one of {', '.join(map(str, choices))}; got {value!r}", arg=name)

    declared_type = descriptor.get("type")
    check = _TYPE_CHECKS.get(declared_type)
    if check is not None and not check(value):
        raise BadArgument(
            f"'{name}' must be {declared_type}; got {type(value).__name__}", arg=name)


def describe(name: str, kind: str, description: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """One catalog entry, carrying both the legacy block and a usable schema."""
    return {
        "name": name,
        "kind": kind,
        "description": description,
        # Kept as-is: the UI, the copilot prompt and the MCP catalog all read it.
        "params": params,
        # What a function-calling API or a constrained decoder should be given.
        "schema": json_schema(params),
    }


def tool_definitions() -> List[Dict[str, Any]]:
    """The whole catalog as OpenAI-style function/tool definitions."""
    from app.services import cad_operations

    return [{
        "type": "function",
        "function": {
            "name": op.name,
            "description": op.description,
            "parameters": json_schema(op.params),
        },
    } for op in cad_operations.OPERATIONS.values()]
