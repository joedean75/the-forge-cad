"""What was the part *supposed* to be — and does it meet that?

Every other check in this codebase asks whether the geometry is well formed.
`geometry_check` asks whether it is a closed solid, `dfm` whether a machine can
make it, `drawing.check_drawing` whether the print says enough to work from.
None of them can tell a correct bracket from a correct paperweight, because
nothing anywhere recorded what was being asked for.

That gap matters most for training. A reward signal built only on validity
teaches a model to emit *some* valid solid; it has no gradient toward the right
one. A spec closes the loop: the requirements are stated up front, every one is
independently measurable against the evaluated body, and the score is the
fraction met.

Two design rules keep it honest.

**Every requirement kind is something the kernel can actually measure.** There
is no `looks_like` or `is_ergonomic`. A requirement that cannot be checked
mechanically is a requirement a model can learn to claim rather than meet.

**An unmeasurable requirement fails loudly rather than passing quietly.** An
unknown `kind`, a missing argument, a body the exact kernel cannot address — all
of them return `fail` with the reason. The alternative is a spec that scores 1.0
because half of it was skipped, which is the single worst thing a reward
function can do.

Results are `DfmCheck` rows, so a spec report renders and scores exactly like
the manufacturability ones.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.models.schemas import DfmCheck

# Requirements are written in millimetres, grams and degrees — the units an
# engineer states a requirement in, and the units every result already reports.
# The kernel's metres never surface here.
_MM = 1000.0

_AXES = {"x": 0, "y": 1, "z": 2}


class SpecError(ValueError):
    """The spec itself is malformed, as opposed to the part failing it."""


def _num(req: Dict[str, Any], key: str) -> Optional[float]:
    value = req.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise SpecError(f"requirement {req.get('id', '?')}: {key!r} must be a number")


def _range_check(label: str, actual: float, req: Dict[str, Any], unit: str,
                 ) -> Tuple[str, str]:
    """Compare a measured value against min/max/nominal±tol. Returns (status, detail).

    All three forms are accepted because all three are how requirements get
    written: "at least 2 mm", "no more than 200 g", "80 mm ±0.5".
    """
    low, high = _num(req, "min"), _num(req, "max")
    nominal, tol = _num(req, "value"), _num(req, "tolerance")
    if nominal is not None:
        span = tol if tol is not None else 0.0
        low, high = nominal - span, nominal + span
    if low is None and high is None:
        raise SpecError(
            f"requirement {req.get('id', '?')}: needs 'value' (with optional "
            "'tolerance'), 'min' or 'max'")

    within = (low is None or actual >= low - 1e-9) and (high is None or actual <= high + 1e-9)
    suffix = f" {unit}" if unit else ""
    if low is not None and high is not None:
        wanted = (f"exactly {low:g}{suffix}" if low == high
                  else f"{low:g}–{high:g}{suffix}")
    elif low is not None:
        wanted = f"≥ {low:g}{suffix}"
    else:
        wanted = f"≤ {high:g}{suffix}"
    return ("pass" if within else "fail",
            f"{label} {actual:.4g}{suffix}, required {wanted}")


# ── measurements ──
#
# Each returns (status, detail, value). They take the already-computed context
# so a spec with twenty requirements evaluates the body once.

def _c_bbox(req, ctx) -> Tuple[str, str, float]:
    axis = str(req.get("axis", "")).lower()
    if axis not in _AXES:
        raise SpecError(f"requirement {req.get('id', '?')}: 'axis' must be x, y or z")
    actual = ctx["size_mm"][_AXES[axis]]
    status, detail = _range_check(f"{axis.upper()} extent", actual, req, "mm")
    return status, detail, actual


def _c_mass(req, ctx) -> Tuple[str, str, float]:
    actual = ctx["mass_kg"] * 1000.0
    status, detail = _range_check("Mass", actual, req, "g")
    return status, detail, actual


def _c_volume(req, ctx) -> Tuple[str, str, float]:
    actual = ctx["volume_m3"] * 1e9 / 1000.0        # mm³ -> cm³
    status, detail = _range_check("Volume", actual, req, "cm³")
    return status, detail, actual


def _c_material(req, ctx) -> Tuple[str, str, Optional[float]]:
    wanted = req.get("value")
    if not wanted:
        raise SpecError(f"requirement {req.get('id', '?')}: 'value' must be a material id")
    actual = ctx["material_id"]
    return (("pass" if actual == wanted else "fail"),
            f"material is {actual or 'unset'}, required {wanted}", None)


def _c_watertight(req, ctx) -> Tuple[str, str, Optional[float]]:
    rows = {c.id: c for c in ctx["geometry"]}
    row = rows.get("watertight")
    if row is None:
        return "fail", "geometry check did not run", None
    return row.status, row.detail, row.value


def _c_bodies(req, ctx) -> Tuple[str, str, float]:
    rows = {c.id: c for c in ctx["geometry"]}
    row = rows.get("connectivity")
    count = float(row.value) if (row and row.value is not None) else float("nan")
    if math.isnan(count):
        return "fail", "connectivity check did not run", 0.0
    status, detail = _range_check("Connected bodies", count, req, "")
    return status, detail, count


def _c_hole(req, ctx) -> Tuple[str, str, float]:
    """Cylindrical faces of a given diameter — a hole the part actually has.

    Counted from topology, not from the feature tree: a HoleFeature that a later
    boolean swallowed is not a hole, and a bore that arrived inside an imported
    STEP body is one even though no operation created it.
    """
    faces = ctx.get("cylinders")
    if faces is None:
        return "fail", ctx.get("topology_error", "needs the exact kernel"), 0.0

    diameter = _num(req, "diameter")
    tol = _num(req, "diameter_tolerance")
    tol = 0.1 if tol is None else tol
    if diameter is None:
        matched = faces
        which = "any diameter"
    else:
        matched = [f for f in faces if abs(f["radius_mm"] * 2.0 - diameter) <= tol]
        which = f"Ø{diameter:g} ±{tol:g} mm"

    count = float(len(matched))
    wanted = req.get("count")
    if wanted is None:
        status = "pass" if matched else "fail"
        return status, f"{len(matched)} hole(s) at {which}", count
    status, detail = _range_check(f"Holes at {which}", count,
                                  {**req, "value": wanted, "tolerance": 0}, "")
    return status, detail, count


def _c_feature(req, ctx) -> Tuple[str, str, float]:
    wanted = req.get("value")
    if not wanted:
        raise SpecError(
            f"requirement {req.get('id', '?')}: 'value' must be a feature type")
    count = float(sum(1 for n in ctx["nodes"] if n.get("type") == wanted))
    minimum = _num(req, "min")
    minimum = 1.0 if minimum is None else minimum
    return (("pass" if count >= minimum else "fail"),
            f"{int(count)} × {wanted}, required at least {minimum:g}", count)


_CHECKS: Dict[str, Callable[..., Tuple[str, str, Optional[float]]]] = {
    "bounding_box": _c_bbox,
    "mass": _c_mass,
    "volume": _c_volume,
    "material": _c_material,
    "watertight": _c_watertight,
    "bodies": _c_bodies,
    "hole": _c_hole,
    "feature": _c_feature,
}

KINDS = tuple(sorted(_CHECKS))


def validate_spec(spec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Check a spec's shape before it is stored, so a bad one is refused early."""
    if spec is None:
        return {"requirements": []}
    if not isinstance(spec, dict):
        raise SpecError("a spec is an object with a 'requirements' list")

    requirements = spec.get("requirements")
    if requirements is None or not isinstance(requirements, list):
        raise SpecError("a spec needs a 'requirements' list")

    seen: set = set()
    cleaned: List[Dict[str, Any]] = []
    for index, raw in enumerate(requirements):
        if not isinstance(raw, dict):
            raise SpecError(f"requirement {index} is not an object")
        kind = raw.get("kind")
        if kind not in _CHECKS:
            raise SpecError(
                f"requirement {raw.get('id', index)}: unknown kind {kind!r}; "
                f"available: {', '.join(KINDS)}")
        req = dict(raw)
        req.setdefault("id", f"r{index + 1}")
        if req["id"] in seen:
            raise SpecError(f"duplicate requirement id {req['id']!r}")
        seen.add(req["id"])
        cleaned.append(req)

    out = {"requirements": cleaned}
    if spec.get("goal"):
        out["goal"] = str(spec["goal"])
    return out


def _context(doc) -> Dict[str, Any]:
    """Everything the checks need, measured once."""
    from app.cad_kernel.core.feature_tree import walk_nodes
    from app.services import document_service, geometry_check  # noqa: F401

    derived = doc.derived
    bbox = derived.bbox if derived else None
    if bbox is None:
        raise SpecError(
            "the document has no evaluated geometry to check"
            + (f": {derived.error}" if derived and derived.error else ""))

    size_mm = [(bbox.max.x - bbox.min.x) * _MM,
               (bbox.max.y - bbox.min.y) * _MM,
               (bbox.max.z - bbox.min.z) * _MM]

    ctx: Dict[str, Any] = {
        "size_mm": size_mm,
        "mass_kg": float(derived.mass or 0.0),
        "volume_m3": float(derived.volume or 0.0),
        "material_id": doc.material_id,
        "nodes": list(walk_nodes(doc.feature_tree)),
        "geometry": geometry_check.check_solid(document_service.get_solid(doc.id)),
        "cylinders": None,
        "topology_error": "",
    }

    # Topology is optional: a spec that never mentions holes should not require
    # the exact kernel, so the failure is recorded and surfaces only if used.
    try:
        from app.cad_kernel.core import naming
        from app.services import cad_operations

        table = naming.face_table(cad_operations._exact_shape(doc.id))
        ctx["cylinders"] = [f for f in table if f["kind"] == "cylinder"]
    except Exception as exc:
        ctx["topology_error"] = str(exc)
    return ctx


def verify(doc) -> Dict[str, Any]:
    """Score the document against its spec. This is the reward function."""
    spec = validate_spec(getattr(doc, "spec", None))
    requirements = spec["requirements"]
    if not requirements:
        raise SpecError(
            "this document has no design spec — set one with set_spec first")

    ctx = _context(doc)
    checks: List[DfmCheck] = []
    for req in requirements:
        label = str(req.get("label") or f"{req['kind']} requirement")
        try:
            status, detail, value = _CHECKS[req["kind"]](req, ctx)
        except SpecError as exc:
            status, detail, value = "fail", str(exc), None
        checks.append(DfmCheck(id=str(req["id"]), label=label, status=status,
                               detail=detail, value=value))

    passed = sum(1 for c in checks if c.status == "pass")
    return {
        "goal": spec.get("goal"),
        "checks": [c.model_dump() for c in checks],
        "passed": passed,
        "failed": len(checks) - passed,
        # The scalar an RL loop optimises. Deliberately the plain fraction: any
        # weighting belongs to whoever wrote the spec, not buried in here.
        "score": round(passed / len(checks), 6),
        "met": passed == len(checks),
    }
