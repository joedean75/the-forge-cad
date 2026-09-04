"""Is the geometry a valid solid at all?

Nothing in the codebase asked this. The only validity gate anywhere was
``mass_properties`` refusing a zero-volume mesh; there was no manifoldness,
watertightness, winding or connectivity check, even though ``trimesh`` has been
a dependency all along and answers every one of them.

The SDF + marching-cubes pipeline *tends* to produce closed manifold meshes, and
that is presumably why no one checked. But "tends to" is not a guarantee a
reward function can be built on, and the GridSDF path (a thresholded voxel grid
run through a distance transform and trilinear interpolation — how imported
meshes and optimizer output re-enter the kernel) can genuinely yield thin or
disconnected shells.

Results reuse ``DfmCheck`` so a caller renders or scores these exactly like the
manufacturability checks.
"""

from __future__ import annotations

from typing import List

import numpy as np
import trimesh

from app.cad_kernel.geom.meshing import solid_to_mesh
from app.models.schemas import DfmCheck

MM = 1000.0

# Below this a "solid" is a rounding error rather than a part. 1e-12 m³ is a
# cube 10 µm on a side — far under anything the kernel can resolve.
_MIN_VOLUME_M3 = 1e-12


def _as_trimesh(solid) -> trimesh.Trimesh:
    mesh = solid_to_mesh(solid)
    return trimesh.Trimesh(
        vertices=np.asarray(mesh.vertices, dtype=float),
        faces=np.asarray(mesh.faces, dtype=np.int64),
        process=False,  # never silently repair what we are here to measure
    )


def _open_edges(tm: trimesh.Trimesh) -> int:
    """Edges used by exactly one triangle — the true open boundary.

    Not `facets_boundary`, which groups by coplanar facet and counts a
    four-sided tube's two rims as four. The question a sheet is asked is how
    much edge is unattached, and that is an edge count.
    """
    from trimesh.grouping import group_rows

    if not len(tm.faces):
        return 0
    return int(len(group_rows(tm.edges_sorted, require_count=1)))


def bodies_of(solid) -> List[dict]:
    """Every disconnected solid in the body, measured separately.

    A document holds one feature tree, but a tree can perfectly well evaluate to
    several disjoint solids — a pair of brackets machined from one billet, a part
    that a cut deliberately separated. Volume, mass and the bounding box are all
    correct for that already, because they are sums and unions; what was missing
    was any way to *see* the bodies. "2 disconnected bodies" cannot be told apart
    from "the part fell in half" without knowing how big each one is and where.

    Ordered largest first, so the main body is index 0 and the debris — if it is
    debris — is at the end.
    """
    tm = _as_trimesh(solid)
    if not len(tm.faces):
        return []
    parts = tm.split(only_watertight=False)
    if len(parts) <= 1:
        parts = [tm]

    out = []
    for part in parts:
        volume = abs(float(part.volume))
        low, high = (np.asarray(b, dtype=float) * MM for b in part.bounds)
        centre = (np.asarray(part.centroid, dtype=float) * MM)
        out.append({
            "volume_mm3": round(volume * MM ** 3, 4),
            "area_mm2": round(float(part.area) * MM * MM, 4),
            "triangles": int(len(part.faces)),
            "watertight": bool(part.is_watertight),
            "size_mm": [round(float(v), 4) for v in (high - low)],
            "center_mm": [round(float(v), 4) for v in centre],
            "min_mm": [round(float(v), 4) for v in low],
            "max_mm": [round(float(v), 4) for v in high],
        })
    out.sort(key=lambda b: -b["volume_mm3"])
    return out


def check_solid(solid, expect_bodies: int | None = None,
                body_kind: str | None = None) -> List[DfmCheck]:
    """Structural validity of an evaluated body, as DfmCheck rows.

    ``expect_bodies`` is how many disjoint solids the part is *supposed* to have.
    Without it, more than one is a warning — the usual cause is a cut that went
    further than intended. With it, the count is judged against the intent, so a
    deliberately two-piece part stops being reported as damage. The spec is where
    that intent already lives (`design_spec`'s ``bodies`` requirement), and
    `check_document` reads it from there.

    ``body_kind`` decides which questions are even askable. **On a sheet, "not
    watertight" and "encloses no volume" are the definition, not defects** —
    reporting them as failures would mean every surface body scored zero on a
    check that had understood it perfectly. What a sheet is still checked for is
    everything that is a defect on any body: emptiness, consistent winding, and
    how many disconnected pieces it is in. Taken from the body itself when the
    caller does not say, because `Solid.attrs` is where the evaluator recorded
    it and a second guess here would be a second answer.
    """
    tm = _as_trimesh(solid)
    kind = str(body_kind or getattr(solid, "attrs", {}).get("body_kind", "solid"))
    sheet = kind == "sheet"
    checks: List[DfmCheck] = []

    triangles = int(len(tm.faces))
    checks.append(DfmCheck(
        id="non_empty",
        label="Non-empty body",
        status="pass" if triangles else "fail",
        detail=f"{triangles:,} triangles",
        value=float(triangles),
    ))
    if not triangles:
        # Every remaining check is meaningless on an empty mesh and trimesh
        # reports confusing values for one (euler_number of 0, "watertight").
        return checks

    if sheet:
        # The two readings a surface body genuinely has, in place of the two it
        # cannot: how much surface, and how much edge is left open. An open edge
        # is not a fault — it is where the next sheet knits on — so it is
        # reported as a measurement rather than judged.
        area = float(tm.area)
        checks.append(DfmCheck(
            id="area", label="Surface area", status="pass",
            detail=f"{area * 1e6:.3f} mm²", value=area,
        ))
        open_edges = _open_edges(tm)
        checks.append(DfmCheck(
            id="open_edges", label="Open boundary", status="pass",
            detail=("closed shell — nothing is open, so this sheet could be "
                    "made solid" if not open_edges
                    else f"{open_edges} open edges, which is what a sheet has "
                         f"until something knits onto it"),
            value=float(open_edges),
        ))
    else:
        volume = float(abs(tm.volume))
        checks.append(DfmCheck(
            id="volume",
            label="Enclosed volume",
            status="pass" if volume > _MIN_VOLUME_M3 else "fail",
            detail=f"{volume * 1e9:.3f} mm³",
            value=volume,
        ))

        watertight = bool(tm.is_watertight)
        checks.append(DfmCheck(
            id="watertight",
            label="Watertight",
            status="pass" if watertight else "fail",
            detail="closed surface" if watertight
                   else f"{len(tm.facets_boundary)} boundary loop(s) — the body has holes in its skin",
            value=1.0 if watertight else 0.0,
        ))

    winding = bool(tm.is_winding_consistent)
    checks.append(DfmCheck(
        id="winding",
        label="Consistent winding",
        status="pass" if winding else "fail",
        detail="normals agree" if winding else "faces disagree on which side is outside",
        value=1.0 if winding else 0.0,
    ))

    # Euler characteristic 2 == a single closed surface of genus 0. Anything else
    # is not wrong per se (a torus is genus 1, legitimately) but it tells a caller
    # the topology is not a simple blob, which is worth surfacing rather than
    # failing on. An open sheet is *never* 2, so the reading is reported without
    # a verdict rather than warning about every surface ever made.
    euler = int(tm.euler_number)
    checks.append(DfmCheck(
        id="topology",
        label="Topology",
        status="pass" if (sheet or euler == 2) else "warn",
        detail=f"Euler characteristic {euler}"
               + ("" if euler == 2 else
                  " (an open sheet, so not 2)" if sheet else
                  " (genus > 0 or split shell)"),
        value=float(euler),
    ))

    bodies = int(tm.body_count)
    wanted = 1 if expect_bodies is None else int(expect_bodies)
    plural = "body" if bodies == 1 else "bodies"
    if bodies == wanted:
        detail = f"{bodies} {plural}" + ("" if wanted == 1 else " — as specified")
        status = "pass"
    elif expect_bodies is None:
        detail = f"{bodies} disconnected bodies — a subtraction may have severed the part"
        status = "warn"
    else:
        detail = f"{bodies} {plural}, the spec asks for {wanted}"
        status = "warn"
    checks.append(DfmCheck(
        id="connectivity",
        label="Connected bodies" if expect_bodies not in (None, 1) else "Single connected body",
        status=status,
        detail=detail,
        value=float(bodies),
    ))

    return checks


def check_document(doc_id: str, resolution: int = 128) -> dict:
    """Validate a document's evaluated body.

    An unevaluatable document is itself a result — reported as a failed
    ``evaluates`` check rather than an exception, so a caller scoring geometry
    gets one uniform shape back either way.
    """
    from app.services import document_service as docs

    try:
        solid = docs.get_solid(doc_id, resolution=resolution)
    except (KeyError, ValueError) as exc:
        checks = [DfmCheck(id="evaluates", label="Evaluates to geometry",
                           status="fail", detail=str(exc))]
        return _report(checks)

    kind = str(getattr(solid, "attrs", {}).get("body_kind", "solid"))
    checks = [DfmCheck(id="evaluates", label="Evaluates to geometry",
                       status="pass",
                       detail=f"body evaluated ({kind})")]
    checks.extend(check_solid(solid, expect_bodies=_expected_bodies(doc_id),
                              body_kind=kind))
    return _report(checks)


def _expected_bodies(doc_id: str) -> int | None:
    """How many disjoint solids the document's spec asks for, if it says.

    Read from the spec rather than added as a separate flag: "what the part is
    supposed to be" already has a home, and a second place to declare the same
    thing is a second place for the two to disagree.
    """
    from app.services import document_service as docs

    doc = docs.get_document(doc_id)
    for requirement in ((doc.spec or {}).get("requirements") or []) if doc else []:
        if requirement.get("kind") == "bodies":
            for key in ("value", "min", "max"):
                if requirement.get(key) is not None:
                    try:
                        return int(float(requirement[key]))
                    except (TypeError, ValueError):
                        return None
    return None


def _report(checks: List[DfmCheck]) -> dict:
    failed = [c.id for c in checks if c.status == "fail"]
    warned = [c.id for c in checks if c.status == "warn"]
    return {
        "valid": not failed,
        "checks": [c.model_dump() for c in checks],
        "failed": failed,
        "warnings": warned,
    }
