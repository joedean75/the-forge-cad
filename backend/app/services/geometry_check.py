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


def check_solid(solid) -> List[DfmCheck]:
    """Structural validity of an evaluated solid, as DfmCheck rows."""
    tm = _as_trimesh(solid)
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
    # failing on.
    euler = int(tm.euler_number)
    checks.append(DfmCheck(
        id="topology",
        label="Topology",
        status="pass" if euler == 2 else "warn",
        detail=f"Euler characteristic {euler}" + ("" if euler == 2 else " (genus > 0 or split shell)"),
        value=float(euler),
    ))

    bodies = int(tm.body_count)
    checks.append(DfmCheck(
        id="connectivity",
        label="Single connected body",
        status="pass" if bodies == 1 else "warn",
        detail=f"{bodies} disconnected {'body' if bodies == 1 else 'bodies'}"
               + ("" if bodies == 1 else " — a subtraction may have severed the part"),
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

    checks = [DfmCheck(id="evaluates", label="Evaluates to geometry",
                       status="pass", detail="body evaluated")]
    checks.extend(check_solid(solid))
    return _report(checks)


def _report(checks: List[DfmCheck]) -> dict:
    failed = [c.id for c in checks if c.status == "fail"]
    warned = [c.id for c in checks if c.status == "warn"]
    return {
        "valid": not failed,
        "checks": [c.model_dump() for c in checks],
        "failed": failed,
        "warnings": warned,
    }
