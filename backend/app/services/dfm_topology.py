"""Manufacturability measured from the body, not from the mesh that renders it.

The existing DFM report answers three questions about an LPBF build, and one of
its answers is not about the part at all. `feature_size` reports the shortest
edge of a marching-cubes mesh — which is a property of the sampling grid, so it
changes when `resolution` changes and says nothing about whether the wall is
thick enough to print. The other checks (overhang, support) are sound: face
normals against a build direction survive tessellation fine.

What tessellation cannot answer is anything that needs to know where the *other
side* of a wall is, or whether a tool can reach a face. Those need rays cast
through a real solid, so they live here, on the exact kernel:

* **Wall thickness** — from each face sample, a ray inward to the first exit.
  That is the local thickness, and its minimum is the number a process spec is
  written against ("0.8 mm minimum wall").
* **Draft** — the angle each face makes with the mould's pull direction. A
  vertical wall is 0° draft: it will not release, and no mesh statistic notices.
* **Undercuts** — a face that faces the pull direction but has material in front
  of it along that direction. Two-part moulds cannot make these at all.
* **Tool access** — for 3-axis milling, a face is reachable if a ray from it
  along some approach direction escapes the solid. Faces reachable from none of
  the six directions need 5-axis or EDM, which is a quote, not a warning.
* **Trapped voids** — an inner shell is a cavity with no way out. Exact and
  cheap: it is a shell count, not a search.

Every measurement is a length or an angle on the actual geometry, so it means
the same thing at any resolution. Where a body cannot be built exactly, the
caller keeps the mesh checks and this contributes nothing rather than guessing.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.cad_kernel.evaluator import brep
from app.models.schemas import DfmCheck

_MM = 1000.0

# Ray tolerance and the shortest hit that counts as "the other side". A ray
# leaving a face re-finds that same face at ~0 distance; anything under a
# micron is that self-hit rather than an opposing wall.
_RAY_TOL = 1e-7
_MIN_HIT = 1e-6

# Rays are cast far enough to leave any part this kernel handles (metres).
_RAY_LENGTH = 100.0

# Escape rays start this far outside the face they leave. A ray running *along*
# a surface it starts on is tangent to it, and OCC reports the grazing contact
# as a hit — which called the wall of a plain through-bore unreachable, since
# every vertical ray from it grazed the bore. One micron off the surface and the
# ray is unambiguously in free space.
_RAY_OFFSET = 1e-5

# Below this a face is too small to bother sampling for thickness — it is a
# sliver between two real faces, and its own "thickness" is meaningless.
_MIN_SAMPLE_AREA = 1e-10        # m², a 10 µm square

# A face whose normal is within this of perpendicular to the pull direction is
# a side wall: it is the draft check's subject, not the undercut check's.
_PERPENDICULAR_TOL = math.radians(0.5)

_AXES: Dict[str, Tuple[float, float, float]] = {
    "+X": (1.0, 0.0, 0.0), "-X": (-1.0, 0.0, 0.0),
    "+Y": (0.0, 1.0, 0.0), "-Y": (0.0, -1.0, 0.0),
    "+Z": (0.0, 0.0, 1.0), "-Z": (0.0, 0.0, -1.0),
}

PROCESSES = ("milling", "molding", "casting", "additive")


class _Sample:
    """One point on a face, with the outward normal and the area it stands for."""

    __slots__ = ("point", "normal", "area", "face_index")

    def __init__(self, point, normal, area, face_index):
        self.point = point
        self.normal = normal
        self.area = area
        self.face_index = face_index


def available() -> bool:
    return brep.AVAILABLE


def _mesh(shape, deflection: float) -> None:
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepTools import BRepTools

    BRepTools.Clean_s(shape)
    BRepMesh_IncrementalMesh(shape, deflection, False, 0.5, True)


def _faces(shape) -> List[Any]:
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    out, exp = [], TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        out.append(TopoDS.Face_s(exp.Current()))
        exp.Next()
    return out


def _samples(shape, faces: Sequence[Any], deflection: float) -> List[_Sample]:
    """One sample per triangle, placed *on the surface* with its true normal.

    The triangulation decides only *where* to sample. Using it for the position
    and normal as well is what a first attempt does, and it is wrong on any
    curved face: a chord across a convex surface lies **inside the material**, so
    every escape ray from such a sample starts buried and reports the face
    unreachable — the outer wall of a plain tube came back as 50% of the part
    needing 5-axis. The chord normal is skewed for the same reason, which pulled
    a measured 0.4 mm wall down to 0.34 mm.

    So the (u, v) centroid of each triangle is evaluated on the real surface
    instead. Sampling per triangle still sidesteps trimming entirely — every
    triangle is inside the face by construction, so no sample lands in a hole.
    """
    from OCP.BRep import BRep_Tool
    from OCP.BRepGProp import BRepGProp_Face
    from OCP.gp import gp_Pnt, gp_Vec
    from OCP.TopLoc import TopLoc_Location

    _mesh(shape, deflection)
    out: List[_Sample] = []
    for index, face in enumerate(faces):
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is None:
            continue
        transform = location.Transformation()
        has_uv = triangulation.HasUVNodes()
        prop = BRepGProp_Face(face) if has_uv else None

        nodes = np.array([
            [(p := triangulation.Node(i).Transformed(transform)).X(), p.Y(), p.Z()]
            for i in range(1, triangulation.NbNodes() + 1)])
        for t in range(1, triangulation.NbTriangles() + 1):
            a, b, c = triangulation.Triangle(t).Get()
            p0, p1, p2 = nodes[a - 1], nodes[b - 1], nodes[c - 1]
            cross = np.cross(p1 - p0, p2 - p0)
            norm = float(np.linalg.norm(cross))
            area = 0.5 * norm
            if area < _MIN_SAMPLE_AREA:
                continue

            point = normal = None
            if prop is not None:
                uv = [triangulation.UVNode(i) for i in (a, b, c)]
                u = sum(n.X() for n in uv) / 3.0
                v = sum(n.Y() for n in uv) / 3.0
                surface_point, surface_normal = gp_Pnt(), gp_Vec()
                try:
                    # BRepGProp_Face applies the face's own orientation, so this
                    # already points out of the material.
                    prop.Normal(u, v, surface_point, surface_normal)
                    vector = np.array([surface_normal.X(), surface_normal.Y(),
                                       surface_normal.Z()])
                    length = float(np.linalg.norm(vector))
                    if length > 0.0:
                        moved = surface_point.Transformed(transform)
                        point = np.array([moved.X(), moved.Y(), moved.Z()])
                        normal = vector / length
                except Exception:
                    point = normal = None

            if point is None:  # no UV nodes: a planar face, where the chord is exact
                point, normal = (p0 + p1 + p2) / 3.0, cross / norm
                if face.Orientation() == _reversed():
                    normal = -normal
            out.append(_Sample(point, normal, area, index))
    return out


def _reversed():
    from OCP.TopAbs import TopAbs_REVERSED

    return TopAbs_REVERSED


class _Rays:
    """Ray casts against one shape, loaded once."""

    def __init__(self, shape):
        from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector

        self._inter = IntCurvesFace_ShapeIntersector()
        self._inter.Load(shape, _RAY_TOL)

    def _cast(self, origin, direction, start: float):
        from OCP.gp import gp_Dir, gp_Lin, gp_Pnt

        line = gp_Lin(gp_Pnt(*(float(v) for v in origin)),
                      gp_Dir(*(float(v) for v in direction)))
        self._inter.Perform(line, start, _RAY_LENGTH)
        return self._inter.IsDone()

    def hits(self, origin, direction, start: float = _MIN_HIT) -> List[float]:
        """Distances along `direction` at which the ray meets the solid."""
        if not self._cast(origin, direction, start):
            return []
        return sorted(self._inter.WParameter(i)
                      for i in range(1, self._inter.NbPnt() + 1))

def wall_thickness(shape, samples: Sequence[_Sample], rays: _Rays) -> Dict[str, Any]:
    """Minimum and mean local wall thickness, in metres.

    From each sample a ray goes *into* the material along the inward normal; the
    first surface it meets is the far side of the wall.

    A first attempt here filtered those hits, keeping only faces roughly
    anti-parallel to the ray, on the theory that a sample near an edge would
    otherwise pick up the perpendicular neighbour microns away. Measured across
    nine shapes — plates, an L bracket, a 1.2 mm rib, a chamfered plate, a 1 mm
    spherical shell, a narrow slot, a blind pocket, a tapered boss — the filter
    never changed a single reading, because a sample placed *on the surface* with
    the *true* normal does not aim at its neighbours in the first place. It was
    guarding against a symptom of the sampling bug, so it is gone.
    """
    thicknesses = [hits[0] for sample in samples
                   if (hits := rays.hits(sample.point, -sample.normal))]
    if not thicknesses:
        return {"min": None, "mean": None, "samples": 0}
    array = np.asarray(thicknesses)
    return {"min": float(array.min()), "mean": float(array.mean()),
            "samples": int(array.size)}


def draft(shape, samples: Sequence[_Sample], pull) -> Dict[str, Any]:
    """Smallest draft angle over the faces a mould has to slide past.

    Draft is measured from the pull direction: a face parallel to the pull
    (a vertical wall) is 0°, a face perpendicular to it (the top) is 90° and is
    not a draft concern at all. Only the walls are scored, weighted by area so a
    sliver does not decide the answer for the whole part.
    """
    pull = np.asarray(pull, dtype=float)
    pull = pull / np.linalg.norm(pull)

    worst, worst_area = None, 0.0
    for sample in samples:
        # Angle between the face and the pull direction, in [0°, 90°].
        along = abs(float(np.dot(sample.normal, pull)))
        angle = math.degrees(math.asin(min(1.0, along)))
        if angle > 80.0:
            continue                       # a top or bottom face, not a wall
        if worst is None or angle < worst:
            worst, worst_area = angle, sample.area
        elif abs(angle - worst) < 1e-9:
            worst_area += sample.area
    return {"min_deg": worst, "area": worst_area}


def undercuts(shape, samples: Sequence[_Sample], rays: _Rays, pull) -> Dict[str, Any]:
    """Area that a two-part mould pulled along `pull` cannot release.

    A face is released along +pull if its outward normal has any component that
    way *and* nothing is in the way — so the test is a ray along the pull
    direction from the face itself. Material in front of it means the mould has
    to travel through solid to get out, which it cannot.
    """
    pull = np.asarray(pull, dtype=float)
    pull = pull / np.linalg.norm(pull)

    trapped, total = 0.0, 0.0
    for sample in samples:
        along = float(np.dot(sample.normal, pull))
        if abs(along) < math.sin(_PERPENDICULAR_TOL):
            continue                       # a side wall: draft's problem, not this one
        total += sample.area
        direction = pull if along > 0 else -pull
        if rays.hits(sample.point + sample.normal * _RAY_OFFSET, direction, start=0.0):
            trapped += sample.area
    return {"trapped_area": trapped, "checked_area": total}


def tool_access(shape, samples: Sequence[_Sample], rays: _Rays,
                axes: Sequence[str] = ("+X", "-X", "+Y", "-Y", "+Z", "-Z")) -> Dict[str, Any]:
    """Area no 3-axis approach direction can reach.

    A face is reachable from a direction when a ray leaving it that way clears
    the solid, and when the tool is not coming in from behind it. Note that a
    wall *parallel* to the tool axis counts as reachable: an end mill cuts with
    its flank, so the wall of a through-bore is machined by a cutter travelling
    down the bore, not by one pointing at the wall. Requiring the normal to face
    the tool outright called a plain drilled hole unreachable.

    Everything left over needs 5-axis, EDM or another setup; that is a quoting
    decision, so it is reported as an area, not a verdict.
    """
    directions = [np.asarray(_AXES[a], dtype=float) for a in axes if a in _AXES]
    blocked, total = 0.0, 0.0
    for sample in samples:
        total += sample.area
        origin = sample.point + sample.normal * _RAY_OFFSET
        for direction in directions:
            if float(np.dot(sample.normal, direction)) < 0.0:
                continue                   # the tool would come in behind the face
            if not rays.hits(origin, direction, start=0.0):
                break                      # clears the part: reachable
        else:
            blocked += sample.area
    return {"blocked_area": blocked, "total_area": total}


def trapped_voids(shape) -> int:
    """Internal cavities, counted as inner shells — exact, and not a search."""
    from OCP.TopAbs import TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer

    voids, solid_exp = 0, TopExp_Explorer(shape, TopAbs_SOLID)
    while solid_exp.More():
        shells, shell_exp = 0, TopExp_Explorer(solid_exp.Current(), TopAbs_SHELL)
        while shell_exp.More():
            shells += 1
            shell_exp.Next()
        voids += max(0, shells - 1)
        solid_exp.Next()
    return voids


def small_features(shape, faces: Sequence[Any]) -> Dict[str, Any]:
    """Shortest real edge, smallest face, smallest bore — all from the B-rep.

    This is what `feature_size` was reaching for. Measuring it on the mesh gave
    the grid spacing; measuring it here gives the part.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepGProp import BRepGProp
    from OCP.GCPnts import GCPnts_AbscissaPoint
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_EDGE
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS

    from OCP.BRep import BRep_Tool

    shortest = None
    exp = TopExp_Explorer(shape, TopAbs_EDGE)
    while exp.More():
        edge = TopoDS.Edge_s(exp.Current())
        # A sphere's poles and a cone's apex carry degenerate edges of zero
        # length. They are bookkeeping, not geometry, and reporting one as the
        # part's smallest feature is how "0.00 mm" gets onto a DFM report.
        if BRep_Tool.Degenerated_s(edge):
            exp.Next()
            continue
        try:
            length = GCPnts_AbscissaPoint.Length_s(BRepAdaptor_Curve(edge))
        except Exception:
            length = None
        if length and (shortest is None or length < shortest):
            shortest = float(length)
        exp.Next()

    smallest_face, smallest_bore = None, None
    for face in faces:
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        area = float(props.Mass())
        if area > 0 and (smallest_face is None or area < smallest_face):
            smallest_face = area

    from app.cad_kernel.core import naming

    for entry in naming.face_table(shape):
        radius = entry.get("radius_mm")
        if radius and (smallest_bore is None or radius < smallest_bore):
            smallest_bore = float(radius)

    return {"min_edge_m": shortest, "min_face_area_m2": smallest_face,
            "min_bore_diameter_mm": None if smallest_bore is None else smallest_bore * 2.0}


# ── the report ──
#
# Limits are the conventional ones for each process. They are named constants
# rather than magic numbers in a comparison so a caller can see what standard
# a "fail" is a failure against.

_MIN_WALL_M = {"milling": 0.0008, "molding": 0.001, "casting": 0.003, "additive": 0.0004}
_MIN_DRAFT_DEG = {"molding": 0.5, "casting": 1.0}
_MIN_BORE_MM = {"milling": 1.0, "molding": 1.0, "casting": 3.0, "additive": 0.5}


def _mm(value: Optional[float]) -> str:
    return "—" if value is None else f"{value * _MM:.2f} mm"


def checks(shape, process: str = "milling", axis: str = "+Z",
           deflection: float = 0.0005) -> List[DfmCheck]:
    """Manufacturability of an exact body, as DfmCheck rows.

    `axis` is the mould pull direction for molding/casting and the primary tool
    approach for milling. It does not affect wall thickness or bore size, which
    are properties of the part however it is set up.
    """
    if not brep.AVAILABLE:
        raise ValueError("topology DFM needs the exact kernel (pip install cadquery-ocp)")
    process = process.lower()
    if process not in PROCESSES:
        raise ValueError(f"unknown process {process!r}; one of {', '.join(PROCESSES)}")
    if axis not in _AXES:
        raise ValueError(f"unknown axis {axis!r}; one of {', '.join(_AXES)}")

    faces = _faces(shape)
    points = _samples(shape, faces, deflection)
    rays = _Rays(shape)
    rows: List[DfmCheck] = []

    wall = wall_thickness(shape, points, rays)
    limit = _MIN_WALL_M[process]
    if wall["min"] is None:
        rows.append(DfmCheck(
            id="wall_thickness", label="Minimum wall thickness", status="warn",
            detail="no enclosed wall found — the body is solid stock"))
    else:
        rows.append(DfmCheck(
            id="wall_thickness", label="Minimum wall thickness",
            status="pass" if wall["min"] >= limit else "fail",
            detail=f"{_mm(wall['min'])} (limit {_mm(limit)} for {process}), "
                   f"{wall['samples']} samples",
            value=round(wall["min"] * _MM, 4)))

    small = small_features(shape, faces)
    bore_limit = _MIN_BORE_MM[process]
    bore = small["min_bore_diameter_mm"]
    rows.append(DfmCheck(
        id="min_bore", label="Smallest bore",
        status="pass" if bore is None or bore >= bore_limit else "fail",
        detail="no cylindrical faces" if bore is None
               else f"Ø{bore:.2f} mm (limit Ø{bore_limit:g} mm for {process})",
        value=None if bore is None else round(bore, 4)))
    rows.append(DfmCheck(
        id="min_edge", label="Shortest edge",
        status="pass",
        detail=f"{_mm(small['min_edge_m'])} — measured on the solid, not the mesh",
        value=None if small["min_edge_m"] is None
              else round(small["min_edge_m"] * _MM, 4)))

    voids = trapped_voids(shape)
    rows.append(DfmCheck(
        id="trapped_void", label="Trapped internal void",
        status="pass" if voids == 0 else "fail",
        detail="none" if voids == 0
               else f"{voids} sealed cavity/cavities — no way to remove material or powder",
        value=float(voids)))

    pull = _AXES[axis]
    if process in ("molding", "casting"):
        angle = draft(shape, points, pull)["min_deg"]
        draft_limit = _MIN_DRAFT_DEG[process]
        rows.append(DfmCheck(
            id="draft", label=f"Draft angle about {axis}",
            status="pass" if angle is None or angle >= draft_limit else "fail",
            detail="no side walls" if angle is None
                   else f"{angle:.2f}° minimum (limit {draft_limit:g}° for {process})",
            value=None if angle is None else round(angle, 3)))

        cut = undercuts(shape, points, rays, pull)
        fraction = (cut["trapped_area"] / cut["checked_area"]) if cut["checked_area"] else 0.0
        rows.append(DfmCheck(
            id="undercut", label=f"Undercuts about {axis}",
            status="pass" if cut["trapped_area"] <= 0.0 else "fail",
            detail="none — a two-part mould releases"
                   if cut["trapped_area"] <= 0.0
                   else f"{cut['trapped_area'] * 1e6:.1f} mm² ({fraction * 100:.1f}%) "
                        "cannot release; needs a side action",
            value=round(cut["trapped_area"] * 1e6, 4)))

    if process == "milling":
        access = tool_access(shape, points, rays)
        fraction = (access["blocked_area"] / access["total_area"]) if access["total_area"] else 0.0
        rows.append(DfmCheck(
            id="tool_access", label="3-axis tool access",
            status="pass" if access["blocked_area"] <= 0.0 else "warn",
            detail="every face reachable from a principal direction"
                   if access["blocked_area"] <= 0.0
                   else f"{access['blocked_area'] * 1e6:.1f} mm² ({fraction * 100:.1f}%) "
                        "unreachable in 3 axes — needs 5-axis, EDM or another setup",
            value=round(access["blocked_area"] * 1e6, 4)))

    return rows
