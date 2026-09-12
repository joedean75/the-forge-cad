"""Display quality must not change the engineering answer for an exact body."""
import math

import numpy as np
import pytest

from app.cad_kernel.evaluator import brep
from app.cad_kernel.geom.measurements import exact_measurements
from app.services import design_spec, document_service as docs, mass_properties, materials
from conftest import whole_tree_document

pytestmark = pytest.mark.skipif(not brep.AVAILABLE, reason="requires OpenCascade")


def sphere_tree():
    return {"root": {"id": "ball", "type": "SphereFeature", "params": {
        "radius": 0.01, "center": {"x": 0.003, "y": -0.007, "z": 0.02}}}}


def test_curved_body_readings_and_spec_are_independent_of_display_quality():
    doc = whole_tree_document(sphere_tree(), resolution=16)
    volume = 4 * math.pi * 0.01 ** 3 / 3
    doc.spec = {"requirements": [
        {"id": "width", "kind": "bounding_box", "axis": "x", "value": 20, "tolerance": 0.001},
        {"id": "volume", "kind": "volume", "value": volume * 1e6, "tolerance": 0.00001},
    ]}
    samples, hashes = [], []
    for resolution in (16, 128):
        docs.evaluate_document(doc, resolution=resolution)
        assert doc.derived.error is None
        assert doc.derived.measurement_basis == "exact"
        assert doc.derived.volume == pytest.approx(volume, rel=1e-9)
        assert doc.derived.area == pytest.approx(4 * math.pi * 0.01 ** 2, rel=1e-9)
        assert doc.derived.bbox.min.x == pytest.approx(-0.007, abs=1e-7)
        assert doc.derived.bbox.max.x == pytest.approx(0.013, abs=1e-7)
        assert design_spec.verify(doc)["met"]
        report = mass_properties.mass_properties(doc.id)
        assert doc.derived.resolution == resolution
        assert report["mass_kg"] == pytest.approx(doc.derived.mass, abs=1e-9)
        assert report["center_of_mass_mm"] == pytest.approx([3, -7, 20])
        expected_inertia = 2 / 5 * volume * materials.density_of(doc.material_id) * 0.01 ** 2 * 1e6
        assert np.diag(report["inertia_kg_mm2"]) == pytest.approx([expected_inertia] * 3, abs=0.0005)
        samples.append((doc.derived.volume, doc.derived.area, report))
        hashes.append(doc.derived.mesh_hash)
    assert samples[0] == samples[1]
    assert hashes[0] != hashes[1]


def test_rotated_cylinder_bounds_and_volume_use_surfaces():
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    axis = np.array([1, 2, 3], dtype=float)
    axis /= np.linalg.norm(axis)
    radius, height = 0.006, 0.04
    origin = np.array([0.01, -0.03, 0.02])
    shape = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*origin), gp_Dir(*axis)), radius, height).Shape()
    measured = exact_measurements(shape, "solid")
    extent = radius * np.sqrt(1 - axis ** 2)
    assert measured["bbox"]["min"] == pytest.approx(origin - extent, abs=1e-7)
    assert measured["bbox"]["max"] == pytest.approx(origin + height * axis + extent, abs=1e-7)
    assert measured["volume"] == pytest.approx(math.pi * radius ** 2 * height, rel=1e-9)


def test_mesh_fallback_is_labelled_and_retains_mesh_measurements(monkeypatch):
    monkeypatch.setattr(brep, "AVAILABLE", False)
    doc = whole_tree_document(sphere_tree(), resolution=24)
    assert doc.derived.error is None
    assert doc.derived.measurement_basis == "mesh"
    assert doc.derived.sampler in ("native", "numpy")
    assert doc.derived.volume > 0
    assert not mass_properties.mass_properties(doc.id)["exact"]


def test_exact_sheet_has_area_but_no_volume_or_inertia():
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
    from OCP.gp import gp_Pln, gp_Pnt, gp_Dir

    shape = BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 0, 0.05, 0, 0.03).Face()
    measured = exact_measurements(shape, "sheet")
    assert measured["area"] == pytest.approx(0.0015)
    assert measured["center"] == pytest.approx([0.025, 0.015, 0])
    assert measured["volume"] == 0
    assert measured["inertia"] is None
