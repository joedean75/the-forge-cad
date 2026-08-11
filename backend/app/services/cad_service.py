"""Mesh measurement and STEP text, for `services/document_analysis`.

What is left here after the candidate-based flow was removed: the manufacturing
metrics DFM and the quote engine read off a mesh, and the faceted STEP writer.

Three functions went with that flow and are gone — `feature_tree_from_spec`
(it took the *generative* `DesignSpec`, which no document has held since the
`DesignDocument` rewrite), `generate_kernel_solid`, and `solid_to_stl_data_uri`,
whose data URI stopped being how a mesh reaches the browser when P1 made it a
hash-keyed URL. Nothing had called any of them for several programmes.
"""

from __future__ import annotations

from typing import Optional

import numpy as np

from app.cad_kernel.core.topology import Solid
from app.cad_kernel.geom.meshing import (
    bounding_box,
    min_edge_length,
    face_normals,
    mesh_area,
    mesh_volume,
    solid_to_mesh,
)
from app.cad_kernel.io.step import write_step


def solid_to_step_string(solid: Solid, name: str = "part") -> str:
    """Return a STEP AP214 string for the solid."""
    return write_step(solid, name)


_EMPTY_ANALYSIS = {
    "overhang_area": 0.0,
    "max_overhang_angle": 0.0,
    "support_volume": 0.0,
    "total_area": 0.0,
    "total_volume": 0.0,
    "min_edge": 0.0,
}


class MeshAnalysis:
    """Per-mesh manufacturing metrics, split by what depends on the build axis.

    Total area, volume and minimum feature size are properties of the mesh; only
    overhang depends on which way up it is printed. Separating them means an
    orientation search costs one face-normal pass per candidate instead of a full
    re-analysis — the auto-axis search used to redo *everything* three times and
    then once more for the winner.
    """

    def __init__(self, solid: Solid):
        mesh = solid_to_mesh(solid)
        self.verts = mesh.vertices
        self.faces = mesh.faces
        self.empty = self.faces.size == 0
        if self.empty:
            return
        # Computed once and reused across every candidate orientation.
        self.normals = face_normals(self.verts, self.faces)
        edges1 = self.verts[self.faces[:, 1]] - self.verts[self.faces[:, 0]]
        edges2 = self.verts[self.faces[:, 2]] - self.verts[self.faces[:, 0]]
        self.areas = np.linalg.norm(np.cross(edges1, edges2), axis=1) * 0.5
        self.total_area = float(self.areas.sum())
        self.total_volume = abs(mesh_volume(self.verts, self.faces))
        self.min_edge = min_edge_length(self.verts, self.faces)

    def overhang(self, build_dir: np.ndarray) -> tuple[float, float]:
        """``(overhang_area, max_overhang_angle)`` for one build direction."""
        if self.empty:
            return 0.0, 0.0
        dots = self.normals @ (build_dir / (np.linalg.norm(build_dir) + 1e-12))
        # Faces pointing downward more than 45 deg need support.
        mask = dots < -np.cos(np.radians(45.0))
        if not np.any(mask):
            return 0.0, 0.0
        area = float(self.areas[mask].sum())
        angle = float(np.degrees(np.arccos(np.clip(-dots[mask].min(), -1.0, 1.0))))
        return area, angle

    def stats_for(self, build_dir: np.ndarray) -> dict:
        if self.empty:
            return dict(_EMPTY_ANALYSIS)
        overhang_area, max_angle = self.overhang(build_dir)
        return {
            "overhang_area": overhang_area,
            "max_overhang_angle": max_angle,
            "support_volume": overhang_area * 5e-3,
            "total_area": self.total_area,
            "total_volume": self.total_volume,
            "min_edge": self.min_edge,
        }


def kernel_manufacturing_analysis(solid: Solid, build_axis: Optional[np.ndarray] = None) -> dict:
    """Compute manufacturing metrics from a kernel tessellation.

    Callers analysing several orientations should build a :class:`MeshAnalysis`
    once and call ``stats_for`` per axis instead of calling this repeatedly.
    """
    if build_axis is None:
        build_axis = np.array([0.0, 0.0, 1.0])
    return MeshAnalysis(solid).stats_for(build_axis)
