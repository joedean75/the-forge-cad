"""Tests for universal import (roadmap feature 02): mesh file → editable document."""

import io

import numpy as np
import trimesh
from fastapi.testclient import TestClient

from main import app
from app.services import cad_operations, document_service, import_service

client = TestClient(app)


def _box_stl(extents=(0.1, 0.06, 0.02)) -> bytes:
    return trimesh.creation.box(extents=list(extents)).export(file_type="stl")


def _bbox_mm(doc):
    b = doc.derived.bbox
    return (
        round((b.max.x - b.min.x) * 1000, 1),
        round((b.max.y - b.min.y) * 1000, 1),
        round((b.max.z - b.min.z) * 1000, 1),
    )


def test_import_stl_metres():
    doc = import_service.import_document("bracket.stl", _box_stl())
    assert doc.provenance == "imported"
    assert doc.derived.mesh_url.startswith("data:")
    x, y, z = _bbox_mm(doc)
    # Voxel bridge tolerance: within ~2 cells of the true 100x60x20.
    assert abs(x - 100) < 5 and abs(y - 60) < 5 and abs(z - 20) < 5
    assert doc.derived.mass > 0


def test_import_auto_detects_millimetres():
    data = trimesh.creation.box(extents=[100, 60, 20]).export(file_type="stl")
    doc = import_service.import_document("mm_part.stl", data)
    x, _, _ = _bbox_mm(doc)
    assert abs(x - 100) < 5  # interpreted as mm, not 100 m
    assert "mm" in doc.feature_tree["root"]["params"]["source"]["units"]


def test_import_step_roundtrip():
    from app.cad_kernel.core.topology import FaceMesh
    from app.cad_kernel.io.step import write_step

    box = trimesh.creation.box(extents=[0.1, 0.06, 0.02])
    mesh = FaceMesh(vertices=np.asarray(box.vertices), faces=np.asarray(box.faces))
    doc = import_service.import_document("part.step", write_step(mesh).encode())
    x, y, z = _bbox_mm(doc)
    assert abs(x - 100) < 5 and abs(y - 60) < 5 and abs(z - 20) < 5


def test_imported_body_composes_with_operations():
    doc = import_service.import_document("t.stl", _box_stl())
    before = doc.derived.volume
    cad_operations.dispatch(
        "add_primitive",
        doc.id,
        {"shape": "cylinder", "operation": "subtract",
         "params": {"radius": 0.012, "height": 0.08, "center": {"x": 0, "y": 0, "z": 0}}},
    )
    after = document_service.get_document(doc.id)
    assert after.derived.volume < before  # the hole removed material
    assert after.feature_tree["root"]["type"] == "BooleanFeature"


def test_import_survives_reload_from_persistence():
    doc = import_service.import_document("keep.stl", _box_stl())
    document_service.DOCUMENTS.pop(doc.id, None)  # force re-load from the datastore
    document_service._SOLID_CACHE.pop(doc.id, None)
    reloaded = document_service.evaluate_document(document_service.get_document(doc.id), resolution=64)
    assert reloaded.derived.mass > 0


def test_import_endpoint_multipart():
    resp = client.post(
        "/api/documents/import",
        files={"file": ("bracket.stl", io.BytesIO(_box_stl()), "application/octet-stream")},
        data={"units": "auto", "resolution": "64"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provenance"] == "imported"
    assert body["derived"]["mesh_url"].startswith("data:")


def test_import_endpoint_rejects_unknown_format():
    resp = client.post(
        "/api/documents/import",
        files={"file": ("cad.dwg", io.BytesIO(b"not a mesh"), "application/octet-stream")},
    )
    assert resp.status_code == 422
    assert "Unsupported format" in resp.json()["detail"]


def test_import_rejects_nurbs_step():
    nurbs = "ISO-10303-21;\nDATA;\n#1=B_SPLINE_SURFACE_WITH_KNOTS(...);\nENDSEC;\nEND-ISO-10303-21;"
    resp = client.post(
        "/api/documents/import",
        files={"file": ("nurbs.step", io.BytesIO(nurbs.encode()), "application/octet-stream")},
    )
    assert resp.status_code == 422
    assert "faceted" in resp.json()["detail"]
