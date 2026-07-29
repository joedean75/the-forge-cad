"""Public entry point for the Forge CAD kernel."""

from __future__ import annotations

import uuid
from typing import Optional

import numpy as np

from app.cad_kernel.core.feature_tree import Feature, FeatureTree
from app.cad_kernel.core.topology import Face, FaceMesh, Shell, Solid
from app.cad_kernel.core.types import Point3, Vector3
from app.cad_kernel.evaluator.sdf import evaluate_sdf, sdf_from_feature
from app.cad_kernel.geom.meshing import solid_to_mesh
from app.cad_kernel.ops.primitives import make_box, make_cylinder, make_cone, make_sphere


class Kernel:
    """High-level CAD kernel facade."""

    @staticmethod
    def box(size: Vector3, center: Point3 = Point3()) -> Solid:
        return make_box(size, center)

    @staticmethod
    def sphere(radius: float, center: Point3 = Point3()) -> Solid:
        return make_sphere(radius, center)

    @staticmethod
    def cylinder(radius: float, height: float, center: Point3 = Point3(), axis: Vector3 = Vector3(0.0, 0.0, 1.0)) -> Solid:
        return make_cylinder(radius, height, center, axis)

    @staticmethod
    def cone(base_radius: float, height: float, center: Point3 = Point3(), axis: Vector3 = Vector3(0.0, 0.0, 1.0)) -> Solid:
        return make_cone(base_radius, height, center, axis)

    @staticmethod
    def from_mesh(vertices: np.ndarray, faces: np.ndarray, id: Optional[str] = None) -> Solid:
        """Wrap an arbitrary triangle mesh as a single-faced solid."""
        mesh = FaceMesh(np.array(vertices, dtype=float), np.array(faces, dtype=int))
        face = Face(surface=None, mesh=mesh, id=str(uuid.uuid4()))
        return Solid(Shell([face]), id=id or str(uuid.uuid4()))

    @staticmethod
    def evaluate_feature(feature: Feature, resolution: int = 128) -> Solid:
        """Evaluate a feature tree into a solid mesh via SDF.

        Uses the native Rust sampler for box/sphere/cylinder+boolean trees (the
        common, hot case); falls back to the numpy SDF for everything else.
        """
        from app.cad_kernel.evaluator.sdf_native import evaluate_feature_native

        native = evaluate_feature_native(feature, resolution=resolution)
        if native is not None:
            return native
        return evaluate_sdf(sdf_from_feature(feature), resolution=resolution)

    @staticmethod
    def sampler_for(feature: Feature) -> str:
        """Which field sampler ``evaluate_feature`` will use: "native" or "numpy".

        The two samplers share the grid and the marching-cubes step but evaluate
        the field differently, so a mesh is only comparable across machines when
        this matches. Callers record it alongside the geometry.
        """
        from app.cad_kernel.evaluator import sdf_native

        if sdf_native.AVAILABLE and sdf_native.compile_feature(feature) is not None:
            return "native"
        return "numpy"

    @staticmethod
    def evaluate_tree(tree: FeatureTree, resolution: int = 128) -> Solid:
        return Kernel.evaluate_feature(tree.root, resolution=resolution)

    @staticmethod
    def tessellate(solid: Solid) -> FaceMesh:
        return solid_to_mesh(solid)
