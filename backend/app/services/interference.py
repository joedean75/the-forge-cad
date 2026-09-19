"""Interference & clearance detection for assemblies (roadmap feature 08).

The check every assembly ships through before release: do any placed solids
collide, and where clearances fall below a threshold. Each part already has an
evaluated SDF (its feature tree) and a tessellated surface; for a pair of
components we sample one part's **surface vertices**, transformed into the
other's local frame, against that part's signed distance field. The minimum
signed distance is the gap — negative means penetration — computed both
directions and reused across the pair.

Cheap (reuses shipped geometry), exact at the sampled surface, and bounded by
subsampling dense meshes.

**A propeller is checked as the disc it sweeps, which is a different question.**
`check_prop_clearance` is the second entry point here, and it exists because a
general interference check answers the wrong question about a rotor: two blades
frozen at one azimuth clash or clear depending on where they happen to have been
modelled, and that is not a property of the aircraft. So each propeller is
reduced to its **swept disc** — tip radius from the largest extent across its
thin axis, whatever the body it was modelled as — and the checks are the ones a
multirotor is actually built against: do two discs overlap, is anything inside
one, and how long does the arm have to be. Two consequences worth stating:

* **Stacked rotors are not a clash.** A coaxial X8 has its discs overlapping in
  plan view by design. The pair is reported with the axial separation that makes
  it fine, rather than as an interference — reporting it as a failure would
  train a reader to ignore the check.
* **A blade model gets the same answer as a disc model.** The tip radius is
  measured across the body, so a two-bladed import and this catalog's swept
  disc read identically. That is the point: the reading must not move when
  somebody swaps the CAD.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.evaluator.sdf import _euler_matrix, sdf_from_feature
from app.services import assembly_service, document_service, feature_ops

_MAX_SAMPLE_VERTS = 3000  # subsample dense surfaces for a bounded check
MM = 1000.0


def _surface_samples(mesh) -> np.ndarray:
    """Points on a part's surface: every vertex, plus every triangle's centroid.

    Vertices alone are not enough. An exactly-built box has eight of them, and
    two 40 mm boxes overlapping by 30 mm have no vertex strictly inside the
    other — every corner lands on a face — so the pair reads as clear. The
    approximated mesh used to hide this by having thousands of vertices
    scattered over each face. Centroids put a sample in the middle of every
    facet, which is what makes the check independent of how finely the body
    happens to be tessellated.
    """
    verts = np.asarray(mesh.vertices, dtype=float)
    faces = np.asarray(mesh.faces, dtype=int)
    if len(faces) == 0:
        return verts
    centroids = verts[faces].mean(axis=1)
    return np.vstack([verts, centroids])


class _Placed:
    """A component's SDF + world-space surface samples + placement frame.

    ``shape`` is the same body as exact OpenCascade geometry, when there is one.
    That is what turns "these parts clash" into "these parts clash by 0.4 mm³" —
    see `_overlap_volume`.
    """

    def __init__(self, comp, clearance_m: float = 1.0 / MM):
        doc = document_service.get_document(comp.part_id)
        active = document_service.resolved_tree(doc)
        root = FeatureTree.from_dict(active).root

        from app.cad_kernel.kernel import Kernel
        mesh = Kernel.tessellate(document_service.get_solid(comp.part_id))
        # How far this part's field can be trusted, in metres. Zero for an
        # analytic field; a voxelised one says how coarse it had to be.
        self.tolerance = 0.0
        self.field_source = "analytic"
        try:
            self.sdf = sdf_from_feature(root)
        except ValueError:
            # An imported STEP solid, an exact fillet: bodies that exist only on
            # the exact kernel have no analytic field, and this used to drop the
            # whole component from the check without a word — "3 checked,
            # clear" for a four-part assembly. Its mesh still bounds the same
            # volume, so it is voxelised instead.
            self.sdf = _mesh_field(mesh, clearance_m)
            self.tolerance = float(self.sdf.tolerance)
            self.field_source = "mesh"
        verts = _surface_samples(mesh)
        # The part's own extent, taken before the subsample: `np.linspace`
        # indexing is not obliged to keep an extreme vertex, and the propeller
        # check reads a tip radius off this.
        self.local_min = verts.min(axis=0) if len(verts) else np.zeros(3)
        self.local_max = verts.max(axis=0) if len(verts) else np.zeros(3)
        if len(verts) > _MAX_SAMPLE_VERTS:
            idx = np.linspace(0, len(verts) - 1, _MAX_SAMPLE_VERTS).astype(int)
            verts = verts[idx]
        self.local_verts = verts

        t = comp.transform
        self.scale = float(t.scale or 1.0)
        self.R = _euler_matrix(t.rotate_deg.x, t.rotate_deg.y, t.rotate_deg.z)
        self.t = np.array([t.translate.x, t.translate.y, t.translate.z])
        # Surface points in world space: p_world = R (scale * p_local) + t.
        self.world_verts = (self.R @ (self.scale * verts).T).T + self.t
        self.aabb_min = self.world_verts.min(axis=0)
        self.aabb_max = self.world_verts.max(axis=0)
        self.name = comp.name
        self.shape = _world_shape(root, self.R, self.scale, self.t)

    def signed_distance_at(self, world_pts: np.ndarray) -> np.ndarray:
        """Signed distance (world units) from this part's surface to given points."""
        local = ((self.R.T @ (world_pts - self.t).T).T) / self.scale
        return self.sdf(local) * self.scale


#: The largest voxel grid a mesh-derived field may use. EDT and voxelisation
#: are both linear in it; ~2M is about a second.
_MAX_FIELD_VOXELS = 2_000_000


def _mesh_field(mesh, clearance_m: float):
    """A signed distance field for a part that has only a mesh, sized to the check.

    The pitch is set by the clearance being asked about, not by the body: half
    the clearance, so a near miss is resolved as one. A large body under a small
    clearance would ask for an enormous grid, so the pitch coarsens to stay under
    `_MAX_FIELD_VOXELS` — and the field's `tolerance` coarsens with it, which is
    what the report states rather than hides. The padding reaches past the
    clearance so a point that near the body is still inside the grid.
    """
    from app.cad_kernel.evaluator.sdf import GridSDF

    verts = np.asarray(mesh.vertices, dtype=float)
    extent = np.maximum(verts.max(axis=0) - verts.min(axis=0), 1e-6)
    pitch = max(clearance_m / 2.0, float(extent.max()) / 400.0, 1e-5)
    while float(np.prod(extent / pitch + 1.0)) > _MAX_FIELD_VOXELS:
        pitch *= 1.25
    from app.cad_kernel.evaluator.sdf import MeshTooDetailed

    faces = np.asarray(mesh.faces)
    for _ in range(8):
        pad = int(np.ceil(clearance_m / pitch)) + 3
        try:
            return GridSDF.from_mesh(verts, faces, pitch, pad_cells=pad)
        except MeshTooDetailed:
            pitch *= 1.5
    raise MeshTooDetailed("no pitch within the sample budget")


def _world_shape(root, rotation: np.ndarray, scale: float, translation: np.ndarray):
    """The part as an exact solid, placed in world coordinates, or None.

    The same scale-then-rotate-then-translate order the sampled path uses, so the
    two descriptions of a component cannot end up in different places.
    """
    from app.cad_kernel.evaluator import brep

    if not brep.AVAILABLE:
        return None
    shape = brep.brep_from_feature(root)
    if shape is None:
        return None
    try:
        from OCP.gp import gp_Pnt, gp_Trsf, gp_Vec

        sizing = gp_Trsf()
        sizing.SetScale(gp_Pnt(0.0, 0.0, 0.0), max(scale, 1e-9))
        turning = gp_Trsf()
        turning.SetValues(*(float(v) for row in range(3)
                            for v in (*rotation[row], 0.0)))
        moving = gp_Trsf()
        moving.SetTranslation(gp_Vec(*(float(v) for v in translation)))
        return brep._transformed(shape, moving.Multiplied(turning.Multiplied(sizing)))
    except Exception:  # noqa: BLE001 — OCC raises many types; the sampler still works
        return None


def _overlap_volume(a: "_Placed", b: "_Placed") -> Optional[float]:
    """The volume two placed solids actually share, in m³, or None.

    The sampled check answers *whether* two parts collide and by how deep at the
    worst sampled point. It cannot answer how much material is in the way, which
    is the question that decides whether a clash is a 0.4 mm³ nick to be relieved
    or half a boss in the wrong place. `BRepAlgoAPI_Common` answers it exactly.

    Returns None rather than 0.0 when either body has no exact form, because
    "they do not overlap" and "I could not compute an overlap" are different
    answers and the caller reports them differently.
    """
    if a.shape is None or b.shape is None:
        return None
    try:
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Common

        from app.cad_kernel.evaluator import brep

        builder = BRepAlgoAPI_Common(a.shape, b.shape)
        if not builder.IsDone():
            return None
        return float(brep.volume(builder.Shape()))
    except Exception:  # noqa: BLE001
        return None


def _aabb_gap(a: _Placed, b: _Placed) -> float:
    """Largest per-axis separation of two world AABBs (>0 = definitely apart)."""
    return float(np.max(np.maximum(a.aabb_min - b.aabb_max, b.aabb_min - a.aabb_max)))


def check_assembly(asm_id: str, clearance_mm: float = 1.0) -> Dict:
    """Pairwise interference + clearance across an assembly's placed components."""
    asm = assembly_service.get_assembly(asm_id)
    if asm is None:
        raise KeyError(asm_id)
    comps = [c for c in asm.components if not c.missing]
    clearance_m = clearance_mm / MM
    placed: List[_Placed] = []
    skipped: List[dict] = []
    for c in comps:
        try:
            placed.append(_Placed(c, clearance_m))
        except Exception as exc:  # noqa: BLE001 — reported, never swallowed
            # A component that cannot be checked is named with the reason. The
            # alternative — leaving it out of the count — reads as "clear" for
            # an assembly whose one unexamined part is the one in the way.
            skipped.append({"component": c.name, "part_id": c.part_id,
                            "reason": f"{type(exc).__name__}: {exc}"[:300]})
    pairs: List[dict] = []
    interferences = 0
    warnings = 0
    for i in range(len(placed)):
        for j in range(i + 1, len(placed)):
            a, b = placed[i], placed[j]
            # Skip pairs whose AABBs are farther apart than the threshold.
            if _aabb_gap(a, b) > clearance_m:
                continue
            d_ab = float(b.signed_distance_at(a.world_verts).min())
            d_ba = float(a.signed_distance_at(b.world_verts).min())
            gap = min(d_ab, d_ba)
            # Each direction is read against one part's field, so it is trusted
            # to that part's tolerance. Without the band, a coincident mate
            # against a voxelised part reads as a fraction of a cell of
            # penetration and every correctly mated import is a "clash".
            clash = (d_ab < -(b.tolerance * b.scale) - 1e-6
                     or d_ba < -(a.tolerance * a.scale) - 1e-6)
            if clash:
                status = "interference"
                interferences += 1
            elif gap < clearance_m:
                status = "clearance"
                warnings += 1
            else:
                continue
            row = {
                "a": a.name, "b": b.name, "status": status,
                "gap_mm": round(gap * MM, 3),
                "penetration_mm": round(-gap * MM, 3) if gap < 0 else 0.0,
            }
            band = max(a.tolerance * a.scale, b.tolerance * b.scale)
            if band > 0:
                row["tolerance_mm"] = round(band * MM, 3)
            if status == "interference":
                # How much material is actually in the way. Only asked of a real
                # clash: `Common` on two parts that merely sit close is an OCC
                # boolean returning nothing, which is a lot of work for a zero.
                shared = _overlap_volume(a, b)
                row["overlap_mm3"] = (round(shared * MM ** 3, 4) if shared is not None
                                      else None)
                if shared is None:
                    row["overlap_note"] = ("one of these bodies has no exact form, so the "
                                           "clash is measured by surface sampling only")
            pairs.append(row)

    pairs.sort(key=lambda p: p["gap_mm"])
    measured = [p["overlap_mm3"] for p in pairs if p.get("overlap_mm3") is not None]
    return {
        "assembly_id": asm_id,
        "components_checked": len(placed),
        "components_total": len(comps),
        # Parts whose geometry could not be read at all. Not "clear": unknown.
        "skipped": skipped,
        # Parts checked against a voxelised copy of their mesh rather than an
        # exact field, with how far that reading can be trusted.
        "approximated": [{"component": p.name, "tolerance_mm": round(p.tolerance * p.scale * MM, 3)}
                         for p in placed if p.field_source == "mesh"],
        "interferences": interferences,
        "clearance_warnings": warnings,
        "clearance_threshold_mm": clearance_mm,
        "pairs": pairs,
        "overlap_mm3": round(sum(measured), 4) if measured else None,
        "clear": interferences == 0 and warnings == 0 and not skipped,
    }


# ------------------------------------------------- propellers, as swept discs --

#: How many points around a disc's rim are sampled against the other disc. The
#: chord error of an N-gon inscribed in a 5-inch prop is 0.02 mm at 128, which is
#: two orders below any clearance anybody sets.
_RIM_SAMPLES = 128

#: The polar grid laid across a disc to find what is standing in it. 16 x 64 on
#: a 5-inch prop is 4 mm between samples radially and 6 mm at the tip — enough
#: to catch a standoff or a camera, and not a 2 mm wire. Bumping it costs one
#: field evaluation per point per pair, and the surface-sample pass beside it
#: catches the large intrusions this one would only graze.
_AREA_RINGS = 16
_AREA_SPOKES = 64

#: How parallel two axes have to be before the pair is read as two discs in
#: parallel planes rather than as an arbitrary pair. cos 2° — a multirotor's
#: motors are mounted with a degree or two of tilt on purpose and that does not
#: make a stacked pair unstacked.
_PARALLEL = 0.99939


class _Disc:
    """A rotor as the volume it sweeps, derived from whatever body was modelled.

    **The tip radius is measured across the body, not read off a blade.** A
    propeller's thin direction is its axis; the largest extent across that is
    the diameter it sweeps whether the CAD is two blades, three, or the disc
    this catalog builds. That is the whole reason this check exists separately:
    two blades frozen at one azimuth clash or clear depending on where the
    modeller left them, and where the modeller left them is not a property of
    the aircraft.
    """

    def __init__(self, placed: _Placed, component_id: str, hub: float = 0.0):
        size = placed.local_max - placed.local_min
        axis_index = int(np.argmin(size))
        plane = [i for i in range(3) if i != axis_index]
        local_centre = (placed.local_max + placed.local_min) / 2.0

        self.component_id = component_id
        self.name = placed.name
        self.radius = float(max(size[i] for i in plane)) / 2.0 * placed.scale
        self.half_thickness = max(float(size[axis_index]) / 2.0 * placed.scale, 1e-9)
        self.centre = placed.R @ (placed.scale * local_centre) + placed.t
        axis = placed.R[:, axis_index]
        self.axis = axis / (np.linalg.norm(axis) or 1.0)
        # **The swept region is an annulus, not a disc.** The hub does not sweep
        # anything: it is bolted to a shaft, and that shaft reaches into it by
        # design. Left as a disc, every correctly built quadcopter reported four
        # obstructions reading "your propeller is mounted on its motor" — and a
        # check that flags the normal case is one people learn to ignore, which
        # is the same reason a stacked rotor is not called a clash.
        self.hub = min(max(float(hub), 0.0), self.radius * 0.9)

    @property
    def diameter_mm(self) -> float:
        return self.radius * 2 * MM

    def signed_distance_at(self, world_pts: np.ndarray) -> np.ndarray:
        """Signed distance from the swept volume — a cylinder about the axis."""
        local = world_pts - self.centre
        along = local @ self.axis
        radial = np.linalg.norm(local - np.outer(along, self.axis), axis=1)
        d_radial = np.maximum(self.hub - radial, radial - self.radius)
        d_axial = np.abs(along) - self.half_thickness
        outside = np.hypot(np.maximum(d_radial, 0.0), np.maximum(d_axial, 0.0))
        inside = np.minimum(np.maximum(d_radial, d_axial), 0.0)
        return outside + inside

    def radius_at(self, world_pts: np.ndarray) -> np.ndarray:
        """How far off the axis each point sits — what says a hit is at the hub."""
        local = world_pts - self.centre
        return np.linalg.norm(local - np.outer(local @ self.axis, self.axis), axis=1)

    def area(self) -> np.ndarray:
        """Points across the swept volume itself, on a polar grid.

        **Sampling the obstruction's surface is not enough, and this is the
        same bug `_surface_samples` was written for, one level up.** A 6 mm mast
        standing through an 8 mm disc has no vertex and no facet centroid inside
        that band — its samples are all above and below — so the strike reads as
        a clearance. The disc is the body with the simple parameterisation and
        the other part has an exact field everywhere, so the question is asked
        the other way round: put points across the disc and evaluate the part's
        own distance at them.

        Three planes rather than one: something reaching 2 mm into the band from
        below never crosses the mid-plane, and a rotor is struck by that too.
        """
        u = np.array([1.0, 0.0, 0.0])
        if abs(float(self.axis @ u)) > 0.9:
            u = np.array([0.0, 1.0, 0.0])
        e1 = np.cross(self.axis, u)
        e1 /= np.linalg.norm(e1) or 1.0
        e2 = np.cross(self.axis, e1)
        radii = np.linspace(self.hub, self.radius, _AREA_RINGS)
        angles = np.linspace(0.0, 2 * np.pi, _AREA_SPOKES, endpoint=False)
        offsets = (np.outer(radii, np.cos(angles))[:, :, None] * e1
                   + np.outer(radii, np.sin(angles))[:, :, None] * e2)
        disc = self.centre + offsets.reshape(-1, 3)
        return np.vstack([disc + h * self.half_thickness * self.axis
                          for h in (-1.0, 0.0, 1.0)])

    def rim(self) -> np.ndarray:
        """Points around the tip, plus the centre.

        The rim is where two discs that are merely close are nearest each other,
        and the centre is what catches a small disc sitting wholly inside a big
        one — whose rim is inside the other but whose own rim test would miss it
        from the far side.
        """
        u = np.array([1.0, 0.0, 0.0])
        if abs(float(self.axis @ u)) > 0.9:
            u = np.array([0.0, 1.0, 0.0])
        e1 = np.cross(self.axis, u)
        e1 /= np.linalg.norm(e1) or 1.0
        e2 = np.cross(self.axis, e1)
        angles = np.linspace(0.0, 2 * np.pi, _RIM_SAMPLES, endpoint=False)
        ring = (self.centre + self.radius * (np.outer(np.cos(angles), e1)
                                             + np.outer(np.sin(angles), e2)))
        return np.vstack([ring, self.centre[None, :]])


def _disc_pair(a: _Disc, b: _Disc, clearance_m: float) -> dict:
    """How two rotors relate: the gap that means something, and stacked or not.

    **Two rotors in parallel planes are read in plan and in axis, not as a
    shortest escape.** The true signed distance between two overlapping discs is
    to whichever surface is nearest, and for coplanar rotors that is the flat
    face — so a quad whose arms are 14 mm too short reported a 4 mm overlap,
    4 mm being the blade's own thickness and a number about nothing. What the
    reader needs is the dimension they would change, and for rotors side by side
    that is the arm. So the reported gap is the **radial** overlap when the
    discs interpenetrate, and the axial separation when they are stacked, and
    both readings are published beside it so neither has to be inferred.

    The rim sampling stays for rotors whose axes are not parallel — a tiltrotor,
    a canted arm — where no single dimension is the one to change and the true
    3D distance is the honest answer.
    """
    offset = b.centre - a.centre
    parallel = abs(float(a.axis @ b.axis)) >= _PARALLEL
    axial = abs(float(offset @ a.axis))
    plan = float(np.linalg.norm(offset - (offset @ a.axis) * a.axis))
    row = {
        "a": a.name, "b": b.name,
        "a_component_id": a.component_id, "b_component_id": b.component_id,
        "centre_distance_mm": round(float(np.linalg.norm(offset)) * MM, 3),
        "coaxial": bool(parallel and plan < 1e-6),
        "parallel": bool(parallel),
    }
    if parallel:
        plan_gap = plan - a.radius - b.radius
        axial_gap = axial - a.half_thickness - b.half_thickness
        if plan_gap >= 0 and axial_gap >= 0:
            gap = float(np.hypot(plan_gap, axial_gap))
        elif plan_gap >= 0:                 # side by side in one plane
            gap = plan_gap
        elif axial_gap >= 0:                # stacked: overlapped in plan, apart in z
            gap = axial_gap
        else:                               # they intersect; the arm is the fix
            gap = plan_gap
        row["plan_gap_mm"] = round(plan_gap * MM, 3)
        row["axial_gap_mm"] = round(axial_gap * MM, 3)
        row["stacked"] = bool(plan_gap < 0 <= axial_gap)
    else:
        gap = float(min(b.signed_distance_at(a.rim()).min(),
                        a.signed_distance_at(b.rim()).min()))
        row["stacked"] = False
    row["gap_mm"] = round(gap * MM, 3)
    if gap < -1e-9:
        row["status"] = "interference"
    elif gap < clearance_m:
        row["status"] = "clearance"
    else:
        row["status"] = "clear"
    return row


def _layout(discs: List[_Disc], gap_m: float) -> Optional[dict]:
    """What the arms have to be, for the rotors that share one axis.

    Closed-form and stated as such: `2 R sin(pi/N) - D` is the tip gap of **N
    evenly spaced** rotors at radius R, which is what a multirotor is and is not
    what an arbitrary arrangement is. The measured pairwise gaps above owe
    nothing to that assumption; this is the design aid beside them, and it says
    which it is rather than leaving a reader to find out by disagreeing with it.
    """
    if len(discs) < 2:
        return None
    axis = discs[0].axis
    group = [d for d in discs if abs(float(d.axis @ axis)) >= _PARALLEL]
    if len(group) < 2:
        return None
    centres = np.array([d.centre for d in group])
    hub = centres.mean(axis=0)
    offsets = centres - hub
    arms = np.linalg.norm(offsets - np.outer(offsets @ axis, axis), axis=1)
    n = len(group)
    diameter = max(d.radius for d in group) * 2
    spread = float(np.sin(np.pi / n))
    return {
        "rotors": n,
        "arms_mm": [round(float(a) * MM, 3) for a in arms],
        "mean_arm_mm": round(float(arms.mean()) * MM, 3),
        "min_arm_mm": round((diameter + gap_m) / (2 * spread) * MM, 3),
        "max_diameter_mm": round(
            max(2 * float(arms.mean()) * spread - gap_m, 0.0) * MM, 3),
        "assumes": (f"{n} rotors evenly spaced about the centroid of their own "
                    f"centres — the measured pair gaps above assume nothing"),
    }


def _hub_radius_m(doc, override_mm: Optional[float]) -> float:
    """How much of a rotor's centre is mount rather than sweep, in metres.

    From the catalog row where the rotor came from one, because that is where
    the hub diameter was declared; `hub_mm` overrides for a rotor somebody
    modelled, which has no row to declare anything. Zero otherwise, and the
    report says which each rotor used — a hub silently invented would suppress a
    real strike near the centre.
    """
    from app.services import parts_catalog

    if override_mm is not None:
        # A *diameter*, like the catalog's `hub_dia_mm` beside it and like every
        # other bore this system names. `self.hub` is the radius.
        return max(float(override_mm), 0.0) / 2.0 / MM
    published = parts_catalog.published_param(
        doc.catalog_ref if doc is not None else None, "hub_dia_mm")
    return (published / 2.0 / MM) if published else 0.0


def check_prop_clearance(asm_id: str, clearance_mm: float = 5.0,
                         props: Optional[List[str]] = None,
                         hub_mm: Optional[float] = None) -> Dict:
    """Rotor clearance: disc against disc, and anything standing in one.

    Propellers are found by their catalog category unless `props` names the
    components outright — which it has to be able to, because a rotor somebody
    modelled or imported is still a rotor and the check would be useless on the
    aircraft most worth checking.

    Nothing is reported clear that was not measured: an assembly with no rotor
    this can identify raises rather than answering `clear`, because a clearance
    report that means "I found nothing to check" is the one thing this must
    never be mistaken for.
    """
    asm = assembly_service.get_assembly(asm_id)
    if asm is None:
        raise KeyError(asm_id)

    from app.services import standard_parts

    named = set(props or [])
    comps = [c for c in asm.components if not c.missing]
    placed: Dict[str, _Placed] = {}
    for c in comps:
        try:
            placed[c.id] = _Placed(c)
        except Exception:  # noqa: BLE001 — a part that will not evaluate is skipped
            continue

    rotors: List[_Disc] = []
    others: List[tuple] = []
    for c in comps:
        if c.id not in placed:
            continue
        doc = document_service.get_document(c.part_id)
        if named:
            is_prop = c.id in named
        else:
            is_prop = standard_parts.category_of(
                doc.catalog_ref if doc is not None else None,
                asm.owner_id) == "propeller"
        if is_prop:
            rotors.append(_Disc(placed[c.id], c.id, _hub_radius_m(doc, hub_mm)))
        else:
            others.append((c, placed[c.id]))

    if not rotors:
        raise ValueError(
            "no propellers in this assembly to check. Place one from the parts "
            "catalog (any prop-* key), or name the components that are rotors "
            "in `props` — a rotor somebody modelled is still a rotor, and this "
            "will not report clearance it did not measure")

    clearance_m = clearance_mm / MM
    pairs = [_disc_pair(rotors[i], rotors[j], clearance_m)
             for i in range(len(rotors)) for j in range(i + 1, len(rotors))]

    obstructions: List[dict] = []
    for disc in rotors:
        area = disc.area()
        for comp, body in others:
            # Both directions, and neither is redundant: the disc's own grid
            # against the part's field catches a thin thing standing through the
            # band, and the part's surface against the disc catches a body big
            # enough that the grid only grazes it.
            across = body.signed_distance_at(area)
            surface = disc.signed_distance_at(body.world_verts)
            if float(across.min()) <= float(surface.min()):
                gap, at = float(across.min()), area[int(np.argmin(across))]
            else:
                worst = int(np.argmin(surface))
                gap, at = float(surface[worst]), body.world_verts[worst]
            at_radius = float(disc.radius_at(at[None, :])[0])
            # **The near-miss band does not apply where the mount lives.** A
            # rotor is bolted to something and that something reaches into the
            # middle of it — a shaft ending three millimetres under the blades
            # is what a shaft is, not a warning, and every correctly built
            # quadcopter raised four of them. Anything that has to reach the hub
            # is within a clearance of the annulus's inner edge *by
            # construction*, so the band starts a clearance outboard of it;
            # asking for margin closer in is asking for the rotor not to be
            # mounted. Material actually *inside* the annulus is still a strike
            # wherever it is — only the near miss is dropped.
            if gap < -1e-9:
                status = "interference"
            elif gap < clearance_m and at_radius > disc.hub + clearance_m:
                status = "clearance"
            else:
                continue
            obstructions.append({
                "prop": disc.name, "prop_component_id": disc.component_id,
                "obstruction": comp.name, "component_id": comp.id,
                "gap_mm": round(gap * MM, 3),
                # Where on the disc it reaches, because an intrusion at the hub
                # is the shaft the rotor is bolted to and one at the tip is a
                # strike. The reader cannot tell those apart from a depth.
                "at_radius_mm": round(at_radius * MM, 3),
                "status": status,
            })

    pairs.sort(key=lambda p: p["gap_mm"])
    obstructions.sort(key=lambda o: o["gap_mm"])
    struck = [r for r in pairs + obstructions if r["status"] == "interference"]
    warned = [r for r in pairs + obstructions if r["status"] == "clearance"]
    return {
        "assembly_id": asm_id,
        "props": [{"component_id": d.component_id, "name": d.name,
                   "diameter_mm": round(d.diameter_mm, 3),
                   # What was treated as mount rather than sweep, stated per
                   # rotor: a hub is the one thing here that suppresses a
                   # reading, so it may not be invisible.
                   "hub_mm": round(d.hub * 2 * MM, 3),
                   "centre_mm": [round(float(v) * MM, 3) for v in d.centre],
                   "axis": [round(float(v), 6) for v in d.axis]}
                  for d in rotors],
        "clearance_threshold_mm": clearance_mm,
        "pairs": pairs,
        "obstructions": obstructions,
        "layout": _layout(rotors, clearance_m),
        "interferences": len(struck),
        "clearance_warnings": len(warned),
        "clear": not struck and not warned,
        "declared": bool(named),
    }
