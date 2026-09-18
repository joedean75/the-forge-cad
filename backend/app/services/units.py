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
    # A thread is named by its major *diameter* — M8 is 8 mm across, not 8 mm
    # of radius — so `add_thread` takes the number everybody already knows
    # rather than renaming it to match the primitives next door.
    "diameter": "m",
    "height": "m", "thickness": "m", "size": "m", "width": "m", "length": "m",
    "offset": "m", "offset_a": "m", "offset_b": "m", "spacing": "m",
    "center": "m", "distance": "m", "translate": "m", "path": "m",
    "a": "m", "b": "m",
    # machined holes: the recess at the mouth
    "recess_radius": "m", "recess_depth": "m",
    # variable fillet and draft. A rib's `start`/`end` are *not* here: `start`
    # is already a boolean on `print_part`, so one name genuinely means two
    # things and the rib's descriptors carry an explicit `unit` instead — which
    # is what `unit_of` checks before either map.
    "radius_end": "m", "neutral_offset": "m",
    # helices: the coil, the wire wound on it, and the rise per turn
    "helix_radius": "m", "wire_radius": "m", "pitch": "m",
    # knit: how far apart two edges may be and still be the same edge
    "tolerance": "m",
    # a mate connector's frame origin, in part space
    "origin": "m",
    # angles
    "angle": "deg", "rotate": "deg",
    "countersink_angle": "deg", "tip_angle": "deg", "draft": "deg",
    # already named for their unit, and not in metres — never warned about
    "feed_mm_min": "mm", "kerf_mm": "mm", "min_length_mm": "mm",
    "nominal_mm": "mm", "general_tolerance": "mm", "capacity_mah": "mAh",
}

#: Vector components. They mean whatever the vector means — metres under
#: ``center``, degrees under ``rotate``, nothing at all under ``axis`` — so they
#: take their unit from above and are never a name that needs classifying.
COMPONENTS = frozenset({"x", "y", "z"})

#: Parameters that are genuinely dimensionless, listed so that "has no unit" is
#: a decision rather than an omission.
UNITLESS = frozenset({
    "component_ids", "image_indices",
    "skip",
    "active", "axis", "build_axis", "configurations", "direction", "edge",
    "edges", "elements", "faces", "feature", "feature_id", "fit", "flanges",
    "gdt", "goal", "instruction", "k_factor", "keep", "kind", "label",
    "material_id", "modifier", "name", "notes", "on_face", "operation",
    "parameters", "params", "plane", "power", "printer_url", "process",
    "profile", "profile_a", "profile_b", "requirements", "resolution", "ruled",
    # `steps` is a count of copilot turns, not a distance — `build_part` budgets
    # the part run it starts.
    "steps",
    "scale", "section", "sections", "settings", "shape", "sketch", "sketch_a",
    "sketch_b", "spec", "start", "state", "suppressed", "target", "tolerances",
    "count", "annotations", "dimensions", "projection", "sheet", "show_hidden",
    "views", "plane_origin",
    # a review render: which named directions to look from, and how big the
    # picture is. `pixels` is deliberately not called `size`, which this map
    # reads as metres — the same trap `clearance_hole`'s `screw` avoids.
    "cameras", "pixels",
    # background jobs: an opaque id, and the per-mode settings block that
    # `workflow_status` uses to answer freshness (whose contents are themselves
    # classified wherever they are declared).
    "job_id",
    # a battery pack's electrical record: a count and a chemistry name. The
    # capacity carries its unit in its own name and is in the map above.
    "cells", "chemistry",
    # mechanism notes: a tooth count, the designation of a bought part, free
    # text, and a pair of note ids. `module_mm` is a length and carries its own
    # explicit unit on the descriptor, which is why it is not listed here.
    "teeth", "designation", "text", "ratio",
    # RC presets: a preset group name and two tooth counts. `module`,
    # `wheelbase`, `kingpin_track` and `arm` are lengths and carry an explicit
    # `unit` on their descriptors.
    "group", "pinion_teeth", "spur_teeth",
    # manufacturing intent and printability: a machine name, a list of screw
    # designations, a percentage, a drawing style and a boolean. Every length
    # among them (`nozzle_mm`, `layer_mm`, `bed_mm`, `clearance_mm`,
    # `margin_mm`) names its own unit and carries it explicitly.
    "printer", "screws", "infill_percent", "style", "mechanism", "preset",
    # assembly operations: ids, constraint kinds and bounding-box extremes
    "part_id", "component_id", "mate_id", "type", "base", "moved",
    "base_face", "moved_face",
    # export + release package: a file format and a revision letter
    "format", "revision",
    # viewport-only: whether to return points along each edge
    "polyline",
    # the standard-parts catalog: a catalog key, a count, the ISO 273 fit
    # series, and `screw` — a thread designation ("M4"), which is why it is not
    # called `size`: that name is a length everywhere else here and the unit is
    # folded into the description from the name.
    "key", "quantity", "category", "search", "limit", "fit", "counterbore",
    "screw",
    # machined holes: the recess shape, whether it breaks out of the far side,
    # and the thread callout. `tapped`/`thread` are designations ("M6"), which
    # is why neither is called `size` — see `screw` above.
    "hole_type", "through", "tapped", "thread",
    # a rib's growth direction and a draft's pull direction — both are
    # directions, like `axis`, so neither carries a unit
    "up", "pull",
    # a helix's turn count (a number of revolutions) and its handedness
    "turns", "right_handed",
    # a table pattern's placements. Unitless at the top: the lengths inside it
    # are under `translate`, which `_walk` does not descend into for a list of
    # objects — so those escape the magnitude warning, deliberately and not by
    # oversight. A pattern is written by the decompiler far more often than by
    # hand, and a per-entry warning on a fifty-copy pattern is fifty warnings.
    "transforms",
    # mate connectors: a frame's two direction vectors, and the ids a mate
    # uses to name one on each side. Directions, like `axis`, carry no unit.
    "z_axis", "x_axis", "base_connector", "moved_connector",
    # the toolpath verifier: a grid resolution, like `resolution` above.
    "samples",
    # URDF export: whether links reference their mesh for visual geometry.
    "mesh_urls",
    # the starter-part gallery: which template to build
    "template",
    # the purchased-parts listing: which portal an imported part came from
    "source",
    # in-context design: the document a reference reads, which of its features
    # it names, and which released revision it is pinned to. Ids and a letter,
    # like `part_id` and `revision` above — `entities` is a list of ids, not of
    # lengths, which is why it belongs here and not in `COMPONENTS`.
    "document_id", "entities",
    # `mounting_pattern`: which named bolt pattern to drill. A name, like
    # `template` and `kind` above — the dimensions behind it are the catalog's.
    "pattern",
    # `prop_clearance`: component ids to treat as rotors. A list of ids, like
    # `edges` and `faces` — what it names carries the dimension, not this.
    "props",
    # `trim`: which side of the plane survives. A name, and deliberately not a
    # sign — see the operation.
    "keep",
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
