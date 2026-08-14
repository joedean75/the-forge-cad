"""Machined holes: counterbore, countersink, drill point, through, tapped.

`HoleFeature` was a bore — one cylinder, one radius, one depth — and
`clearance_hole` computed the correct ISO 273 diameter and then drilled exactly
that. Anything a real drawing calls for was built by hand as a second primitive
stacked on the first, which meant two rows in `list_features` for one hole and
a counterbore left behind when the bore was resized.

The tests that matter most here are the ones about *not* changing meaning. A
hole stored before any of this existed must cut the identical body, `depth`
must go on measuring to the shoulder, and both kernels have to agree — the
geometry is enumerated once in `geom/holes.py` precisely so they cannot drift.
"""

import math

import pytest

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.evaluator import brep
from app.cad_kernel.geom import holes
from app.cad_kernel.geom.meshing import mesh_volume, solid_to_mesh
from app.cad_kernel.kernel import Kernel
from app.services import cad_operations as ops
from app.services import document_service as docs

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731

PLATE = {"type": "BoxFeature", "id": "b1",
         "params": {"size": V(.08, .06, .02), "center": V(0, 0, 0)}}


def _hole_tree(**params) -> FeatureTree:
    merged = {"radius": .005, "depth": .2, "center": V(0, 0, 0), "axis": V(0, 0, 1),
              "children": [PLATE]}
    merged.update(params)
    return FeatureTree.from_dict(
        {"version": "hole-v1", "root": {"type": "HoleFeature", "id": "h1", "params": merged}})


def _sdf_volume(tree: FeatureTree, resolution: int = 180) -> float:
    mesh = solid_to_mesh(Kernel.evaluate_tree(tree, resolution=resolution))
    return abs(mesh_volume(mesh.vertices, mesh.faces))


# ------------------------------------------------------ the tool geometry --

def test_a_default_hole_is_still_exactly_one_centred_cylinder():
    """The compatibility guarantee. Every stored `HoleFeature` deserialises with
    these defaults, and if this list ever grows a second entry, every document
    in the datastore quietly cuts a different body."""
    tools = holes.hole_tools(radius=.005, depth=.02, center=(0, 0, 0), axis=(0, 0, 1))
    assert len(tools) == 1
    assert isinstance(tools[0], holes.Cyl)
    assert tools[0] == holes.Cyl(.005, .02, (0.0, 0.0, 0.0), (0.0, 0.0, 1.0))


def test_the_counterbore_sits_at_the_mouth_not_the_middle():
    """A cylinder in this kernel is centred on its origin, so a recess placed at
    the hole's centre would be half above the material and half buried. The
    mouth is `center + axis * depth/2`, and the recess hangs below it."""
    tools = holes.hole_tools(radius=.003, depth=.02, center=(0, 0, 0), axis=(0, 0, 1),
                             hole_type="counterbore", recess_radius=.006, recess_depth=.004)
    bore, cbore = tools
    assert cbore.radius == pytest.approx(.006)
    # Top flush with the mouth at z = +0.01, extending down 4 mm.
    assert cbore.center[2] + cbore.height / 2 == pytest.approx(.01)
    assert bore.height == pytest.approx(.02)


def test_a_countersink_is_cut_with_the_whole_cone():
    """Below the point where the cone narrows past the bore, the bore has
    already removed the material — so the full cone and the frustum are the
    same subtraction, and the full cone is a shape both kernels already build."""
    tools = holes.hole_tools(radius=.002, depth=.02, center=(0, 0, 0), axis=(0, 0, 1),
                             hole_type="countersink", recess_radius=.005,
                             countersink_angle=90.0)
    cone = tools[1]
    assert isinstance(cone, holes.Cone)
    # 90 degrees included: the apex is one radius below the mouth.
    assert cone.height == pytest.approx(.005)
    assert cone.axis == (0.0, 0.0, -1.0)          # apex pointing into the body
    assert cone.center[2] == pytest.approx(.01 - .0025)


def test_a_stated_countersink_depth_overrides_the_angle():
    """'8 dia x 3 deep' is how a drawing says it, and the angle then follows."""
    tools = holes.hole_tools(radius=.002, depth=.02, center=(0, 0, 0), axis=(0, 0, 1),
                             hole_type="countersink", recess_radius=.004, recess_depth=.003)
    cone = tools[1]
    # The cone reaches the bore radius at 3 mm, so the apex is proportionally on.
    assert cone.height == pytest.approx(.003 * .004 / (.004 - .002))


def test_depth_measures_to_the_shoulder_so_a_tip_is_added_below_it():
    """If `depth` meant "to the point", adding `tip_angle` to a stored feature
    would move the flat bottom of every blind hole in the datastore upward."""
    plain = holes.hole_tools(radius=.003, depth=.01, center=(0, 0, 0), axis=(0, 0, 1))
    tipped = holes.hole_tools(radius=.003, depth=.01, center=(0, 0, 0), axis=(0, 0, 1),
                              tip_angle=118.0)
    assert tipped[0] == plain[0]                  # the bore is untouched
    tip = tipped[1]
    assert tip.base_radius == pytest.approx(.003)
    # 118 degrees included -> half-angle 59 -> h = r / tan(59 deg).
    assert tip.height == pytest.approx(.003 / math.tan(math.radians(59)))
    # Base flush with the shoulder at z = -0.005, apex further down.
    assert tip.center[2] + tip.height / 2 == pytest.approx(-.005)


def test_through_lengthens_the_far_end_and_leaves_the_mouth_alone():
    """Extending both ends would carry the recess out of the material it is
    supposed to sit in."""
    tools = holes.hole_tools(radius=.003, depth=.02, center=(0, 0, 0), axis=(0, 0, 1),
                             through=True)
    bore = tools[0]
    assert bore.height == pytest.approx(.02 + holes.THROUGH_REACH)
    assert bore.center[2] + bore.height / 2 == pytest.approx(.01)   # mouth unmoved


def test_a_through_hole_gets_no_drill_point():
    """There is no shoulder to put one below — it broke out of the far side."""
    tools = holes.hole_tools(radius=.003, depth=.02, center=(0, 0, 0), axis=(0, 0, 1),
                             through=True, tip_angle=118.0)
    assert len(tools) == 1


def test_a_recess_narrower_than_the_bore_is_ignored_rather_than_inverted():
    """It would be a cone that widens downward, which is not a countersink."""
    assert len(holes.hole_tools(radius=.005, depth=.02, center=(0, 0, 0), axis=(0, 0, 1),
                                hole_type="countersink", recess_radius=.003)) == 1


def test_tap_drill_is_the_thread_minus_its_pitch():
    """ISO 2306's rule, and what every tap chart lists: an M6x1 is drilled 5 mm."""
    assert holes.tap_drill_radius("M6") == pytest.approx(.0025)
    assert holes.tap_drill_radius("M4") == pytest.approx(.00165)
    with pytest.raises(KeyError):
        holes.tap_drill_radius("M99")


# ------------------------------------------------------- sampled geometry --

def test_a_counterbore_removes_more_material_than_the_bore_alone():
    plain = _sdf_volume(_hole_tree(radius=.003, depth=.2))
    bored = _sdf_volume(_hole_tree(radius=.003, depth=.2, hole_type="counterbore",
                                   recess_radius=.006, recess_depth=.004,
                                   center=V(0, 0, -.09)))
    assert bored < plain


def test_a_blind_holes_volume_matches_the_closed_form():
    """A 6 mm bore 8 mm deep into a 20 mm plate, flat-bottomed."""
    tree = _hole_tree(radius=.003, depth=.008, center=V(0, 0, .006))
    plate = .08 * .06 * .02
    bore = math.pi * .003 ** 2 * .008
    assert _sdf_volume(tree, resolution=220) == pytest.approx(plate - bore, rel=.02)


# ---------------------------------------------- the two kernels must agree --

@pytest.mark.skipif(not brep.AVAILABLE, reason="OCP (OpenCascade) not installed")
@pytest.mark.parametrize("params, ident", [
    ({"radius": .004, "depth": .2}, "simple-through"),
    ({"radius": .003, "depth": .008, "center": V(0, 0, .006)}, "blind"),
    ({"radius": .003, "depth": .008, "center": V(0, 0, .006), "tip_angle": 118.0}, "drill-point"),
    ({"radius": .003, "depth": .02, "center": V(0, 0, 0), "through": True,
      "hole_type": "counterbore", "recess_radius": .006, "recess_depth": .005}, "counterbore"),
    ({"radius": .003, "depth": .02, "center": V(0, 0, 0), "through": True,
      "hole_type": "countersink", "recess_radius": .006}, "countersink"),
    ({"radius": .003, "depth": .06, "center": V(.01, 0, 0), "axis": V(1, 0, 0),
      "hole_type": "counterbore", "recess_radius": .005, "recess_depth": .004}, "along-x"),
])
def test_both_kernels_build_the_same_hole(params, ident):
    """The whole reason `geom/holes.py` exists. Two transcriptions of this
    arithmetic would agree the day they were written."""
    tree = _hole_tree(**params)
    shape = brep.brep_from_feature(tree.root)
    assert shape is not None, "expected an exact form"
    assert brep.is_valid(shape)
    assert brep.volume(shape) == pytest.approx(_sdf_volume(tree, resolution=200), rel=.02)


# ------------------------------------------------------------ the operation --

def _plate_doc(account_id="dev"):
    doc = docs.create_document(name="hole test", owner_id=account_id)
    ops.dispatch("add_primitive", doc.id,
                 {"shape": "box", "params": {"size": V(.08, .06, .02), "center": V(0, 0, 0)}})
    return doc.id


def test_add_hole_places_the_mouth_on_the_body_face():
    """Without this a counterbore lands in mid-air: the default centre was the
    origin, so on a plate centred at z=0 the mouth sat 100 mm above it."""
    doc_id = _plate_doc()
    result = ops.dispatch("add_hole", doc_id, {"radius": .003, "hole_type": "counterbore",
                                               "recess_radius": .006, "recess_depth": .004})
    assert result["hole_type"] == "counterbore"
    node = next(n for n in ops.feature_ops.flatten(docs.get_document(doc_id).feature_tree)
                if n["type"] == "HoleFeature")
    p = node["params"]
    # Mouth = center.z + depth/2, and the plate's top face is at +10 mm.
    assert p["center"]["z"] + p["depth"] / 2 == pytest.approx(.01)


def test_add_hole_is_one_feature_not_two():
    doc_id = _plate_doc()
    ops.dispatch("add_hole", doc_id, {"radius": .003, "hole_type": "countersink",
                                      "recess_radius": .006})
    holes_in_tree = [n for n in ops.feature_ops.flatten(docs.get_document(doc_id).feature_tree)
                     if n["type"] == "HoleFeature"]
    assert len(holes_in_tree) == 1


def test_tapped_drills_the_tap_drill_size_and_records_the_callout():
    doc_id = _plate_doc()
    result = ops.dispatch("add_hole", doc_id, {"tapped": "M6"})
    assert result["diameter_mm"] == pytest.approx(5.0)
    assert result["thread"] == "M6"


def test_radius_and_tapped_together_are_refused():
    """A tapped hole's bore is the tap-drill size for the thread. Accepting both
    means one of them is silently ignored, which is the failure mode the whole
    envelope exists to prevent."""
    doc_id = _plate_doc()
    envelope = ops.execute("add_hole", doc_id, {"tapped": "M6", "radius": .004})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "BAD_ARG"


def test_a_counterbore_without_a_depth_is_refused_rather_than_ignored():
    doc_id = _plate_doc()
    envelope = ops.execute("add_hole", doc_id, {"radius": .003, "hole_type": "counterbore",
                                                "recess_radius": .006})
    assert envelope["ok"] is False
    assert "recess_depth" in envelope["error"]["message"]


def test_a_recess_no_wider_than_the_bore_is_refused_by_name():
    doc_id = _plate_doc()
    envelope = ops.execute("add_hole", doc_id, {"radius": .005, "hole_type": "countersink",
                                                "recess_radius": .004})
    assert envelope["ok"] is False
    assert envelope["error"]["arg"] == "recess_radius"


def test_drilling_a_document_with_no_body_is_a_precondition():
    doc = docs.create_document(name="empty", owner_id="dev")
    envelope = ops.execute("add_hole", doc.id, {"radius": .003})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "PRECONDITION"


def test_clearance_hole_is_now_one_feature_with_the_published_head_depth():
    """It used to stack a second cylinder and halve its own table value to
    compensate for a centred primitive, dividing the ISO 4762 head height by
    500 instead of 1000."""
    from app.services import parts_catalog

    doc_id = _plate_doc()
    ops.dispatch("clearance_hole", doc_id, {"screw": "M4", "counterbore": True})
    nodes = [n for n in ops.feature_ops.flatten(docs.get_document(doc_id).feature_tree)
             if n["type"] in ("HoleFeature", "CylinderFeature")]
    assert len(nodes) == 1 and nodes[0]["type"] == "HoleFeature"
    expected = parts_catalog.clearance_hole("M4")["counterbore_depth_mm"] / 1000.0
    assert nodes[0]["params"]["recess_depth"] == pytest.approx(expected)


# --------------------------------------------------------- the round trip --

def test_the_decompiler_recovers_every_hole_parameter():
    """A key it drops is a counterbore that vanishes on replay, and the mesh
    comparison reports that as `geometry_mismatch` without saying which."""
    from app.services import decompiler

    doc_id = _plate_doc()
    ops.dispatch("add_hole", doc_id, {"radius": .003, "hole_type": "counterbore",
                                      "recess_radius": .006, "recess_depth": .004,
                                      "tip_angle": 118.0})
    recovered = decompiler.decompile_document(doc_id, record=False)
    assert recovered["ok"], recovered.get("reason")
    hole = next(step for step in recovered["steps"]
                if step["args"].get("modifier") == "hole")
    params = hole["args"]["params"]
    assert params["hole_type"] == "counterbore"
    assert params["recess_radius"] == pytest.approx(.006)
    assert params["tip_angle"] == pytest.approx(118.0)
