"""STEP assembly import: a file's product structure becomes parts and placements.

The point is not that a file loads. It is that the assembly that comes out is
the one that was exported: one part per distinct part (so the BOM counts two
pins as qty 2), every instance where the file put it, compound rotations in the
convention the rest of the backend reads, and nothing mirrored placed as its
unmirrored twin.
"""

import json
import math
import os
import tempfile

import numpy as np
import pytest
from fastapi.testclient import TestClient

from main import app
from app import body_limit
from app.cad_kernel.evaluator.sdf import _euler_matrix
from app.cad_kernel.io import step_brep, step_import
from app.services import cad_operations, document_service, import_service

client = TestClient(app)

pytestmark = pytest.mark.skipif(
    not step_import.available(), reason="assembly import needs cadquery-ocp")


# ---------------------------------------------------------------------------
# Writing real STEP assemblies, in millimetres
# ---------------------------------------------------------------------------

def _doc():
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    doc = TDocStd_Document(TCollection_ExtendedString("XmlXCAF"))
    XCAFApp_Application.GetApplication_s().InitDocument(doc)
    return doc, XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())


def _name(label, text):
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name

    TDataStd_Name.Set_s(label, TCollection_ExtendedString(text))
    return label


def _box(x, y, z):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    return BRepPrimAPI_MakeBox(x, y, z).Shape()


def _trsf(tx=0.0, ty=0.0, tz=0.0, rx=0.0, rz=0.0, mirror_x=False):
    """translate . Rz . Rx . mirror, applied right to left."""
    from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

    out = gp_Trsf()
    out.SetTranslation(gp_Vec(tx, ty, tz))
    for axis, deg in ((gp_Dir(0, 0, 1), rz), (gp_Dir(1, 0, 0), rx)):
        r = gp_Trsf()
        r.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), axis), math.radians(deg))
        out = out.Multiplied(r)
    if mirror_x:
        m = gp_Trsf()
        m.SetMirror(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)))
        out = out.Multiplied(m)
    return out


def _loc(**kw):
    from OCP.TopLoc import TopLoc_Location

    return TopLoc_Location(_trsf(**kw))


def _write(doc) -> bytes:
    from OCP.Interface import Interface_Static
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs

    step_brep._quiet()
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    writer = STEPCAFControl_Writer()
    writer.SetNameMode(True)
    assert writer.Transfer(doc, STEPControl_AsIs)
    handle, path = tempfile.mkstemp(suffix=".step")
    os.close(handle)
    try:
        writer.Write(path)
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        os.remove(path)


def _bracket_step() -> bytes:
    """A 60x40x5 plate and two pins, the second turned 90 degrees about Z."""
    doc, tool = _doc()
    asm = _name(tool.NewShape(), "Bracket assembly")
    plate = _name(tool.AddShape(_box(60, 40, 5), False), "Base plate")
    pin = _name(tool.AddShape(_box(8, 4, 30), False), "Pin")
    tool.AddComponent(asm, plate, _loc())
    tool.AddComponent(asm, pin, _loc(tx=10, ty=10, tz=5))
    tool.AddComponent(asm, pin, _loc(tx=50, ty=30, tz=5, rz=90))
    tool.UpdateAssemblies()
    return _write(doc)


def _world_bbox_of_component(component, part):
    """The placed part's bounding box, applying the transform the way the
    interference check does: R (scale * p) + t, with R = Rz . Ry . Rx."""
    b = part.derived.bbox
    corners = np.array([[x, y, z] for x in (b.min.x, b.max.x)
                        for y in (b.min.y, b.max.y) for z in (b.min.z, b.max.z)])
    t = component.transform
    R = _euler_matrix(t.rotate_deg.x, t.rotate_deg.y, t.rotate_deg.z)
    world = (R @ (t.scale * corners).T).T + [t.translate.x, t.translate.y, t.translate.z]
    return world.min(axis=0), world.max(axis=0)


def _occ_bbox_mm(shape, trsf):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform

    placed = BRepBuilderAPI_Transform(shape, trsf, True).Shape()
    box = Bnd_Box()
    box.SetGap(0.0)
    BRepBndLib.Add_s(placed, box, False)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return np.array([x0, y0, z0]), np.array([x1, y1, z1])


def _by_name(asm):
    out = {}
    for c in asm.components:
        out.setdefault(c.name, []).append(c)
    return out


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

def test_a_shared_part_becomes_one_document_placed_twice():
    asm, report = import_service.import_assembly("bracket.step", _bracket_step())
    assert report["structured"] is True
    assert report["parts_created"] == 2 and report["instances"] == 3
    groups = _by_name(asm)
    assert set(groups) == {"Base plate", "Pin"}
    pins = groups["Pin"]
    assert len(pins) == 2 and pins[0].part_id == pins[1].part_id
    assert asm.name == "Bracket assembly"


def test_the_bom_counts_the_shared_part_as_qty_two():
    r = client.post("/api/assemblies/import",
                    files={"file": ("bracket.step", _bracket_step(), "application/step")})
    assert r.status_code == 200, r.text
    asm_id = r.json()["assembly"]["id"]
    bom = client.get(f"/api/assemblies/{asm_id}/bom")
    assert bom.status_code == 200, bom.text
    quantities = {line["name"]: line["quantity"] for line in bom.json()["lines"]}
    assert quantities == {"Base plate": 1, "Pin": 2}


def test_placements_arrive_in_metres_and_degrees():
    asm, _ = import_service.import_assembly("bracket.step", _bracket_step())
    pins = sorted(_by_name(asm)["Pin"], key=lambda c: c.transform.translate.x)
    first, turned = pins[0].transform, pins[1].transform
    assert (first.translate.x, first.translate.y, first.translate.z) == pytest.approx((0.010, 0.010, 0.005))
    assert first.rotate_deg.z == pytest.approx(0.0, abs=1e-9)
    assert (turned.translate.x, turned.translate.y) == pytest.approx((0.050, 0.030))
    assert turned.rotate_deg.z == pytest.approx(90.0)


def test_the_part_body_is_exact_and_measures_what_was_exported():
    asm, _ = import_service.import_assembly("bracket.step", _bracket_step())
    plate = document_service.get_document(_by_name(asm)["Base plate"][0].part_id)
    root = plate.feature_tree["root"]
    assert root["type"] == "BrepBodyFeature"
    assert root["params"]["source"]["volume_mm3"] == pytest.approx(60 * 40 * 5, rel=1e-6)
    assert plate.provenance == "imported"


def test_a_nested_subassembly_composes_its_offset_into_each_placement():
    doc, tool = _doc()
    top = _name(tool.NewShape(), "Top")
    sub = _name(tool.NewShape(), "Sub")
    block = _name(tool.AddShape(_box(10, 10, 10), False), "Block")
    tool.AddComponent(sub, block, _loc(tx=10))
    tool.AddComponent(top, sub, _loc(tx=100))
    tool.UpdateAssemblies()
    asm, _ = import_service.import_assembly("nested.step", _write(doc))
    (block_c,) = _by_name(asm)["Block"]
    assert block_c.transform.translate.x == pytest.approx(0.110)


def test_a_compound_rotation_lands_where_the_file_put_it():
    """Two axes at once is where Euler conventions disagree, and a wrong one
    places the part somewhere plausible."""
    doc, tool = _doc()
    asm_label = _name(tool.NewShape(), "Turned")
    bar = _name(tool.AddShape(_box(40, 10, 4), False), "Bar")
    placement = dict(tx=5, ty=-3, tz=12, rx=90, rz=90)
    tool.AddComponent(asm_label, bar, _loc(**placement))
    tool.UpdateAssemblies()
    asm, _ = import_service.import_assembly("turned.step", _write(doc))
    (bar_c,) = _by_name(asm)["Bar"]

    lo, hi = _world_bbox_of_component(bar_c, document_service.get_document(bar_c.part_id))
    want_lo, want_hi = _occ_bbox_mm(_box(40, 10, 4), _trsf(**placement))
    assert lo * 1000 == pytest.approx(want_lo, abs=1e-6)
    assert hi * 1000 == pytest.approx(want_hi, abs=1e-6)


def test_a_mirrored_instance_becomes_its_own_mirrored_part(monkeypatch):
    """A placement cannot reflect. Decomposed anyway, the matrix gives a
    believable rotation of the unmirrored part: a right-hand bracket where the
    file had a left-hand one.

    The reflection is injected at the reader's location seam, because a STEP
    file cannot carry one: `AXIS2_PLACEMENT_3D` is a right-handed frame, and
    OCC's writer drops a mirror from an instance location without a word
    (checked; the instance reads back as a plain translation). So this guards
    whatever else produces such a location, rather than a case STEP exports."""
    real = step_import._location_matrix
    mirror = np.diag([-1.0, 1.0, 1.0, 1.0])

    def reflect_the_turned_pin(location):
        m = real(location)
        # The second pin sits at x = 50 mm; mirror it through its own YZ plane.
        return m @ mirror if abs(m[0, 3] - 0.050) < 1e-9 else m

    monkeypatch.setattr(step_import, "_location_matrix", reflect_the_turned_pin)
    asm, report = import_service.import_assembly("bracket.step", _bracket_step())

    assert report["mirrored"] == 1 and report["parts_created"] == 3
    groups = _by_name(asm)
    assert set(groups) == {"Base plate", "Pin", "Pin (mirrored)"}
    (mirrored,) = groups["Pin (mirrored)"]
    assert mirrored.part_id != groups["Pin"][0].part_id

    lo, hi = _world_bbox_of_component(mirrored, document_service.get_document(mirrored.part_id))
    want_lo, want_hi = _occ_bbox_mm(_box(8, 4, 30), _trsf(tx=50, ty=30, tz=5, rz=90, mirror_x=True))
    assert lo * 1000 == pytest.approx(want_lo, abs=1e-6)
    assert hi * 1000 == pytest.approx(want_hi, abs=1e-6)


def test_unmirror_moves_the_reflection_into_the_part():
    m = np.diag([-1.0, 1.0, 1.0, 1.0])
    proper, mirrored = step_import.unmirror(m)
    assert mirrored and np.linalg.det(proper[:3, :3]) > 0
    assert step_import.unmirror(np.eye(4))[1] is False


def test_a_step_with_no_structure_splits_its_solids_and_says_so():
    """Many exporters flatten to one product holding every solid."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopoDS import TopoDS_Compound

    builder, compound = BRep_Builder(), TopoDS_Compound()
    builder.MakeCompound(compound)
    builder.Add(compound, _box(10, 10, 10))
    builder.Add(compound, BRepBuilderAPI_Transform(_box(10, 10, 10), _trsf(tx=50), True).Shape())

    step_brep._quiet()
    previous = Interface_Static.CVal_s("write.step.assembly")
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    # Off, or the writer wraps a compound in product structure of its own.
    Interface_Static.SetCVal_s("write.step.assembly", "0")
    try:
        writer = STEPControl_Writer()
        writer.Transfer(compound, STEPControl_AsIs)
        handle, path = tempfile.mkstemp(suffix=".step")
        os.close(handle)
        writer.Write(path)
        with open(path, "rb") as fh:
            data = fh.read()
        os.remove(path)
    finally:
        Interface_Static.SetCVal_s("write.step.assembly", previous)

    asm, report = import_service.import_assembly("flat.step", data)
    assert report["structured"] is False
    assert report["instances"] == 2 and report["parts_created"] == 2
    xs = sorted(document_service.get_document(c.part_id).derived.bbox.min.x for c in asm.components)
    assert xs == pytest.approx([0.0, 0.050])  # each solid left where the file put it


def test_a_units_override_scales_parts_and_placements_together():
    asm, _ = import_service.import_assembly("bracket.step", _bracket_step(), units="m")
    pins = sorted(_by_name(asm)["Pin"], key=lambda c: c.transform.translate.x)
    assert pins[0].transform.translate.x == pytest.approx(10.0)  # 10 "m", not 10 mm
    pin = document_service.get_document(pins[0].part_id)
    assert pin.feature_tree["root"]["params"]["source"]["volume_mm3"] == pytest.approx(
        8 * 4 * 30 * 1e9, rel=1e-6)


# ---------------------------------------------------------------------------
# What the rest of the system can do with it
# ---------------------------------------------------------------------------

def test_the_interference_check_runs_on_an_imported_assembly():
    asm, _ = import_service.import_assembly("bracket.step", _bracket_step())
    env = cad_operations.execute("check_interference", asm.id, {}, include_state=False)
    assert env["ok"], env.get("error")
    # The pins stand on the plate's top face: touching, not intersecting.
    assert int(env["result"].get("interferences") or 0) == 0


# ---------------------------------------------------------------------------
# The route and its refusals
# ---------------------------------------------------------------------------

def test_the_route_reports_what_it_made():
    r = client.post("/api/assemblies/import",
                    files={"file": ("bracket.step", _bracket_step(), "application/step")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parts_created"] == 2 and body["instances"] == 3
    assert body["structured"] is True and body["skipped"] == []
    assert len(body["assembly"]["components"]) == 3


def test_an_assembly_upload_gets_the_large_file_limit():
    """The limit is matched on the path, so a new upload route not on the list
    is refused at 8 MB, and a real supplier assembly is bigger than that."""
    assert body_limit.limit_for("/api/assemblies/import") == body_limit.UPLOAD_MAX_BYTES


def test_a_format_with_no_assembly_structure_is_refused_with_where_to_go():
    r = client.post("/api/assemblies/import",
                    files={"file": ("part.stl", b"solid x\nendsolid x\n", "model/stl")})
    assert r.status_code == 422
    assert "import them as a part" in r.text


def test_an_oversized_assembly_is_refused_before_anything_is_created(monkeypatch):
    monkeypatch.setattr(step_import, "MAX_ASSEMBLY_INSTANCES", 2)
    before = len(document_service.list_documents(owner_id=None))
    with pytest.raises(import_service.ImportError_, match="more than 2"):
        import_service.import_assembly("bracket.step", _bracket_step())
    assert len(document_service.list_documents(owner_id=None)) == before


def test_garbage_is_refused_with_a_reason():
    with pytest.raises(import_service.ImportError_):
        import_service.import_assembly("junk.step", b"ISO-10303-21;\nnot really\n")
