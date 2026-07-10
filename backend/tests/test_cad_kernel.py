import math

import numpy as np

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.kernel import Kernel
from app.cad_kernel.geom.meshing import mesh_volume
from app.cad_kernel.io.stl import write_stl_binary
from app.cad_kernel.io.step import write_step
from app.cad_kernel.ops.primitives import BoxFeature, SphereFeature, CylinderFeature
from app.cad_kernel.ops.booleans import BooleanFeature
from app.cad_kernel.core.types import Vector3, Point3


def test_box_volume():
    solid = Kernel.box(size=Vector3(2, 3, 4))
    mesh = Kernel.tessellate(solid)
    vol = abs(mesh_volume(mesh.vertices, mesh.faces))
    assert abs(vol - 24.0) < 1e-3, vol


def test_sphere_volume():
    solid = Kernel.sphere(radius=1.0)
    mesh = Kernel.tessellate(solid)
    vol = abs(mesh_volume(mesh.vertices, mesh.faces))
    assert abs(vol - 4.0 / 3.0 * math.pi) < 0.05, vol


def test_cylinder_volume():
    solid = Kernel.cylinder(radius=1.0, height=2.0)
    mesh = Kernel.tessellate(solid)
    vol = abs(mesh_volume(mesh.vertices, mesh.faces))
    assert abs(vol - math.pi * 2.0) < 0.05, vol


def test_sdf_union():
    a = BoxFeature(size=Vector3(2, 2, 2))
    b = BoxFeature(size=Vector3(2, 2, 2), center=Point3(1, 0, 0))
    tree = FeatureTree(BooleanFeature(operation="union", children=[a, b]))
    solid = Kernel.evaluate_tree(tree, resolution=160)
    vol = abs(mesh_volume(Kernel.tessellate(solid).vertices, Kernel.tessellate(solid).faces))
    assert abs(vol - 12.0) < 0.5, vol


def test_sdf_subtract():
    a = BoxFeature(size=Vector3(2, 2, 2))
    b = SphereFeature(radius=1.25, center=Point3(0, 0, 0))
    tree = FeatureTree(BooleanFeature(operation="subtract", children=[a, b]))
    solid = Kernel.evaluate_tree(tree, resolution=160)
    vol = abs(mesh_volume(Kernel.tessellate(solid).vertices, Kernel.tessellate(solid).faces))
    # Half the sphere (volume ~4.09) removed from cube (8) -> ~3.91
    assert 0.5 < vol < 3.0, vol


def test_sdf_intersect():
    a = BoxFeature(size=Vector3(2, 2, 2))
    b = BoxFeature(size=Vector3(2, 2, 2), center=Point3(1, 0, 0))
    tree = FeatureTree(BooleanFeature(operation="intersect", children=[a, b]))
    solid = Kernel.evaluate_tree(tree, resolution=160)
    vol = abs(mesh_volume(Kernel.tessellate(solid).vertices, Kernel.tessellate(solid).faces))
    assert abs(vol - 4.0) < 0.5, vol


def test_stl_export():
    solid = Kernel.box(size=Vector3(1, 1, 1))
    data = write_stl_binary(solid)
    assert len(data) >= 84
    count = int.from_bytes(data[80:84], "little")
    assert count == 12


def test_step_export():
    solid = Kernel.box(size=Vector3(1, 1, 1))
    text = write_step(solid)
    assert "ISO-10303-21" in text
    assert "MANIFOLD_SOLID_BREP" in text
    assert "CLOSED_SHELL" in text


if __name__ == "__main__":
    test_box_volume()
    test_sphere_volume()
    test_cylinder_volume()
    test_sdf_union()
    test_sdf_subtract()
    test_sdf_intersect()
    test_stl_export()
    test_step_export()
    print("All kernel tests passed")
