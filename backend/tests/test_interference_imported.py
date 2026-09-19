"""The interference check covers every part, including ones with no analytic field.

An imported STEP solid exists only on the exact kernel, so it has no signed
distance field — and the check used to drop such a part without a word. A live
assembly reported "3 components checked, clear" with four in it, and the
unexamined one was the motor that had just been added. These pin the three
things that replaced that: the part is checked against a voxelised copy of its
mesh, a mated (touching) pair is not called a clash because of the voxels, and a
part that still cannot be read is named rather than left out of the count.
"""

import functools
import os
import tempfile

import pytest

from app.cad_kernel.io import step_import
from app.models.schemas import ComponentTransform, Vector3
from app.services import (assembly_service, cad_operations, copilot, document_service,
                          import_service, interference)

pytestmark = pytest.mark.skipif(
    not step_import.available(), reason="STEP import needs cadquery-ocp")


@functools.lru_cache(maxsize=None)
def _box_step(x_mm: float, y_mm: float, z_mm: float) -> bytes:
    """A centred box written to STEP in millimetres, the way a supplier sends one.

    A bare `STEPControl_Writer` interprets the shape through OCC's
    process-global unit settings, which earlier imports move (see CLAUDE.md):
    after `test_step_import` has run, the same 40 mm box is written as 40 m.
    Both globals are pinned to millimetres for the write and put back after, so
    this file says the same thing whatever ran first.
    """
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    names = ("xstep.cascade.unit", "write.step.unit")
    saved = {n: Interface_Static.CVal_s(n) for n in names}
    fd, path = tempfile.mkstemp(suffix=".step")
    os.close(fd)
    try:
        for n in names:
            Interface_Static.SetCVal_s(n, "MM")
        shape = BRepPrimAPI_MakeBox(gp_Pnt(-x_mm / 2, -y_mm / 2, -z_mm / 2),
                                    x_mm, y_mm, z_mm).Shape()
        writer = STEPControl_Writer()
        writer.Transfer(shape, STEPControl_AsIs)
        assert writer.Write(path) == IFSelect_RetDone
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        for n, v in saved.items():
            Interface_Static.SetCVal_s(n, v)
        os.unlink(path)


def _imported_box(name="Imported block"):
    doc = import_service.import_document("block.step", _box_step(40, 30, 20), name=name)
    root = doc.feature_tree.get("root") or doc.feature_tree
    # Guard the premise: if this ever imports as something with a field, the
    # tests below would run the old path and prove nothing.
    assert "BrepBody" in str(root.get("type")), root.get("type")
    return doc


def _modelled_box():
    doc = document_service.create_document(name="Modelled block")
    env = cad_operations.execute("add_primitive", doc.id, {
        "shape": "box", "params": {"size": {"x": 0.04, "y": 0.03, "z": 0.02}}})
    assert env["ok"], env.get("error")
    return document_service.get_document(doc.id)


def _assembly_of(first, second, dx_mm: float):
    """Two parts side by side along x, `dx_mm` between their centres."""
    asm = assembly_service.create_assembly("pair")
    asm = assembly_service.add_component(asm.id, first.id)
    asm = assembly_service.add_component(asm.id, second.id)
    moved = asm.components[1]
    assembly_service.update_component(asm.id, moved.id, ComponentTransform(
        translate=Vector3(x=dx_mm / 1000.0, y=0.0, z=0.0)))
    return asm.id


def _check(asm_id):
    env = cad_operations.execute("check_interference", asm_id, {}, include_state=False)
    assert env["ok"], env.get("error")
    return env["result"]


@pytest.mark.parametrize("other", ["modelled", "imported"])
def test_a_face_to_face_pair_with_an_import_is_checked_and_not_a_clash(other):
    """40 mm apart is touching: the coincident faces a fasten mate produces."""
    first = _imported_box()
    second = _modelled_box() if other == "modelled" else _imported_box("Second block")
    result = _check(_assembly_of(first, second, 40.0))
    assert result["components_checked"] == 2 == result["components_total"]
    assert result["skipped"] == []
    assert result["interferences"] == 0, result["pairs"]
    assert any(a["component"] == "Imported block" for a in result["approximated"])


@pytest.mark.parametrize("other", ["modelled", "imported"])
def test_an_import_overlapping_by_two_millimetres_is_a_clash(other):
    """The other half of the calibration: an over-eroded field would pass the
    touching case above by missing this one."""
    first = _imported_box()
    second = _modelled_box() if other == "modelled" else _imported_box("Second block")
    result = _check(_assembly_of(first, second, 38.0))
    assert result["interferences"] == 1, result["pairs"]
    pair = result["pairs"][0]
    assert pair["penetration_mm"] == pytest.approx(2.0, abs=0.5)
    assert pair["tolerance_mm"] > 0
    # Both bodies are exact, so how much material is in the way is still an
    # exact boolean, whatever the sampled field was built from.
    assert pair["overlap_mm3"] == pytest.approx(2 * 30 * 20, rel=0.02)


def test_an_unreadable_component_is_named_and_the_assembly_is_not_clear(monkeypatch):
    first, second = _modelled_box(), _imported_box()
    asm_id = _assembly_of(first, second, 60.0)
    real = interference._Placed

    def refuse_the_import(comp, *args, **kwargs):
        if comp.part_id == second.id:
            raise RuntimeError("no geometry")
        return real(comp, *args, **kwargs)

    monkeypatch.setattr(interference, "_Placed", refuse_the_import)
    result = _check(asm_id)
    assert result["components_checked"] == 1 and result["components_total"] == 2
    assert result["skipped"][0]["component"] == "Imported block"
    assert "no geometry" in result["skipped"][0]["reason"]
    assert result["interferences"] == 0 and result["clear"] is False

    # And the copilot's done gate reads it as unverified, not as a pass.
    failures = copilot._verify("assembly", asm_id, lambda _: None)
    assert failures and "could not be checked" in failures[0]
    assert "Imported block" in failures[0]
