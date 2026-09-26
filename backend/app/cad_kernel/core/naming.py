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

Descriptors are no longer the whole story. `core/history` follows entities through
`BRepAlgoAPI`'s Modified/Generated/IsDeleted maps and gives each one a **tag** — an
identity minted by the feature that created it, rather than a measurement of what
it currently looks like. Where a tag is available it is tried first, because it
answers the question descriptors cannot: an edge a later cut split in two is
*both* segments, not the nearer one. Descriptor matching remains the fallback and
is not going anywhere — it is the only thing that works for a `BrepBodyFeature`
read from a STEP file, which has no construction history by definition, and for
references stored before tags existed.

A reference that cannot be matched within tolerance is reported as unresolvable
rather than quietly snapping to whatever was nearest; one the history says was
destroyed is reported *deleted*, and is not offered to descriptor matching at all,
because "the face you picked is gone" and "I could not find the face you picked"
call for different fixes.

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

class EmptyBody(ValueError):
    """No topology to name, because there is no body.

    A `ValueError` so the routers' existing handlers keep working, in the same
    shape `op_errors` uses.
    """


def body_bounds(shape) -> Tuple[List[float], List[float]]:
    """(centre, half-extent) of the body, for normalising positions."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    # **An empty body has no bounds, and `Bnd_Box.Get` raises about itself.** A
    # subtract that removes everything leaves a shape with nothing in it, and
    # every descriptor here is normalised to these bounds — so the whole naming
    # scheme arrives at `Standard_ConstructionError: Bnd_Box is void`, which
    # names neither the body nor the operation that asked. Reachable in three
    # calls: a box, a bigger box subtracted, then a fillet.
    if box.IsVoid():
        raise EmptyBody("this body is empty — there is no topology to name")
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

def edge_polyline(edge, samples: int = 2) -> List[List[float]]:
    """Points along the edge, in millimetres — the curve, sampled.

    Only the viewport wants this: it is what lets a click near an edge resolve to
    *that* edge by exact point-to-segment distance rather than by comparing
    midpoints, which puts a long edge and a short one at the same remove from a
    click that is plainly on one of them. Kept off the default listing because
    `list_edges` is also what an agent reads, and 16 points per hole rim is
    noise in a prompt.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GeomAbs import GeomAbs_CurveType

    adaptor = BRepAdaptor_Curve(edge)
    if adaptor.GetType() != GeomAbs_CurveType.GeomAbs_Line:
        samples = max(samples, 17)
    first, last = adaptor.FirstParameter(), adaptor.LastParameter()
    steps = max(int(samples), 2)
    out = []
    for i in range(steps):
        point = adaptor.Value(first + (last - first) * i / (steps - 1))
        out.append([round(point.X() * MM, 4), round(point.Y() * MM, 4),
                    round(point.Z() * MM, 4)])
    return out


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


def as_kind(sub, kind: str):
    """Downcast a bare `TopoDS_Shape` to the face or edge type OCC's API wants.

    Anything arriving from a history map — `Modified`, `Generated`, a shape map —
    is the base class, and `BRepFilletAPI_MakeFillet.Add` will not take it.
    """
    from OCP.TopoDS import TopoDS

    return TopoDS.Face_s(sub) if kind == "face" else TopoDS.Edge_s(sub)


def face_table(shape, entities=None) -> List[Dict[str, Any]]:
    """Every face with its name, sorted so the listing is stable run to run.

    ``entities`` is the build's `history.EntityMap`, when there is one. Each
    descriptor then also carries the ``tag`` its entity was created under, which
    is what makes a stored selection followable through a later re-topologising
    edit rather than merely re-measurable.
    """
    return _table(shape, faces(shape), face_descriptor, entities, "face")


def edge_table(shape, entities=None) -> List[Dict[str, Any]]:
    return _table(shape, edges(shape), edge_descriptor, entities, "edge")


def _table(shape, subshapes, describe, entities, kind: str) -> List[Dict[str, Any]]:
    bounds = body_bounds(shape)
    table = []
    for sub in subshapes:
        descriptor = describe(sub, bounds)
        tag = entities.tag_of(sub) if entities is not None else None
        if tag is not None:
            descriptor["tag"] = tag
        table.append(descriptor)
    return sorted(table, key=_sort_key)


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


def resolve(shape, references: List[Dict[str, Any]], kind: str = "edge",
            entities=None):
    """Match stored references against the current body.

    Returns ``(matched_subshapes, unresolved_references)`` — the shape of the
    answer the B-rep builders want. `resolve_references` is the same work with the
    per-reference verdict kept, which is what anything reporting to a caller
    should use.
    """
    outcomes = resolve_references(shape, references, kind=kind, entities=entities)
    matched = [sub for outcome in outcomes for sub in outcome["shapes"]]
    unresolved = [o["reference"] for o in outcomes if not o["shapes"]]
    return matched, unresolved


def resolve_references(shape, references: List[Dict[str, Any]], kind: str = "edge",
                       entities=None) -> List[Dict[str, Any]]:
    """What each stored reference resolves to on the current body, and how.

    One row per reference: ``{reference, id, status, shapes}``, where ``status``
    is one of

    ``matched``     one entity, the ordinary case;
    ``split``       several — an upstream edit divided what this named, and the
                    reference now correctly names every piece;
    ``deleted``     the history says an upstream edit destroyed it;
    ``unresolved``  nothing on this body is it, within tolerance.

    Order of attempts is history, then exact id, then nearest descriptor.
    ``deleted`` deliberately short-circuits the fallbacks: once the construction
    history has said an entity was consumed, letting a descriptor find "something
    a bit like it nearby" is exactly the silent mis-selection this module exists
    to prevent.
    """
    from OCP.TopTools import TopTools_IndexedMapOfShape

    explore_fn = faces if kind == "face" else edges
    describe = face_descriptor if kind == "face" else edge_descriptor

    # Describe each sub-shape once and keep the table, the id -> shape mapping and
    # the shape -> id mapping from that single pass, so none of the three can
    # disagree with the others.
    bounds = body_bounds(shape)
    subshapes: Dict[str, Any] = {}
    current: List[Dict[str, Any]] = []
    index = TopTools_IndexedMapOfShape()
    id_at: Dict[int, str] = {}
    for sub in explore_fn(shape):
        descriptor = describe(sub, bounds)
        subshapes[descriptor["id"]] = sub
        current.append(descriptor)
        id_at[index.Add(sub)] = descriptor["id"]

    by_id = {d["id"]: d for d in current}
    by_tag = entities.entities(shape, kind) if entities is not None else {}
    removed = entities.removed if entities is not None else set()

    outcomes: List[Dict[str, Any]] = []
    used: set = set()

    def claim(sub) -> None:
        found = id_at.get(index.FindIndex(sub))
        if found:
            used.add(found)

    for reference in references:
        ref = reference if isinstance(reference, dict) else {"id": str(reference)}
        ref_id = ref.get("id")
        row = {"reference": reference, "id": ref_id}

        tag = ref.get("tag")
        if tag:
            following = [as_kind(s, kind) for s in (by_tag.get(tag) or [])]
            if following:
                for sub in following:
                    claim(sub)
                outcomes.append({**row, "shapes": following,
                                 "status": "split" if len(following) > 1 else "matched"})
                continue
            if tag in removed:
                outcomes.append({**row, "shapes": [], "status": "deleted"})
                continue
            # No tag on this body and nothing said it was destroyed: the history
            # has a gap (not every builder reports what happened to every
            # sub-shape), so fall through to measuring.

        if ref_id in by_id and ref_id not in used:
            used.add(ref_id)
            outcomes.append({**row, "shapes": [subshapes[ref_id]], "status": "matched"})
            continue

        if len(ref) <= 1:
            outcomes.append({**row, "shapes": [], "status": "unresolved"})
            continue

        best, best_distance = None, None
        for descriptor in current:
            if descriptor["id"] in used:
                continue
            distance = _distance(ref, descriptor)
            if distance is None:
                continue
            if best_distance is None or distance < best_distance:
                best, best_distance = descriptor, distance

        if best is not None and best_distance is not None and best_distance <= _MATCH_TOL:
            used.add(best["id"])
            outcomes.append({**row, "shapes": [subshapes[best["id"]]], "status": "matched"})
        else:
            outcomes.append({**row, "shapes": [], "status": "unresolved"})

    return outcomes
