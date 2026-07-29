"""Stable names for faces and edges — the topological naming problem, pragmatically.

OCC hands out `TopoDS_Face` and `TopoDS_Edge` objects, but their identity does not
survive a rebuild: change a dimension and re-evaluate, and the "same" face is a
different object in a different position in the exploration order. Referring to
topology by index is therefore wrong the moment anything upstream changes, which
is the failure every CAD kernel has to solve and which FreeCAD has fought for a
decade.

The scheme here names an entity by *what it is* rather than where it appears: the
surface or curve kind, its size, and its position **normalised to the body's
bounding box** — 0 at the centre, ±1 at the faces. That last part is what makes
it work. Stretch a plate from 80 mm to 120 mm and its corner edges move 20 mm in
absolute terms, which no sane tolerance accepts; in normalised terms they have
not moved at all, so a fillet placed on them stays where it was put.

Three properties follow.

* An entity untouched by an edit keeps its name, because none of those
  measurements moved.
* An entity carried along by a resize is still matched, because the measurement
  that matters is relative.
* An entity that genuinely changed size beyond `_SIZE_TOL` is *not* matched, so a
  20 mm bore that becomes 21 mm is still the same bore while one that becomes
  60 mm is treated as a different feature.

This is weaker than a full generative-history naming scheme (following faces
through `BRepAlgoAPI`'s Generated/Modified maps), and it is honest about that: a
reference that cannot be matched within tolerance is reported as unresolvable
rather than quietly snapping to whatever was nearest.

Ids are content-derived, so the same body always produces the same names — the
same property the feature-id allocator provides, and for the same reason: an
operation sequence that references `F3` is only replayable if `F3` means the same
thing every time.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Tuple

MM = 1000.0

# How close two descriptors must be to count as the same entity, in *normalised*
# body coordinates: 0 is the centre of the bounding box and ±1 its faces. Matching
# on absolute position instead would be useless for the case that matters most —
# stretch a plate from 80 to 120 mm and its corner edges move 20 mm, which no
# sane absolute tolerance accepts, yet they are obviously still the same corners.
# Normalised, they do not move at all.
_MATCH_TOL = 0.08

# Relative size change tolerated on top of that. A 20 mm bore that becomes 21 mm
# is the same bore; one that becomes 60 mm is a design decision, not a nudge.
_SIZE_TOL = 0.5


def _round(value: float, places: int = 6) -> float:
    return round(float(value) + 0.0, places)  # +0.0 folds -0.0


def _name(prefix: str, payload: str) -> str:
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]
    return f"{prefix}{digest}"


# ------------------------------------------------------------------ faces --

def body_bounds(shape) -> Tuple[List[float], List[float]]:
    """(centre, half-extent) of the body, for normalising positions."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    centre = [(xmin + xmax) / 2, (ymin + ymax) / 2, (zmin + zmax) / 2]
    half = [max((xmax - xmin) / 2, 1e-9), max((ymax - ymin) / 2, 1e-9),
            max((zmax - zmin) / 2, 1e-9)]
    return centre, half


def _normalised(point: List[float], bounds) -> List[float]:
    """A position in body coordinates: 0 at the centre, ±1 at the bounding faces.

    This is what makes a reference survive a resize. A corner edge of an 80 mm
    plate and of the same plate stretched to 120 mm sit at the same normalised
    place, so they match; their absolute positions differ by 20 mm, so they
    would not.
    """
    if bounds is None:
        return [0.0, 0.0, 0.0]
    centre, half = bounds
    return [_round((point[i] - centre[i]) / half[i], 4) for i in range(3)]


def face_descriptor(face, bounds=None) -> Dict[str, Any]:
    """What a face is, measured rather than counted."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp
    from OCP.GeomAbs import (
        GeomAbs_SurfaceType,
    )
    from OCP.GProp import GProp_GProps

    adaptor = BRepAdaptor_Surface(face)
    kind_enum = adaptor.GetType()
    kind = {
        GeomAbs_SurfaceType.GeomAbs_Plane: "plane",
        GeomAbs_SurfaceType.GeomAbs_Cylinder: "cylinder",
        GeomAbs_SurfaceType.GeomAbs_Cone: "cone",
        GeomAbs_SurfaceType.GeomAbs_Sphere: "sphere",
        GeomAbs_SurfaceType.GeomAbs_Torus: "torus",
        GeomAbs_SurfaceType.GeomAbs_BSplineSurface: "bspline",
        GeomAbs_SurfaceType.GeomAbs_BezierSurface: "bezier",
    }.get(kind_enum, "other")

    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    area = float(props.Mass())
    com = props.CentreOfMass()
    centre = [_round(com.X()), _round(com.Y()), _round(com.Z())]

    descriptor: Dict[str, Any] = {
        "kind": kind,
        "area": _round(area, 9),
        "center": centre,
        "area_mm2": round(area * MM * MM, 4),
        "center_mm": [round(c * MM, 4) for c in centre],
    }

    if kind == "plane":
        axis = adaptor.Plane().Axis().Direction()
        descriptor["normal"] = [_round(axis.X(), 4), _round(axis.Y(), 4), _round(axis.Z(), 4)]
    elif kind == "cylinder":
        cylinder = adaptor.Cylinder()
        descriptor["radius"] = _round(cylinder.Radius(), 9)
        descriptor["radius_mm"] = round(cylinder.Radius() * MM, 4)
        axis = cylinder.Axis().Direction()
        descriptor["axis"] = [_round(axis.X(), 4), _round(axis.Y(), 4), _round(axis.Z(), 4)]
    elif kind == "sphere":
        descriptor["radius"] = _round(adaptor.Sphere().Radius(), 9)
        descriptor["radius_mm"] = round(adaptor.Sphere().Radius() * MM, 4)
    elif kind == "cone":
        descriptor["radius"] = _round(adaptor.Cone().RefRadius(), 9)
        descriptor["half_angle"] = _round(adaptor.Cone().SemiAngle(), 6)

    descriptor["at"] = _normalised(centre, bounds)
    descriptor["id"] = _name("F", f"{kind}|{descriptor['area']}|{centre}|"
                                  f"{descriptor.get('normal')}|{descriptor.get('radius')}")
    return descriptor


# ------------------------------------------------------------------ edges --

def edge_descriptor(edge, bounds=None) -> Dict[str, Any]:
    """What an edge is. Length and midpoint locate it; the curve kind types it."""
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepGProp import BRepGProp
    from OCP.GeomAbs import GeomAbs_CurveType
    from OCP.GProp import GProp_GProps

    adaptor = BRepAdaptor_Curve(edge)
    kind = {
        GeomAbs_CurveType.GeomAbs_Line: "line",
        GeomAbs_CurveType.GeomAbs_Circle: "circle",
        GeomAbs_CurveType.GeomAbs_Ellipse: "ellipse",
        GeomAbs_CurveType.GeomAbs_BSplineCurve: "bspline",
        GeomAbs_CurveType.GeomAbs_BezierCurve: "bezier",
    }.get(adaptor.GetType(), "other")

    props = GProp_GProps()
    BRepGProp.LinearProperties_s(edge, props)
    length = float(props.Mass())

    mid_param = (adaptor.FirstParameter() + adaptor.LastParameter()) / 2.0
    point = adaptor.Value(mid_param)
    midpoint = [_round(point.X()), _round(point.Y()), _round(point.Z())]

    descriptor: Dict[str, Any] = {
        "kind": kind,
        "length": _round(length, 9),
        "midpoint": midpoint,
        "length_mm": round(length * MM, 4),
        "midpoint_mm": [round(c * MM, 4) for c in midpoint],
    }
    if kind == "circle":
        descriptor["radius"] = _round(adaptor.Circle().Radius(), 9)
        descriptor["radius_mm"] = round(adaptor.Circle().Radius() * MM, 4)

    descriptor["at"] = _normalised(midpoint, bounds)
    descriptor["id"] = _name("E", f"{kind}|{descriptor['length']}|{midpoint}|"
                                  f"{descriptor.get('radius')}")
    return descriptor


# ------------------------------------------------------------ exploration --

def _explore(shape, kind):
    """Sub-shapes of one type, de-duplicated and in a deterministic order.

    TopExp_Explorer visits a shared face once per parent, so the same face comes
    back several times on any non-trivial solid; `TopTools_IndexedMapOfShape`
    exists precisely to collapse that.
    """
    from OCP.TopExp import TopExp
    from OCP.TopTools import TopTools_IndexedMapOfShape

    mapping = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, mapping)
    return [mapping.FindKey(i) for i in range(1, mapping.Extent() + 1)]


def faces(shape) -> List[Any]:
    # Downcast: the OCC adaptors take a TopoDS_Face, and an explorer yields the
    # base TopoDS_Shape.
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopoDS import TopoDS

    return [TopoDS.Face_s(s) for s in _explore(shape, TopAbs_ShapeEnum.TopAbs_FACE)]


def edges(shape) -> List[Any]:
    from OCP.TopAbs import TopAbs_ShapeEnum
    from OCP.TopoDS import TopoDS

    return [TopoDS.Edge_s(s) for s in _explore(shape, TopAbs_ShapeEnum.TopAbs_EDGE)]


def face_table(shape) -> List[Dict[str, Any]]:
    """Every face with its name, sorted so the listing is stable run to run."""
    bounds = body_bounds(shape)
    return sorted([face_descriptor(f, bounds) for f in faces(shape)], key=_sort_key)


def edge_table(shape) -> List[Dict[str, Any]]:
    bounds = body_bounds(shape)
    return sorted([edge_descriptor(e, bounds) for e in edges(shape)], key=_sort_key)


def _sort_key(descriptor: Dict[str, Any]) -> Tuple:
    """Largest first, then by position — a readable order, not exploration order."""
    size = descriptor.get("area", descriptor.get("length", 0.0))
    where = descriptor.get("center", descriptor.get("midpoint", [0, 0, 0]))
    return (-size, where[0], where[1], where[2], descriptor["id"])


# -------------------------------------------------------------- resolving --

def _distance(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[float]:
    """How far apart two descriptors are, or None if they are not comparable."""
    if a.get("kind") != b.get("kind"):
        return None
    pa, pb = a.get("at"), b.get("at")
    if not pa or not pb:
        return None

    size_a = a.get("area", a.get("length", 0.0))
    size_b = b.get("area", b.get("length", 0.0))
    relative_size = abs(size_a - size_b) / max(size_a, size_b, 1e-12)
    if relative_size > _SIZE_TOL:
        return None  # too different to be the same feature, wherever it sits

    positional = sum((x - y) ** 2 for x, y in zip(pa, pb)) ** 0.5
    # Position dominates; size only breaks ties between equally-placed candidates.
    return positional + relative_size * _MATCH_TOL * 0.5


def resolve(shape, references: List[Dict[str, Any]], kind: str = "edge"):
    """Match stored references against the current body.

    Returns ``(matched_subshapes, unresolved_references)``. Exact id matches are
    taken first; anything left over is matched to the nearest descriptor of the
    same kind within tolerance, so a reference survives the upstream dimension
    change that moved what it points at. Whatever still cannot be matched is
    handed back rather than guessed at — a fillet silently applied to the wrong
    edge is worse than one that reports it lost its target.
    """
    explore_fn = faces if kind == "face" else edges
    describe = face_descriptor if kind == "face" else edge_descriptor

    # Describe each sub-shape once and keep both the table and the id -> shape
    # mapping from that single pass, so the two can never disagree.
    bounds = body_bounds(shape)
    subshapes: Dict[str, Any] = {}
    current: List[Dict[str, Any]] = []
    for sub in explore_fn(shape):
        descriptor = describe(sub, bounds)
        subshapes[descriptor["id"]] = sub
        current.append(descriptor)

    by_id = {d["id"]: d for d in current}
    matched, unresolved, used = [], [], set()

    for reference in references:
        ref_id = reference.get("id") if isinstance(reference, dict) else str(reference)
        if ref_id in by_id and ref_id not in used:
            matched.append(subshapes[ref_id])
            used.add(ref_id)
            continue

        if not isinstance(reference, dict) or len(reference) <= 1:
            unresolved.append(reference)
            continue

        best, best_distance = None, None
        for descriptor in current:
            if descriptor["id"] in used:
                continue
            distance = _distance(reference, descriptor)
            if distance is None:
                continue
            if best_distance is None or distance < best_distance:
                best, best_distance = descriptor, distance

        if best is not None and best_distance is not None and best_distance <= _MATCH_TOL:
            matched.append(subshapes[best["id"]])
            used.add(best["id"])
        else:
            unresolved.append(reference)

    return matched, unresolved
