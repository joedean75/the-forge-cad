"""Structural validation of generated geometry.

There was no manifoldness, watertightness, winding or connectivity check
anywhere in the codebase — the sole validity gate was mass_properties refusing a
zero-volume mesh. An agent could therefore "successfully" build a body that was
not a solid, and a reward function had nothing to score against.
"""

import numpy as np
import pytest

from app.cad_kernel.core.topology import Face, FaceMesh, Shell, Solid
from app.services import cad_operations as ops
from app.services import document_service as docs
from app.services import geometry_check


def _doc(name="check"):
    return docs.create_document(name=name, resolution=32, owner_id=None)


def _status(report, check_id):
    return next(c["status"] for c in report["checks"] if c["id"] == check_id)


# --------------------------------------------------------------- happy path --

def test_a_plain_box_is_a_valid_solid():
    doc = _doc()
    ops.dispatch("add_primitive", doc.id, {"shape": "box"})
    report = geometry_check.check_document(doc.id, resolution=32)
    assert report["valid"] is True
    assert report["failed"] == []
    assert _status(report, "watertight") == "pass"
    assert _status(report, "connectivity") == "pass"


def test_a_subtracted_body_is_still_a_valid_solid():
    doc = _doc()
    ops.dispatch("add_primitive", doc.id, {"shape": "box",
                                           "params": {"size": {"x": .06, "y": .06, "z": .06}}})
    ops.dispatch("add_primitive", doc.id, {"shape": "cylinder", "operation": "subtract",
                                           "params": {"radius": .01, "height": .2}})
    assert geometry_check.check_document(doc.id, resolution=48)["valid"] is True


# ------------------------------------------------------------ degenerate input --

def _solid_from(vertices, faces):
    mesh = FaceMesh(np.asarray(vertices, dtype=float), np.asarray(faces, dtype=int))
    return Solid(Shell([Face(surface=None, mesh=mesh)]))


def test_an_empty_body_fails_and_stops_there():
    """The remaining checks are meaningless on an empty mesh, and trimesh calls it watertight."""
    checks = geometry_check.check_solid(_solid_from(np.zeros((0, 3)), np.zeros((0, 3))))
    assert [c.id for c in checks] == ["non_empty"]
    assert checks[0].status == "fail"


def test_an_open_surface_is_caught_as_not_watertight():
    """A single triangle encloses nothing — exactly the shape a thin GridSDF shell takes."""
    checks = geometry_check.check_solid(
        _solid_from([[0, 0, 0], [1, 0, 0], [0, 1, 0]], [[0, 1, 2]]))
    by_id = {c.id: c for c in checks}
    assert by_id["watertight"].status == "fail"
    assert by_id["volume"].status == "fail"


def test_two_separate_boxes_are_reported_as_disconnected():
    doc = _doc()
    ops.dispatch("add_primitive", doc.id, {"shape": "box",
                                           "params": {"size": {"x": .02, "y": .02, "z": .02},
                                                      "center": {"x": -.05, "y": 0, "z": 0}}})
    ops.dispatch("add_primitive", doc.id, {"shape": "box",
                                           "params": {"size": {"x": .02, "y": .02, "z": .02},
                                                      "center": {"x": .05, "y": 0, "z": 0}}})
    report = geometry_check.check_document(doc.id, resolution=48)
    assert _status(report, "connectivity") == "warn"
    # Disconnected is suspicious, not invalid — a two-piece body is legal geometry.
    assert report["valid"] is True


def test_an_unevaluatable_document_reports_a_failed_check_rather_than_raising():
    doc = _doc()
    box = ops.dispatch("add_primitive", doc.id, {"shape": "box"})["feature_id"]
    ops.dispatch("set_feature_params", doc.id,
                 {"feature_id": box, "params": {"size": {"x": "=undefined_param", "y": .02, "z": .02}}})
    report = geometry_check.check_document(doc.id, resolution=32)
    assert report["valid"] is False
    assert _status(report, "evaluates") == "fail"


# ------------------------------------------------- reachable from the catalog --

def test_check_geometry_is_in_the_operation_catalog():
    """The verify half of a verify-and-iterate loop has to be callable as an op."""
    assert "check_geometry" in ops.OPERATIONS
    assert ops.OPERATIONS["check_geometry"].kind == "query"


def test_dfm_is_now_callable_as_an_operation():
    doc = _doc()
    ops.dispatch("add_primitive", doc.id, {"shape": "box"})
    env = ops.execute("dfm", doc.id, {"resolution": 32})
    assert env["ok"] is True
    assert "printability_score" in env["result"]
    ids = {c["id"] for c in env["result"]["checks"]}
    assert {"overhang", "support"} <= ids
    # On the exact kernel the mesh-edge `feature_size` proxy is replaced by a
    # real wall thickness; without it, the proxy is all there is.
    assert ("wall_thickness" in ids) != ("feature_size" in ids)


def test_check_geometry_through_execute_returns_a_scoreable_shape():
    doc = _doc()
    ops.dispatch("add_primitive", doc.id, {"shape": "sphere"})
    env = ops.execute("check_geometry", doc.id, {"resolution": 32})
    assert env["ok"] is True
    assert env["result"]["valid"] is True
    assert isinstance(env["result"]["failed"], list)


def test_slice_part_is_a_mutation_because_it_writes_to_the_document():
    """As a query it skipped the editor check and the quota meter."""
    assert ops.OPERATIONS["slice_part"].kind == "mutate"
