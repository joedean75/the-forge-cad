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
    def from_mesh(vertices: np.ndarray, faces: np.ndarray, id: Optional[str] = None,
                  owners=None, face_ids=None) -> Solid:
        """Wrap an arbitrary triangle mesh as a single-faced solid.

        ``owners`` is one feature id per triangle and ``face_ids`` one face id,
        when the evaluator knew — the two questions a click has to answer. They
        ride on ``attrs`` rather than becoming fields of `Solid`, because they
        are provenance about *this* tessellation rather than part of the
        topology, and there is exactly one face here, so the triangle order the
        caller passed is the order `solid_to_mesh` gives back.

        The irony is the point: this wrapper is why a marching-cubes body cannot
        name a face at all. What arrives here already labelled came from the
        exact kernel and kept its labels on the way through.
        """
        mesh = FaceMesh(np.array(vertices, dtype=float), np.array(faces, dtype=int))
        face = Face(surface=None, mesh=mesh, id=str(uuid.uuid4()))
        solid = Solid(Shell([face]), id=id or str(uuid.uuid4()))
        if owners is not None:
            solid.attrs["triangle_owners"] = list(owners)
        if face_ids is not None:
            solid.attrs["triangle_faces"] = list(face_ids)
        return solid

    @staticmethod
    def evaluate_feature(feature: Feature, resolution: int = 128) -> Solid:
        """Evaluate a feature tree into a solid mesh.

        Three paths, tried in order of fidelity:

        1. **Exact B-rep** (OpenCascade) when every feature in the tree has an
           analytic form. The mesh is a tessellation *of exact geometry*, so a
           dimension is right regardless of `resolution` — which now only
           controls how smoothly curves are drawn.
        2. The **native Rust SDF sampler** for box/sphere/cylinder+boolean trees.
        3. The **numpy SDF**, which handles everything else — smooth blends,
           shells, sheet metal, imported and optimised voxel bodies.

        A tree containing anything the exact kernel cannot represent falls back
        wholesale rather than mixing the two, because half a body built exactly
        and half approximated is worse than either on its own.
        """
        from app.cad_kernel.evaluator import brep
        from app.cad_kernel.evaluator.sdf_native import evaluate_feature_native

        shape, recorded = brep.build_with_history(feature)
        if shape is not None:
            # The same build, with the entity tags kept: they are what turns a
            # triangle back into the feature that made it, and re-deriving them
            # afterwards would mean tessellating the body a second time.
            vertices, faces, owners, face_ids = brep.tessellate_owned(
                shape, (recorded or {}).get("entities"), resolution=resolution)
            if len(faces):
                return Kernel.from_mesh(vertices, faces, owners=owners,
                                        face_ids=face_ids)

        if brep.requires_exact(feature):
            # There is no SDF form of an edge fillet or a face shell, so falling
            # through would swap the real reason ("radius too large for that
            # edge") for a generic one. Rebuild strictly and let it say why.
            brep.build_strict(feature)

        native = evaluate_feature_native(feature, resolution=resolution)
        if native is not None:
            return native
        return evaluate_sdf(sdf_from_feature(feature), resolution=resolution)

    @staticmethod
    def sampler_for(feature: Feature) -> str:
        """Which evaluator ``evaluate_feature`` will use.

        ``"brep"`` is exact geometry from OpenCascade; ``"native"`` and
        ``"numpy"`` are the two signed-distance-field samplers. Geometry is only
        comparable when this matches, so callers record it alongside the mesh.
        """
        from app.cad_kernel.evaluator import brep, sdf_native

        if brep.AVAILABLE and brep.supports(feature):
            return "brep"
        if sdf_native.AVAILABLE and sdf_native.compile_feature(feature) is not None:
            return "native"
        return "numpy"

    @staticmethod
    def brep_for(feature: Feature):
        """The exact shape for a tree, or None. The handle to addressable topology."""
        from app.cad_kernel.evaluator import brep

        return brep.brep_from_feature(feature)

    @staticmethod
    def evaluate_tree(tree: FeatureTree, resolution: int = 128) -> Solid:
        return Kernel.evaluate_feature(tree.root, resolution=resolution)

    @staticmethod
    def tessellate(solid: Solid) -> FaceMesh:
        return solid_to_mesh(solid)
