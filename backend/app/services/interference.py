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
    """A component's SDF + world-space surface samples + placement frame."""

    def __init__(self, comp):
        doc = document_service.get_document(comp.part_id)
        active = feature_ops.resolve_datums(feature_ops.resolve_suppressed(doc.feature_tree))
        self.sdf = sdf_from_feature(FeatureTree.from_dict(active).root)

        from app.cad_kernel.kernel import Kernel
        mesh = Kernel.tessellate(document_service.get_solid(comp.part_id))
        verts = _surface_samples(mesh)
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

    def signed_distance_at(self, world_pts: np.ndarray) -> np.ndarray:
        """Signed distance (world units) from this part's surface to given points."""
        local = ((self.R.T @ (world_pts - self.t).T).T) / self.scale
        return self.sdf(local) * self.scale


def _aabb_gap(a: _Placed, b: _Placed) -> float:
    """Largest per-axis separation of two world AABBs (>0 = definitely apart)."""
    return float(np.max(np.maximum(a.aabb_min - b.aabb_max, b.aabb_min - a.aabb_max)))


def check_assembly(asm_id: str, clearance_mm: float = 1.0) -> Dict:
    """Pairwise interference + clearance across an assembly's placed components."""
    asm = assembly_service.get_assembly(asm_id)
    if asm is None:
        raise KeyError(asm_id)
    comps = [c for c in asm.components if not c.missing]
    placed: List[_Placed] = []
    for c in comps:
        try:
            placed.append(_Placed(c))
        except Exception:
            continue

    clearance_m = clearance_mm / MM
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
            if gap < -1e-6:
                status = "interference"
                interferences += 1
            elif gap < clearance_m:
                status = "clearance"
                warnings += 1
            else:
                continue
            pairs.append({
                "a": a.name, "b": b.name, "status": status,
                "gap_mm": round(gap * MM, 3),
                "penetration_mm": round(-gap * MM, 3) if gap < 0 else 0.0,
            })

    pairs.sort(key=lambda p: p["gap_mm"])
    return {
        "assembly_id": asm_id,
        "components_checked": len(placed),
        "interferences": interferences,
        "clearance_warnings": warnings,
        "clearance_threshold_mm": clearance_mm,
        "pairs": pairs,
        "clear": interferences == 0 and warnings == 0,
    }
