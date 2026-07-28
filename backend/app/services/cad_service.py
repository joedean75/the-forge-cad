"""High-level CAD kernel service used by FastAPI routers.

This layer isolates the kernel from the web layer so routers can stay thin
and the kernel can be tested independently.
"""

from __future__ import annotations

import base64
from io import BytesIO
from typing import List, Optional

import numpy as np

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.core.topology import Solid
from app.cad_kernel.core.types import Point3, Vector3
from app.cad_kernel.evaluator.sdf import evaluate_sdf, sdf_from_feature
from app.cad_kernel.geom.meshing import (
    bounding_box,
    min_edge_length,
    face_normals,
    mesh_area,
    mesh_volume,
    solid_to_mesh,
)
from app.cad_kernel.io.stl import write_stl_binary
from app.cad_kernel.io.step import write_step
from app.cad_kernel.ops.booleans import BooleanFeature
from app.cad_kernel.ops.primitives import BoxFeature, CylinderFeature, SphereFeature
from app.models.schemas import DesignSpec, PreserveRegion, ObstacleRegion, LoadCase


def _vec(v) -> Vector3:
    return Vector3(x=float(v.x), y=float(v.y), z=float(v.z))


def _pt(v) -> Point3:
    return Point3(x=float(v.x), y=float(v.y), z=float(v.z))


def feature_tree_from_spec(spec: DesignSpec) -> FeatureTree:
    """Build a kernel feature tree from a DesignSpec.

    Preserve regions become solid spheres, loads become small interface pads,
    and obstacle regions are subtracted.
    """
    features: List = []

    for region in spec.preserve_regions:
        if region.type == "sphere":
            features.append(
                SphereFeature(
                    radius=float(region.radius),
                    center=_pt(region.center),
                    name=region.name,
                )
            )
        elif region.type == "cylinder":
            # Represent a preserve cylinder as a sphere of equivalent radius
            features.append(
                SphereFeature(
                    radius=float(region.radius),
                    center=_pt(region.center),
                    name=region.name,
                )
            )

    for load in spec.load_cases:
        # Small spherical load pad
        features.append(
            SphereFeature(
                radius=0.12,
                center=_pt(load.point),
                name=load.name,
            )
        )

    if not features:
        # Fallback: a single sphere so the kernel always returns geometry
        features.append(
            SphereFeature(radius=0.5, center=Point3(), name="Default")
        )

    positive = BooleanFeature(operation="union", children=features, name="preserves")

    obstacles: List = []
    for region in spec.obstacle_regions:
        if region.type == "sphere":
            obstacles.append(
                SphereFeature(
                    radius=float(region.radius or 0.1),
                    center=_pt(region.center),
                    name=region.name,
                )
            )
        elif region.type == "box" and region.size:
            obstacles.append(
                BoxFeature(
                    size=_vec(region.size),
                    center=_pt(region.center),
                    name=region.name,
                )
            )

    if obstacles:
        negative = BooleanFeature(operation="union", children=obstacles, name="obstacles")
        root = BooleanFeature(operation="subtract", children=[positive, negative], name="design")
    else:
        root = positive

    return FeatureTree(root)


def generate_kernel_solid(spec: DesignSpec, resolution: int = 120, level: float = 0.0) -> Solid:
    """Generate a solid mesh from a DesignSpec using the SDF evaluator."""
    tree = feature_tree_from_spec(spec)
    return evaluate_sdf(sdf_from_feature(tree.root), resolution=resolution, level=level)


def solid_to_stl_data_uri(solid: Solid) -> str:
    """Return a base64 data URI for an STL of the solid."""
    data = write_stl_binary(solid)
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:application/octet-stream;base64,{b64}"


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
