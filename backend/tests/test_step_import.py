"""STEP/IGES import as exact B-rep — the other half of exact STEP export.

The point of these tests is not that a file loads. It is that an *imported* part
is the same kind of object as a modelled one: measured exactly rather than
sampled, with faces and edges you can name and put a fillet on. A voxelised
import passes "it loaded" and fails every assertion below.
"""

import io
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

from main import app
from app.cad_kernel.evaluator import brep
from app.cad_kernel.io import step_brep, step_import
from app.services import cad_operations, document_analysis, import_service

client = TestClient(app)

pytestmark = pytest.mark.skipif(
    not step_import.available(), reason="exact import needs cadquery-ocp")

# 80 x 40 x 10 mm plate with a Ø10 mm bore through the middle.
_PLATE_MM3 = 80.0 * 40.0 * 10.0 - 3.14159265358979 * 5.0 ** 2 * 10.0


def _plate():
    """A plate with a bore, in metres, built directly with OCC."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 0.08, 0.04, 0.01).Shape()
    bore = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0.04, 0.02, -0.001), gp_Dir(0, 0, 1)), 0.005, 0.02).Shape()
    return BRepAlgoAPI_Cut(box, bore).Shape()


def _step_bytes(shape=None) -> bytes:
    return step_brep.write_step_brep(shape if shape is not None else _plate(),
                                     "plate").encode()


def _iges_bytes(shape=None, brep_mode: int = 1) -> bytes:
    """IGES for the same plate. ``brep_mode=0`` writes trimmed surfaces."""
    from OCP.IGESControl import IGESControl_Writer
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Pnt, gp_Trsf

    step_brep._quiet()
    trsf = gp_Trsf()
    trsf.SetScale(gp_Pnt(0, 0, 0), 1000.0)  # the writer's units are the file's
    scaled = BRepBuilderAPI_Transform(
        shape if shape is not None else _plate(), trsf, True).Shape()

    writer = IGESControl_Writer("MM", brep_mode)
    writer.AddShape(scaled)
    handle, path = tempfile.mkstemp(suffix=".iges")
    os.close(handle)
    try:
        writer.Write(path)
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        os.remove(path)


def _volume_mm3(doc) -> float:
    return cad_operations.dispatch("mass_properties", doc.id, {})["volume_cm3"] * 1000.0


def test_step_imports_as_exact_brep():
    doc = import_service.import_document("plate.step", _step_bytes())

    assert doc.feature_tree["root"]["type"] == "BrepBodyFeature"
    assert doc.feature_tree["root"]["params"]["source"]["exact"] is True
    assert doc.derived.error is None
    assert doc.derived.sampler == "brep"


def test_imported_step_measures_exactly():
    doc = import_service.import_document("plate.step", _step_bytes())
    props = cad_operations.dispatch("mass_properties", doc.id, {})

    assert props["exact"] is True
    # Exact, not "within a voxel" — the voxel path is off by whole percent here.
    assert props["volume_cm3"] * 1000.0 == pytest.approx(_PLATE_MM3, abs=1e-3)
    assert props["bbox_mm"]["size"] == pytest.approx([80.0, 40.0, 10.0], abs=1e-6)


def test_imported_step_has_addressable_topology():
    doc = import_service.import_document("plate.step", _step_bytes())

    faces = cad_operations.dispatch("list_faces", doc.id, {})["faces"]
    assert len(faces) == 7                      # six planes and the bore wall
    bore = [f for f in faces if f["kind"] == "cylinder"]
    assert len(bore) == 1
    assert bore[0]["radius_mm"] == pytest.approx(5.0, abs=1e-6)

    circles = cad_operations.dispatch("list_edges", doc.id, {"kind": "circle"})["edges"]
    assert len(circles) == 2                    # the bore's two rims


def test_imported_step_can_be_filleted():
    """The unlock: an imported part edits like a modelled one."""
    doc = import_service.import_document("plate.step", _step_bytes())
    rims = [e["id"] for e in
            cad_operations.dispatch("list_edges", doc.id, {"kind": "circle"})["edges"]]

    result = cad_operations.execute(
        "fillet_edges", doc.id, {"radius": 0.001, "edges": rims})

    assert result["ok"], result.get("error")
    assert result["delta"]["changed_geometry"] is True
    assert result["delta"]["volume"]["change"] < 0     # a rim fillet cuts material


def test_step_units_come_from_the_file_not_a_size_guess():
    """A 3 mm cube. The mesh path's ">5 m must be millimetres" rule reads this
    as a 3 m block; the header says millimetre and is believed instead."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    cube = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 0.003, 0.003, 0.003).Shape()
    doc = import_service.import_document("tiny.step", _step_bytes(cube))

    size = cad_operations.dispatch("mass_properties", doc.id, {})["bbox_mm"]["size"]
    assert size == pytest.approx([3.0, 3.0, 3.0], abs=1e-6)
    assert "millimetre" in doc.feature_tree["root"]["params"]["source"]["units"]


def test_explicit_units_override_the_header():
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt

    cube = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 0.01, 0.01, 0.01).Shape()
    doc = import_service.import_document("inch.step", _step_bytes(cube), units="inch")

    size = cad_operations.dispatch("mass_properties", doc.id, {})["bbox_mm"]["size"]
    assert size == pytest.approx([254.0, 254.0, 254.0], abs=1e-4)


def test_export_after_import_keeps_its_scale():
    """Regression: OCC reads `xstep.cascade.unit` through a cached global, and
    reading a STEP leaves it at metres. An export taken afterwards then applied
    the conversion twice and wrote a part 1000x too large."""
    data = _step_bytes()
    for _ in range(3):
        doc = import_service.import_document("cycle.step", data)
        size = cad_operations.dispatch("mass_properties", doc.id, {})["bbox_mm"]["size"]
        assert size == pytest.approx([80.0, 40.0, 10.0], abs=1e-6)
        data = document_analysis.exact_step_document(doc.id).encode()


def test_reexported_step_is_still_analytic():
    doc = import_service.import_document("plate.step", _step_bytes())
    text = document_analysis.exact_step_document(doc.id)

    kinds = step_brep.surface_kinds(text)
    assert kinds["CYLINDRICAL_SURFACE"] == 1     # the bore, not a facet fan
    assert kinds["ADVANCED_FACE"] == 7


def test_iges_solid_imports_exactly():
    doc = import_service.import_document("plate.iges", _iges_bytes())

    assert doc.feature_tree["root"]["type"] == "BrepBodyFeature"
    assert _volume_mm3(doc) == pytest.approx(_PLATE_MM3, abs=1e-3)


def test_iges_surface_model_is_sewn_into_a_solid():
    """IGES is a surface format; a bare face set has to be stitched back up.

    The bore is the part that matters. Sewn too tightly its wall is left out and
    the shell closes over the hole, giving a solid plate that is valid, plausible
    and wrong.
    """
    doc = import_service.import_document("surf.iges", _iges_bytes(brep_mode=0))

    source = doc.feature_tree["root"]["params"]["source"]
    assert source["solids"] == 1 and source["faces"] == 7
    assert _volume_mm3(doc) == pytest.approx(_PLATE_MM3, abs=1e-3)


def test_imported_brep_survives_a_reload():
    from app.services import document_service

    doc = import_service.import_document("keep.step", _step_bytes())
    document_service.DOCUMENTS.pop(doc.id, None)
    document_service._SOLID_CACHE.pop(doc.id, None)

    reloaded = document_service.evaluate_document(
        document_service.get_document(doc.id), resolution=64)
    assert reloaded.derived.error is None
    assert reloaded.derived.sampler == "brep"


def test_import_endpoint_accepts_step_and_iges():
    for filename, payload in (("plate.step", _step_bytes()), ("plate.iges", _iges_bytes())):
        resp = client.post(
            "/api/documents/import",
            files={"file": (filename, io.BytesIO(payload), "application/octet-stream")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["provenance"] == "imported"
        assert body["feature_tree"]["root"]["type"] == "BrepBodyFeature"


def test_unreadable_iges_reports_why():
    """IGES has no fallback path, so the reason has to reach the caller."""
    resp = client.post(
        "/api/documents/import",
        files={"file": ("junk.iges", io.BytesIO(b"not an iges file"), "text/plain")},
    )
    assert resp.status_code == 422
    assert "geometry" in resp.json()["detail"].lower()


def test_our_own_faceted_step_round_trips_faithfully():
    """Importing a STEP this app wrote must reproduce the body it described.

    Whether that happens exactly or via the voxel fallback depends on the
    OpenCascade build: some versions read only part of a faceted shell and the
    reader has to decline, others read it whole. Both are acceptable — importing
    a *wrong* body is not — so this asserts the geometry, not which path ran.
    """
    import numpy as np
    import trimesh

    from app.cad_kernel.core.topology import FaceMesh
    from app.cad_kernel.io.step import write_step

    box = trimesh.creation.box(extents=[0.1, 0.06, 0.02])
    mesh = FaceMesh(vertices=np.asarray(box.vertices), faces=np.asarray(box.faces))
    data = write_step(mesh).encode()

    try:
        _shape, info = step_import.read_exact(data, "step")
        # If it reads at all, it must read the whole thing — a partial shell is
        # the failure mode the fallback exists for.
        assert info["faces"] == len(mesh.faces), "exact read dropped faces"
    except step_import.ExactImportError:
        pass  # declined; the importer falls back to voxels below

    doc = import_service.import_document("faceted.step", data)
    assert doc.derived.error is None
    assert doc.feature_tree["root"]["type"] in ("BrepBodyFeature", "ImportFeature")

    bbox = doc.derived.bbox
    for axis, expected_mm in (("x", 100.0), ("y", 60.0), ("z", 20.0)):
        size_mm = (getattr(bbox.max, axis) - getattr(bbox.min, axis)) * 1000.0
        assert size_mm == pytest.approx(expected_mm, abs=1.0), f"{axis} is {size_mm} mm"


def test_read_exact_rejects_a_mesh_format():
    with pytest.raises(step_import.ExactImportError):
        step_import.read_exact(b"solid x\nendsolid x\n", "stl")


def test_serialized_payload_round_trips_the_shape():
    shape = _plate()
    restored = brep.deserialize(brep.serialize(shape))

    assert brep.volume(restored) == pytest.approx(brep.volume(shape), rel=1e-12)
    assert brep.is_valid(restored)
