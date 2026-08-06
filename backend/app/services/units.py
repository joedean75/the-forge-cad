"""Metres in, millimetres out — and the one place that says so.

The kernel works in metres. The *observation* an agent reads back is in
millimetres (`bbox_size_mm`, and every DFM and spec figure). So on every single
call a model reads `100.0` and must write `0.1`, and until now the only thing
holding that line was one sentence of prose in the copilot's system prompt.
Twenty length parameters did not state a unit anywhere in their description, and
`add_primitive` with a 100 x 60 x 20 box returned `ok: true`, no warning, and a
hundred-metre part.

Two halves, and they are deliberately different in strength:

* **The unit is declared on the descriptor** (`"unit": "m"`) and *generated*
  into the description, so the prose cannot drift from the code the way
  hand-written units always do. `op_schema` also emits `exclusiveMinimum: 0`
  from it, because a zero or negative length is impossible rather than merely
  surprising, and a constrained decoder can enforce that before a token is
  spent.
* **The magnitude is only a warning.** A 2 m sheet is a real part and a 0.15 mm
  wall is a real wall, so a hard bound would refuse legitimate work to catch a
  common mistake. The warning names the suspicion instead — "0.1 m = 100 mm; did
  you mean millimetres?" — which is actionable precisely because the copilot
  already forwards warnings to the planner.

Accepting millimetres as a second input unit was considered and rejected: two
accepted units is two ways for a document to mean two different things, and the
kernel is metres throughout.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

#: The band outside which a length is worth a second look, in metres.
#: 50 µm is below any process this system quotes for; 5 m is longer than any
#: machine in `services/machines.py` can hold. Generous on purpose — this is a
#: net for the mm/m confusion, not a statement about what parts exist.
PLAUSIBLE_M: Tuple[float, float] = (5e-5, 5.0)

_UNIT_SUFFIX = {"m": "metres", "deg": "degrees", "mm": "millimetres"}

#: Unit by parameter *name*, applied at catalog build.
#:
#: Central rather than a flag on each of the 142 descriptors, for one reason
#: that matters more than the tidiness: a per-descriptor flag can be forgotten
#: when an operation is added, and a length that quietly carries no unit is
#: exactly the state this module exists to end. A name here is a name everywhere,
#: and `test_action_space` fails on any parameter whose name is in neither this
#: map nor `UNITLESS` — so a new one has to be classified, not defaulted.
#:
#: A descriptor may still carry its own explicit ``unit``, which wins.
UNIT_BY_NAME: Dict[str, str] = {
    # lengths, metres
    "radius": "m", "base_radius": "m", "bend_radius": "m", "depth": "m",
    "height": "m", "thickness": "m", "size": "m", "width": "m", "length": "m",
    "offset": "m", "offset_a": "m", "offset_b": "m", "spacing": "m",
    "center": "m", "distance": "m", "translate": "m", "path": "m",
    "a": "m", "b": "m",
    # angles
    "angle": "deg", "rotate": "deg",
    # already named for their unit, and not in metres — never warned about
    "feed_mm_min": "mm", "kerf_mm": "mm", "min_length_mm": "mm",
    "nominal_mm": "mm", "general_tolerance": "mm",
}

#: Vector components. They mean whatever the vector means — metres under
#: ``center``, degrees under ``rotate``, nothing at all under ``axis`` — so they
#: take their unit from above and are never a name that needs classifying.
COMPONENTS = frozenset({"x", "y", "z"})

#: Parameters that are genuinely dimensionless, listed so that "has no unit" is
#: a decision rather than an omission.
UNITLESS = frozenset({
    "active", "axis", "build_axis", "configurations", "direction", "edge",
    "edges", "elements", "faces", "feature", "feature_id", "fit", "flanges",
    "gdt", "goal", "instruction", "k_factor", "keep", "kind", "label",
    "material_id", "modifier", "name", "notes", "on_face", "operation",
    "parameters", "params", "plane", "power", "printer_url", "process",
    "profile", "profile_a", "profile_b", "requirements", "resolution", "ruled",
    "scale", "section", "sections", "settings", "shape", "sketch", "sketch_a",
    "sketch_b", "spec", "start", "state", "suppressed", "target", "tolerances",
    "count", "annotations", "dimensions", "projection", "sheet", "show_hidden",
    "views", "plane_origin",
    # assembly operations: ids, constraint kinds and bounding-box extremes
    "part_id", "component_id", "mate_id", "type", "base", "moved",
    "base_face", "moved_face",
    # export + release package: a file format and a revision letter
    "format", "revision",
})


def unit_of(name: str, descriptor: Optional[Dict[str, Any]] = None,
            inherited: Optional[str] = None) -> Optional[str]:
    """The unit a parameter is in, or None if it is dimensionless.

    ``inherited`` is the unit of the object this one sits inside. A vector's
    ``x``/``y``/``z`` mean whatever their parent means — metres under ``center``,
    degrees under ``rotate`` — so they take it from above rather than from a
    name that cannot possibly know.
    """
    explicit = (descriptor or {}).get("unit")
    if explicit:
        return explicit
    if name in COMPONENTS:
        return inherited
    if name in UNITLESS:
        return None
    return UNIT_BY_NAME.get(name) or inherited


def classified(name: str, descriptor: Optional[Dict[str, Any]] = None,
               inherited: Optional[str] = None) -> bool:
    """Whether this parameter has been given a unit or declared unitless."""
    return (bool((descriptor or {}).get("unit")) or name in UNITLESS
            or name in UNIT_BY_NAME or name in COMPONENTS or bool(inherited))


def annotate(name: str, descriptor: Dict[str, Any],
             inherited: Optional[str] = None) -> Dict[str, Any]:
    """Fold the parameter's unit into its description, once, at catalog build."""
    unit = unit_of(name, descriptor, inherited)
    if not unit:
        return descriptor
    descriptor = {**(descriptor or {}), "unit": unit}
    word = _UNIT_SUFFIX.get(unit, unit)
    description = (descriptor.get("description") or "").rstrip().rstrip(".")
    if word in description:
        return descriptor
    joined = f"{description}, in {word}" if description else f"in {word}"
    return {**descriptor, "description": joined}


def unclassified(params: Optional[Dict[str, Any]], prefix: str = "",
                 inherited: Optional[str] = None) -> List[str]:
    """Parameter paths whose unit is neither declared nor deliberately absent.

    A component of a *classified* parent counts as classified whatever the
    parent decided: ``axis.x`` is dimensionless because ``axis`` is, and
    ``center.x`` is metres because ``center`` is. What this catches is a new
    name nobody has ruled on — which is the only way a length loses its unit.
    """
    missing: List[str] = []
    for name, descriptor in (params or {}).items():
        known = classified(name, descriptor, inherited)
        if not known:
            missing.append(f"{prefix}{name}")
        nested = (descriptor or {}).get("properties")
        if isinstance(nested, dict):
            missing += unclassified(nested, f"{prefix}{name}.",
                                    unit_of(name, descriptor, inherited))
    return missing


def _lengths(value: Any, unit: Optional[str], path: str) -> Iterable[Tuple[str, float]]:
    """Every numeric length inside a value, with the path that named it."""
    if unit != "m":
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        yield path, float(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                yield f"{path}.{key}", float(item)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                yield f"{path}[{index}]", float(item)


def _walk(args: Dict[str, Any], params: Dict[str, Any], prefix: str = "",
          inherited: Optional[str] = None) -> Iterable[Tuple[str, float]]:
    for name, descriptor in (params or {}).items():
        if name not in (args or {}) or args[name] is None:
            continue
        value = args[name]
        path = f"{prefix}{name}"
        unit = unit_of(name, descriptor, inherited)
        yield from _lengths(value, unit, path)
        nested = (descriptor or {}).get("properties")
        if isinstance(value, dict) and isinstance(nested, dict):
            yield from _walk(value, nested, prefix=f"{path}.", inherited=unit)


def implausible(args: Optional[Dict[str, Any]], params: Optional[Dict[str, Any]]
                ) -> List[Dict[str, str]]:
    """Warnings for lengths outside `PLAUSIBLE_M`, naming the likely mistake."""
    low, high = PLAUSIBLE_M
    out: List[Dict[str, str]] = []
    for path, value in _walk(args or {}, params or {}):
        if low <= abs(value) <= high or value == 0:
            continue
        if abs(value) > high:
            hint = (f"{path} = {value:g} m is {value * 1000:g} mm — "
                    f"did you mean {value / 1000:g} m? Lengths are in metres.")
        else:
            hint = (f"{path} = {value:g} m is {value * 1e6:g} µm — "
                    f"smaller than anything this catalog can make.")
        out.append({"code": "SUSPECT_UNITS", "message": hint})
    return out
