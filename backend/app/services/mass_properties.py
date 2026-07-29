"""Mass properties & inspection (roadmap production-feature 10).

Center of gravity, volume, mass, surface area, and the inertia tensor about the
COG — the quantitative read engineers check constantly — computed from the
evaluated triangle mesh via the divergence-theorem (signed-tetrahedron) method,
scaled by the document's assigned material density.
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np

from app.services import document_service, materials


def _tri_mesh(doc_id: str):
    from app.cad_kernel.kernel import Kernel

    solid = document_service.get_solid(doc_id)
    mesh = Kernel.tessellate(solid)
    return np.asarray(mesh.vertices, dtype=float), np.asarray(mesh.faces, dtype=int)


def mass_properties(doc_id: str) -> Dict:
    """Volume, mass, COG, surface area, and inertia tensor about the COG.

    Uses the signed-tetrahedron integration (Mirtich / divergence theorem):
    every triangle forms a tetrahedron with the origin; signed contributions sum
    to exact volume, centroid and second moments for a closed mesh.
    """
    doc = document_service.get_document(doc_id)
    if doc is None:
        raise KeyError(doc_id)
    verts, faces = _tri_mesh(doc_id)
    if len(faces) == 0:
        raise ValueError("The part has no solid geometry to measure.")

    a = verts[faces[:, 0]]
    b = verts[faces[:, 1]]
    c = verts[faces[:, 2]]

    # Signed tetra volume (origin apex) and surface area.
    cross = np.cross(b - a, c - a)
    tri_area = 0.5 * np.linalg.norm(cross, axis=1)
    surface_area = float(tri_area.sum())
    vol6 = np.einsum("ij,ij->i", a, np.cross(b, c))  # 6*signed volume per tetra
    volume = float(vol6.sum() / 6.0)
    if abs(volume) < 1e-15:
        raise ValueError("Mesh is not a closed solid; mass properties unavailable.")
    sign = 1.0 if volume > 0 else -1.0
    volume = abs(volume)

    # Centroid: sum of tetra-centroid * tetra-volume.
    tetra_centroid = (a + b + c) / 4.0
    centroid = sign * (tetra_centroid * (vol6[:, None] / 6.0)).sum(axis=0) / volume

    density = materials.density_of(doc.material_id)
    mass = volume * density

    # Inertia tensor about the origin via per-tetra covariance, then shift to COG.
    # Canonical covariance of a tetra (origin,a,b,c) integrated over its volume.
    Ixx = Iyy = Izz = Ixy = Iyz = Ixz = 0.0
    for p, q, r, v6 in zip(a, b, c, vol6):
        vol = v6 / 6.0
        for i, j, acc in (
            (0, 0, "xx"), (1, 1, "yy"), (2, 2, "zz"),
            (0, 1, "xy"), (1, 2, "yz"), (0, 2, "xz"),
        ):
            s = (
                p[i] * p[j] + q[i] * q[j] + r[i] * r[j]
                + (p[i] + q[i] + r[i]) * (p[j] + q[j] + r[j])
            ) * vol / 20.0
            if acc == "xx":
                Ixx += s
            elif acc == "yy":
                Iyy += s
            elif acc == "zz":
                Izz += s
            elif acc == "xy":
                Ixy += s
            elif acc == "yz":
                Iyz += s
            else:
                Ixz += s
    Ixx *= sign; Iyy *= sign; Izz *= sign; Ixy *= sign; Iyz *= sign; Ixz *= sign

    # Products give ∫x·y etc.; convert to the inertia tensor about the origin,
    # multiply by density, then parallel-axis shift to the COG.
    inertia_o = density * np.array([
        [Iyy + Izz, -Ixy, -Ixz],
        [-Ixy, Ixx + Izz, -Iyz],
        [-Ixz, -Iyz, Ixx + Iyy],
    ])
    cx, cy, cz = centroid
    d2 = cx * cx + cy * cy + cz * cz
    shift = mass * np.array([
        [cy * cy + cz * cz, -cx * cy, -cx * cz],
        [-cx * cy, cx * cx + cz * cz, -cy * cz],
        [-cx * cz, -cy * cz, cx * cx + cy * cy],
    ])
    inertia_cog = inertia_o - shift
    _ = d2  # (kept for clarity of the parallel-axis relation)

    # Where the body has an exact form, take volume, area and centre of mass from
    # the geometry rather than from its triangulation. The mesh figures are only
    # as good as the tessellation — a bored plate came out ~0.001% heavy — and on
    # a part being quoted or balanced that is a number someone will rely on.
    exact = _exact_properties(doc)
    if exact is not None:
        volume, surface_area = exact["volume"], exact["area"]
        centroid = np.asarray(exact["center_of_mass"], dtype=float)
        mass = volume * density

    mat = materials.get_material(doc.material_id)
    return {
        "material": {"id": mat.id, "name": mat.name, "density": mat.density},
        "volume_cm3": round(volume * 1e6, 6),
        "mass_kg": round(mass, 9),
        "surface_area_cm2": round(surface_area * 1e4, 5),
        "center_of_mass_mm": [round(float(v) * 1000, 5) for v in centroid],
        # Inertia tensor about the COG, kg·mm² (SI * 1e6).
        "inertia_kg_mm2": [[round(float(inertia_cog[i, j] * 1e6), 3) for j in range(3)] for i in range(3)],
        "bbox_mm": _bbox_mm(verts),
        # Whether these came from exact geometry or from a triangulation of it.
        "exact": exact is not None,
    }


def _exact_properties(doc) -> Optional[Dict]:
    """Volume, area and centre of mass straight from the B-rep, or None."""
    from app.cad_kernel.core.feature_tree import FeatureTree
    from app.cad_kernel.evaluator import brep
    from app.services import feature_ops

    if not brep.AVAILABLE:
        return None
    try:
        active = feature_ops.resolve_datums(feature_ops.resolve_suppressed(doc.feature_tree))
        if doc.parameters:
            from app.services import parameters

            scope, _ = parameters.resolve_values(doc.parameters)
            active = parameters.resolve_tree(active, scope)
        shape = brep.brep_from_feature(FeatureTree.from_dict(active).root)
        if shape is None:
            return None
        return {"volume": brep.volume(shape), "area": brep.surface_area(shape),
                "center_of_mass": brep.center_of_mass(shape)}
    except Exception:  # noqa: BLE001 — exactness is a bonus, never a hard failure
        return None


def _bbox_mm(verts: np.ndarray) -> Dict:
    lo = verts.min(axis=0) * 1000.0
    hi = verts.max(axis=0) * 1000.0
    return {"min": [round(float(v), 3) for v in lo],
            "max": [round(float(v), 3) for v in hi],
            "size": [round(float(hi[i] - lo[i]), 3) for i in range(3)]}
