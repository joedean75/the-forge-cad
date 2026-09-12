"""Mass properties & inspection (roadmap production-feature 10).

Center of gravity, volume, mass, surface area, and the inertia tensor about the
COG — the quantitative read engineers check constantly — computed from the
exact B-rep when available, otherwise from the evaluated triangle mesh,
scaled by the document's assigned material density.
"""

from __future__ import annotations

from typing import Dict

import numpy as np

from app.services import document_service, materials


def _tri_mesh(doc_id: str):
    from app.cad_kernel.kernel import Kernel

    doc = document_service.get_document(doc_id)
    solid = document_service.get_solid(doc_id, resolution=doc.derived.resolution or 128)
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
    solid = document_service.get_solid(doc_id, resolution=doc.derived.resolution or 128)
    exact = solid.attrs.get("exact_measurements")
    if exact is not None:
        return _exact_report(doc, exact)
    verts, faces = _tri_mesh(doc_id)
    if len(faces) == 0:
        raise ValueError("The part has no solid geometry to measure.")

    # **A sheet is measured, not refused, and not weighed.** Every figure below
    # this line integrates over an enclosed volume, and a surface has none — the
    # signed-tetrahedron sum would return whatever the mesh's arbitrary closure
    # implies, which is a number about the triangulation rather than about the
    # part. So the sheet answers with what it does have (area, and the centroid
    # of that area) and says `kind` rather than reporting a mass of zero, which
    # reads as "no material assigned" and is the wrong thing to conclude.
    if str(doc.derived.body_kind) == "sheet":
        return _sheet_properties(doc, verts, faces)

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

    mat = materials.get_material(doc.material_id)
    return {
        # Stated rather than inferred, for the same reason
        # `observation.summarize_assembly` states its own: a caller that decides
        # which shape it is holding from the presence of `volume_cm3` will
        # eventually decide wrong.
        "kind": "solid",
        "material": {"id": mat.id, "name": mat.name, "density": mat.density},
        "volume_cm3": round(volume * 1e6, 6),
        "mass_kg": round(mass, 9),
        "surface_area_cm2": round(surface_area * 1e4, 5),
        "center_of_mass_mm": [round(float(v) * 1000, 5) for v in centroid],
        # Inertia tensor about the COG, kg·mm² (SI * 1e6).
        "inertia_kg_mm2": [[round(float(inertia_cog[i, j] * 1e6), 3) for j in range(3)] for i in range(3)],
        "bbox_mm": _bbox_mm(verts),
        # Whether these came from exact geometry or from a triangulation of it.
        "exact": False,
    }


def _sheet_properties(doc, verts: np.ndarray, faces: np.ndarray) -> Dict:
    """Area, area centroid and extent for a body that bounds no volume.

    The same shape the solid answer has, so a caller reads one dictionary either
    way, with `kind` saying which and the volume-derived keys absent rather than
    zero. Absent is the honest form: a zero mass invites "assign a material",
    and there is no material that gives a surface a weight.
    """
    mat = materials.get_material(doc.material_id)
    a, b, c = verts[faces[:, 0]], verts[faces[:, 1]], verts[faces[:, 2]]
    areas = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    total = float(areas.sum())
    centroid = (((a + b + c) / 3.0) * areas[:, None]).sum(axis=0) / (total or 1.0)
    return {
        "kind": "sheet",
        "material": {"id": mat.id, "name": mat.name, "density": mat.density},
        "surface_area_cm2": round(total * 1e4, 5),
        # Of the *area*, not of a mass: a sheet has no mass to have a centre of.
        "centroid_mm": [round(float(v) * 1000, 5) for v in centroid],
        "bbox_mm": _bbox_mm(verts),
        "exact": False,
        "note": ("this body is surface with no enclosed volume, so it has no "
                 "volume, no mass and no inertia tensor. Thicken it to a solid "
                 "if you need those"),
    }


def _exact_report(doc, measured: dict) -> Dict:
    mat = materials.get_material(doc.material_id)
    lo, hi = np.asarray(measured["bbox"]["min"]), np.asarray(measured["bbox"]["max"])
    report = {
        "kind": measured["kind"],
        "material": {"id": mat.id, "name": mat.name, "density": mat.density},
        "surface_area_cm2": round(measured["area"] * 1e4, 5),
        "bbox_mm": _bbox_mm(np.array([lo, hi])),
        "exact": True,
    }
    center_mm = [round(v * 1000, 5) for v in measured["center"]]
    if measured["kind"] == "sheet":
        report.update(centroid_mm=center_mm,
                      note="This surface has no enclosed volume, mass or inertia. Thicken it to a solid to measure those.")
    else:
        density = materials.density_of(doc.material_id)
        report.update(
            volume_cm3=round(measured["volume"] * 1e6, 6),
            mass_kg=round(measured["volume"] * density, 9),
            center_of_mass_mm=center_mm,
            inertia_kg_mm2=[[round(v * density * 1e6, 3) for v in row]
                            for row in measured["inertia"]],
        )
    return report


def _bbox_mm(verts: np.ndarray) -> Dict:
    lo = verts.min(axis=0) * 1000.0
    hi = verts.max(axis=0) * 1000.0
    return {"min": [round(float(v), 3) for v in lo],
            "max": [round(float(v), 3) for v in hi],
            "size": [round(float(hi[i] - lo[i]), 3) for i in range(3)]}
