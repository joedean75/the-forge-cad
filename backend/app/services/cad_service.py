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
    edge_lengths,
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


def _analysis_for_axis(verts: np.ndarray, faces: np.ndarray, build_dir: np.ndarray) -> dict:
    normals = face_normals(verts, faces)
    areas = np.linalg.norm(np.cross(verts[faces[:, 1]] - verts[faces[:, 0]], verts[faces[:, 2]] - verts[faces[:, 0]]), axis=1) * 0.5
    dots = normals @ (build_dir / (np.linalg.norm(build_dir) + 1e-12))

    # Overhang: faces pointing downward more than 45 deg
    threshold = -np.cos(np.radians(45.0))
    mask = dots < threshold
    overhang_area = float(areas[mask].sum())
    max_angle = 0.0
    if np.any(mask):
        max_angle = float(np.degrees(np.arccos(np.clip(-dots[mask].min(), -1.0, 1.0))))

    return {
        "overhang_area": overhang_area,
        "max_overhang_angle": max_angle,
    }


def kernel_manufacturing_analysis(solid: Solid, build_axis: Optional[np.ndarray] = None) -> dict:
    """Compute manufacturing metrics from a kernel tessellation."""
    mesh = solid_to_mesh(solid)
    verts = mesh.vertices
    faces = mesh.faces

    if faces.size == 0:
        return {
            "overhang_area": 0.0,
            "max_overhang_angle": 0.0,
            "support_volume": 0.0,
            "total_area": 0.0,
            "total_volume": 0.0,
            "min_edge": 0.0,
        }

    if build_axis is None:
        build_axis = np.array([0.0, 0.0, 1.0])

    axis_stats = _analysis_for_axis(verts, faces, build_axis)
    overhang_area = axis_stats["overhang_area"]
    max_angle = axis_stats["max_overhang_angle"]

    total_area = float(mesh_area(verts, faces))
    total_volume = abs(mesh_volume(verts, faces))
    support_volume = overhang_area * 5e-3

    lengths = edge_lengths(verts, faces)
    min_edge = float(lengths.min()) if lengths.size else 0.0

    return {
        "overhang_area": overhang_area,
        "max_overhang_angle": max_angle,
        "support_volume": support_volume,
        "total_area": total_area,
        "total_volume": total_volume,
        "min_edge": min_edge,
    }
