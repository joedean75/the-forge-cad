"""DFM measured on the body rather than on the mesh that renders it.

The reason this module exists is one number. `feature_size` reported the
shortest edge of a marching-cubes mesh and called it the minimum feature — a
property of `resolution`, not of the part. These tests pin the replacement to
values that can be worked out on paper: a 0.4 mm tube wall reads 0.4 mm, a
frustum that tapers 2 mm over 30 mm reads 3.81° of draft.
"""

import math

import pytest

from app.cad_kernel.evaluator import brep
from app.services import cad_operations as ops
from app.services import dfm_topology as dt
from app.services import document_service as docs

pytestmark = pytest.mark.skipif(
    not dt.available(), reason="topology DFM needs cadquery-ocp")


def _pnt(x, y, z):
    from OCP.gp import gp_Pnt

    return gp_Pnt(x, y, z)


def _ax2(origin, direction=(0, 0, 1)):
    from OCP.gp import gp_Ax2, gp_Dir

    return gp_Ax2(_pnt(*origin), gp_Dir(*direction))


def _cut(a, b):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut

    return BRepAlgoAPI_Cut(a, b).Shape()


def _fuse(a, b):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse

    return BRepAlgoAPI_Fuse(a, b).Shape()


def _box(dx, dy, dz, origin=(0, 0, 0)):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    return BRepPrimAPI_MakeBox(_pnt(*origin), dx, dy, dz).Shape()


def _cylinder(radius, height, origin=(0, 0, 0), direction=(0, 0, 1)):
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder

    return BRepPrimAPI_MakeCylinder(_ax2(origin, direction), radius, height).Shape()


def _plate_with_bore():
    """80 x 40 x 10 mm, one Ø10 mm through-bore."""
    return _cut(_box(0.08, 0.04, 0.01),
                _cylinder(0.005, 0.02, (0.04, 0.02, -0.001)))


def _row(rows, check_id):
    return next(r for r in rows if r.id == check_id)


def test_wall_thickness_is_the_thickness_of_the_wall():
    """A solid plate is 10 mm thick, whatever its mesh looks like."""
    rows = dt.checks(_plate_with_bore(), "milling")

    wall = _row(rows, "wall_thickness")
    assert wall.value == pytest.approx(10.0, abs=0.01)
    assert wall.status == "pass"


def test_a_thin_wall_is_measured_not_estimated():
    tube = _cut(_cylinder(0.010, 0.030), _cylinder(0.0096, 0.05, (0, 0, -0.001)))

    wall = _row(dt.checks(tube, "milling"), "wall_thickness")

    assert wall.value == pytest.approx(0.4, abs=0.02)   # 10.0 − 9.6 mm
    assert wall.status == "fail"                        # under the 0.8 mm limit


@pytest.mark.parametrize("name,build,expected_mm", [
    ("L bracket", lambda: _fuse(_box(0.06, 0.04, 0.006), _box(0.006, 0.04, 0.05)), 6.0),
    ("thin rib on a plate",
     lambda: _fuse(_box(0.06, 0.04, 0.004), _box(0.0012, 0.04, 0.02, (0.028, 0, 0.004))), 1.2),
    ("blind pocket floor",
     lambda: _cut(_box(0.04, 0.04, 0.01), _box(0.02, 0.02, 0.01, (0.01, 0.01, 0.008))), 8.0),
    ("narrow slot",
     lambda: _cut(_box(0.04, 0.04, 0.01), _box(0.002, 0.042, 0.012, (0.019, -0.001, -0.001))), 10.0),
])
def test_thickness_on_shapes_where_a_corner_could_fool_it(name, build, expected_mm):
    """Concave corners, ribs and slots all put a second surface close to a
    sample. Because samples sit *on* the surface with the *true* normal, the
    inward ray still crosses the wall rather than clipping the neighbour."""
    wall = _row(dt.checks(build(), "milling"), "wall_thickness")

    assert wall.value == pytest.approx(expected_mm, abs=0.02)


def test_the_measurement_does_not_move_with_mesh_density():
    """The whole point: a resolution knob must not change the answer."""
    shape = _plate_with_bore()
    coarse = _row(dt.checks(shape, "milling", deflection=0.002), "wall_thickness")
    fine = _row(dt.checks(shape, "milling", deflection=0.0002), "wall_thickness")

    assert coarse.value == pytest.approx(fine.value, abs=0.01)


def test_draft_matches_the_taper_that_was_modelled():
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCone

    # r 20 -> 18 mm over 30 mm of height: atan(2/30) = 3.814 degrees.
    frustum = BRepPrimAPI_MakeCone(_ax2((0, 0, 0)), 0.020, 0.018, 0.030).Shape()
    expected = math.degrees(math.atan2(0.002, 0.030))

    draft = _row(dt.checks(frustum, "molding"), "draft")

    assert draft.value == pytest.approx(expected, abs=0.05)
    assert draft.status == "pass"


def test_a_vertical_wall_has_no_draft_and_says_so():
    draft = _row(dt.checks(_box(0.04, 0.04, 0.04), "molding"), "draft")

    assert draft.value == pytest.approx(0.0, abs=1e-6)
    assert draft.status == "fail"


def test_a_side_pocket_is_an_undercut_for_a_z_pull():
    part = _cut(_box(0.04, 0.04, 0.04),
                _box(0.02, 0.02, 0.01, (-0.001, 0.01, 0.01)))

    undercut = _row(dt.checks(part, "molding", "+Z"), "undercut")

    assert undercut.status == "fail"
    assert undercut.value > 0.0
    assert "side action" in undercut.detail


def test_a_plain_prism_releases_from_a_two_part_mould():
    undercut = _row(dt.checks(_box(0.04, 0.04, 0.04), "molding", "+Z"), "undercut")

    assert undercut.status == "pass"
    assert undercut.value == 0.0


def test_a_through_bore_is_reachable_by_a_three_axis_cutter():
    """An end mill cuts with its flank, so a bore wall parallel to the tool axis
    is machined by a cutter travelling down the bore. Requiring the wall to face
    the tool called every drilled hole unreachable."""
    access = _row(dt.checks(_plate_with_bore(), "milling"), "tool_access")

    assert access.status == "pass"
    assert access.value == 0.0


def test_a_sealed_cavity_is_both_a_trapped_void_and_unreachable():
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeSphere

    block = _box(0.04, 0.04, 0.04)
    part = _cut(block, BRepPrimAPI_MakeSphere(_pnt(0.02, 0.02, 0.02), 0.01).Shape())

    rows = dt.checks(part, "milling")

    assert _row(rows, "trapped_void").status == "fail"
    assert _row(rows, "trapped_void").value == 1.0
    assert _row(rows, "tool_access").status == "warn"


def test_a_degenerate_pole_edge_is_not_the_smallest_feature():
    """A sphere carries zero-length edges at its poles. They are bookkeeping."""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeSphere

    rows = dt.checks(BRepPrimAPI_MakeSphere(_pnt(0, 0, 0), 0.02).Shape(), "milling")

    assert _row(rows, "min_edge").value > 0.1


def test_a_bore_below_the_process_limit_fails():
    part = _cut(_box(0.08, 0.04, 0.01),
                _cylinder(0.0002, 0.02, (0.04, 0.02, -0.001)))

    bore = _row(dt.checks(part, "milling"), "min_bore")

    assert bore.status == "fail"
    assert bore.value == pytest.approx(0.4, abs=1e-3)


def test_an_unknown_process_is_refused():
    with pytest.raises(ValueError):
        dt.checks(_box(0.01, 0.01, 0.01), "sintering")


# ── through the operation catalog ──

def _doc(*primitives):
    doc = docs.create_document(name="Part", resolution=48, owner_id=None)
    for args in primitives:
        ops.dispatch("add_primitive", doc.id, args)
    return doc.id


def test_dfm_reports_per_process_through_the_operation():
    doc_id = _doc(
        {"shape": "box", "params": {"size": {"x": .08, "y": .04, "z": .01}}},
        {"shape": "cylinder", "operation": "subtract",
         "params": {"radius": .005, "height": .05}},
    )

    for process in ("additive", "milling", "molding", "casting"):
        env = ops.execute("dfm", doc_id, {"process": process, "resolution": 48})
        assert env["ok"], env.get("error")
        ids = {c["id"] for c in env["result"]["checks"]}
        assert {"wall_thickness", "min_bore", "trapped_void"} <= ids

    milling = ops.dispatch("dfm", doc_id, {"process": "milling"})
    molding = ops.dispatch("dfm", doc_id, {"process": "molding"})
    assert "tool_access" in {c["id"] for c in milling["checks"]}
    assert {"draft", "undercut"} <= {c["id"] for c in molding["checks"]}


def test_the_additive_report_keeps_its_own_checks_and_drops_the_mesh_proxy():
    doc_id = _doc({"shape": "box", "params": {"size": {"x": .05, "y": .05, "z": .05}}})

    report = ops.dispatch("dfm", doc_id, {"resolution": 48})

    ids = {c["id"] for c in report["checks"]}
    assert {"overhang", "support", "wall_thickness"} <= ids
    assert "feature_size" not in ids     # replaced, not duplicated
    assert report["quote"] is not None   # additive still quotes


def test_a_non_additive_report_offers_no_quote_it_cannot_compute():
    doc_id = _doc({"shape": "box", "params": {"size": {"x": .05, "y": .05, "z": .05}}})

    report = ops.dispatch("dfm", doc_id, {"process": "milling"})

    assert report["quote"] is None
    assert report["print_time_minutes"] is None


def test_a_body_with_no_exact_form_can_still_be_checked_for_additive():
    """A shelled part has no B-rep, so the topology rows are absent — but the
    overhang and support checks it can answer still run."""
    doc_id = _doc({"shape": "box", "params": {"size": {"x": .05, "y": .05, "z": .05}}})
    ops.dispatch("add_modifier", doc_id, {"modifier": "shell", "params": {"thickness": .004}})

    report = ops.dispatch("dfm", doc_id, {"resolution": 48})

    ids = {c["id"] for c in report["checks"]}
    assert {"overhang", "support", "feature_size"} <= ids
    assert "wall_thickness" not in ids


def test_a_non_additive_report_on_such_a_body_says_why_it_cannot():
    doc_id = _doc({"shape": "box", "params": {"size": {"x": .05, "y": .05, "z": .05}}})
    ops.dispatch("add_modifier", doc_id, {"modifier": "shell", "params": {"thickness": .004}})

    env = ops.execute("dfm", doc_id, {"process": "milling"})

    assert env["ok"] is False
    assert env["error"]["code"] == "PRECONDITION"
    assert "exact kernel" in env["error"]["message"]


def test_an_unknown_process_is_refused_by_the_schema():
    doc_id = _doc({"shape": "box"})

    env = ops.execute("dfm", doc_id, {"process": "sintering"})

    assert env["ok"] is False
    assert env["error"]["code"] == "BAD_ARG"


def test_exactness_means_the_wall_reading_survives_a_resolution_change():
    doc_id = _doc({"shape": "box", "params": {"size": {"x": .05, "y": .05, "z": .004}}})

    coarse = ops.dispatch("dfm", doc_id, {"resolution": 32})
    fine = ops.dispatch("dfm", doc_id, {"resolution": 128})

    def wall(report):
        return next(c["value"] for c in report["checks"] if c["id"] == "wall_thickness")

    assert wall(coarse) == pytest.approx(wall(fine), abs=1e-6)
    assert wall(coarse) == pytest.approx(4.0, abs=0.01)
