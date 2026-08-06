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
    """One parameter descriptor as a JSON Schema property.

    Recursive, because the descriptors are: a primitive's ``params`` declares
    its own ``properties`` block, and ``required`` is *our* spelling — a boolean
    on the property — where JSON Schema wants an array on the object above it.
    Emitting the boolean produced a schema no validator would accept, in the one
    payload whose entire purpose is being handed to someone else's validator.
    """
    prop = {k: v for k, v in (descriptor or {}).items() if k not in _INTERNAL_KEYS}
    # An `enum` alone is valid JSON Schema, but stating the type as well lets a
    # constrained decoder narrow the token space before it reaches the values.
    if "enum" in prop and "type" not in prop:
        if all(isinstance(v, str) for v in prop["enum"]):
            prop["type"] = "string"
    if isinstance(prop.get("properties"), dict):
        nested = prop["properties"]
        prop["properties"] = {k: _property_schema(v) for k, v in nested.items()}
        needed = [k for k, v in nested.items() if (v or {}).get("required")]
        if needed:
            prop["required"] = needed
    if isinstance(prop.get("items"), dict):
        prop["items"] = _property_schema(prop["items"])
    # A length cannot be zero or negative in any operation this catalog has, and
    # unlike the plausibility band (see `units.py`) that is a fact rather than a
    # guess — so it belongs in the schema, where a constrained decoder enforces
    # it before a token is spent.
    if (descriptor or {}).get("unit") == "m" and prop.get("type") == "number":
        prop.setdefault("exclusiveMinimum", 0)
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

    # `type` may be a list — JSON Schema's union form, used where an argument
    # takes either a list of ids or the shorthand "all". Looking a list up in
    # _TYPE_CHECKS would miss and silently skip the check entirely.
    declared_type = descriptor.get("type")
    names = declared_type if isinstance(declared_type, list) else [declared_type]
    checks = [_TYPE_CHECKS[t] for t in names if t in _TYPE_CHECKS]
    if checks and not any(check(value) for check in checks):
        raise BadArgument(
            f"'{name}' must be {' or '.join(str(t) for t in names)}; "
            f"got {type(value).__name__}", arg=name)

    _check_nested(name, value, descriptor)


def _check_nested(path: str, value: Any, descriptor: Dict[str, Any]) -> None:
    """Descend into a declared object or array shape.

    Only where a shape is *declared*. `add_sketch.sketch` is an opaque
    ``{"type": "object"}`` whose contents the sketch solver owns, and inventing
    a check for it here would be a second, weaker definition of a sketch.

    Skipping this descent was the single largest hole in the surface. A
    primitive's ``params`` declares its own properties and
    ``additionalProperties: false``; nothing enforced it, so
    ``{"width": 0.1, "depth": 0.06, "thickness": 0.008}`` — every key invented —
    had all three silently dropped and returned a default 50 mm cube as
    ``ok: true``. A model that invents an argument name has to be told, not
    handed a body it did not ask for.
    """
    properties = descriptor.get("properties")
    if isinstance(value, dict) and isinstance(properties, dict) and properties:
        if descriptor.get("additionalProperties") is False:
            unknown = sorted(k for k in value if k not in properties)
            if unknown:
                known = ", ".join(sorted(properties))
                raise BadArgument(
                    f"'{path}' has no field{'s' if len(unknown) > 1 else ''} "
                    f"{', '.join(repr(u) for u in unknown)}; it takes: {known}",
                    arg=path)
        for key, nested in properties.items():
            if (nested or {}).get("required") and value.get(key) is None:
                raise MissingArgument(f"'{path}.{key}' is required", arg=f"{path}.{key}")
        for key, item in value.items():
            nested = properties.get(key)
            if nested and item is not None:
                _check_value(f"{path}.{key}", item, nested)
        return

    items = descriptor.get("items")
    if isinstance(value, (list, tuple)) and isinstance(items, dict) and items:
        for index, item in enumerate(value):
            if item is not None:
                _check_value(f"{path}[{index}]", item, items)


def annotated(params: Dict[str, Any], inherited: Optional[str] = None) -> Dict[str, Any]:
    """Params with declared units folded into their descriptions, recursively.

    Generated rather than written out by hand, because a unit written into prose
    beside the code that means it drifts from that code — which is how twenty
    length parameters came to state no unit at all while the kernel worked in
    metres and the observation came back in millimetres.
    """
    from app.services import units

    out: Dict[str, Any] = {}
    for name, descriptor in (params or {}).items():
        entry = units.annotate(name, descriptor or {}, inherited)
        nested = entry.get("properties")
        if isinstance(nested, dict):
            entry = {**entry, "properties": annotated(nested, entry.get("unit"))}
        out[name] = entry
    return out


def describe(name: str, kind: str, description: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """One catalog entry, carrying both the legacy block and a usable schema."""
    described = annotated(params)
    return {
        "name": name,
        "kind": kind,
        "description": description,
        # Kept as-is in shape: the UI, the copilot prompt and the MCP catalog all
        # read it. Units are folded into the descriptions on the way out.
        "params": described,
        # What a function-calling API or a constrained decoder should be given.
        "schema": json_schema(described),
    }


def tool_definitions() -> List[Dict[str, Any]]:
    """The whole catalog as OpenAI-style function/tool definitions."""
    from app.services import cad_operations

    return [{
        "type": "function",
        "function": {
            # Which kind of id the operation takes is part of what it does, and
            # a tools payload has nowhere else to put it.
            "name": op.name,
            "description": (f"[operates on an assembly] {op.description}"
                            if op.subject == "assembly" else op.description),
            "parameters": json_schema(annotated(op.params)),
        },
    } for op in cad_operations.OPERATIONS.values()]
