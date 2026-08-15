"""Draft, ribs, and the variable fillet.

Three gaps that were absent for no better reason than that nothing forced them:
OpenCascade has `BRepOffsetAPI_DraftAngle`, a two-radius `BRepFilletAPI` `Add`,
and everything a prism needs. `services/dfm_topology` has been *measuring*
draft — reporting faces that will not release from a tool — with no feature able
to add any.

The load-bearing tests here are the ones about what did *not* change. A stored
constant-radius fillet must keep taking OCC's one-argument `Add`, because the
radius law builds a genuinely different surface even where both radii are equal;
and a rib must mean the same thing on both kernels, which is why its corner
points are computed once in `geom/ribs.py`.
"""

import math

import pytest

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.evaluator import brep
from app.cad_kernel.geom import ribs
from app.cad_kernel.geom.meshing import mesh_volume, solid_to_mesh
from app.cad_kernel.kernel import Kernel
from app.services import cad_operations as ops
from app.services import document_service as docs

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731

exact_only = pytest.mark.skipif(not brep.AVAILABLE, reason="OCP (OpenCascade) not installed")


def _box_doc(size=(.06, .04, .03), centre=(0, 0, 0)):
    doc = docs.create_document(name="tier1-s2", owner_id="dev")
    ops.dispatch("add_primitive", doc.id,
                 {"shape": "box", "params": {"size": V(*size), "center": V(*centre)}})
    return doc.id


def _mm3(doc_id) -> float:
    return ops.dispatch("mass_properties", doc_id, {})["volume_cm3"] * 1000.0


# ==================================================================== draft ==

@exact_only
def test_draft_matches_the_frustum_formula():
    """A 60x40x30 box with all four sides drafted 3 degrees is a rectangular
    frustum, and that has a closed form. Getting this right is what makes the
    number `dfm` reports and the number the feature applies the same number."""
    doc_id = _box_doc()
    assert _mm3(doc_id) == pytest.approx(72000.0)

    sides = [f["id"] for f in ops.dispatch("list_faces", doc_id, {})["faces"]
             if abs(f["normal"][2]) < 0.5]
    assert len(sides) == 4
    ops.dispatch("add_draft", doc_id, {"faces": sides, "angle": 3.0})

    taper = 30.0 * math.tan(math.radians(3.0))
    a1, b1 = 60.0, 40.0
    a2, b2 = a1 - 2 * taper, b1 - 2 * taper
    frustum = 30.0 / 6.0 * (a1 * b1 + a2 * b2 + (a1 + a2) * (b1 + b2))
    assert _mm3(doc_id) == pytest.approx(frustum, rel=1e-5)


@exact_only
def test_draft_pivots_about_the_parting_line_so_the_base_keeps_its_size():
    """A neutral plane anywhere else resizes the part as well as tapering it,
    and the resize is the half nobody notices until the first inspection."""
    doc_id = _box_doc()
    sides = [f["id"] for f in ops.dispatch("list_faces", doc_id, {})["faces"]
             if abs(f["normal"][2]) < 0.5]
    ops.dispatch("add_draft", doc_id, {"faces": sides, "angle": 3.0})

    flats = sorted(f["area_mm2"] for f in ops.dispatch("list_faces", doc_id, {})["faces"]
                   if abs(f["normal"][2]) > 0.9)
    assert flats[-1] == pytest.approx(2400.0, rel=1e-4)     # base, unmoved
    assert flats[0] < 2200.0                                # top, drawn in


@exact_only
def test_a_draft_can_be_pulled_along_any_axis():
    doc_id = _box_doc()
    faces = [f["id"] for f in ops.dispatch("list_faces", doc_id, {})["faces"]
             if abs(f["normal"][0]) < 0.5]
    result = ops.execute("add_draft", doc_id,
                         {"faces": faces, "angle": 2.0, "pull": V(1, 0, 0)})
    assert result["ok"], result.get("error")
    assert _mm3(doc_id) < 72000.0


def test_a_draft_angle_outside_zero_to_ninety_is_refused_by_name():
    doc_id = _box_doc()
    envelope = ops.execute("add_draft", doc_id, {"faces": "all", "angle": 95.0})
    assert envelope["ok"] is False
    assert envelope["error"]["arg"] == "angle"


@exact_only
def test_an_impossible_draft_rolls_back_rather_than_leaving_a_broken_document():
    """`_apply_exact`'s contract: a taper that consumes the face it is applied
    to has no later step that rescues it, so the edit is undone and the failure
    names the cause."""
    doc_id = _box_doc(size=(.06, .04, .03))
    before = _mm3(doc_id)
    sides = [f["id"] for f in ops.dispatch("list_faces", doc_id, {})["faces"]
             if abs(f["normal"][2]) < 0.5]
    envelope = ops.execute("add_draft", doc_id, {"faces": sides, "angle": 89.0})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "PRECONDITION"
    assert _mm3(doc_id) == pytest.approx(before)


def test_draft_has_no_field_form_and_says_so():
    """Tapering a sampled field is a different solid, and `dfm_topology` would
    then measure the approximation and disagree with the exact kernel."""
    from app.cad_kernel.evaluator import sdf

    assert "DraftFeature" in brep.EXACT_ONLY
    node = FeatureTree.from_dict({"version": "v", "root": {
        "type": "DraftFeature", "id": "d", "params": {
            "angle": 3.0, "faces": [], "children": [
                {"type": "BoxFeature", "id": "b",
                 "params": {"size": V(.05, .05, .05), "center": V(0, 0, 0)}}]}}})
    with pytest.raises(Exception, match="(?i)exact"):
        sdf.sdf_from_feature(node.root)


# ====================================================================== rib ==

def test_a_ribs_base_opens_out_and_its_tip_holds_the_stated_thickness():
    """A mould tool is cut to the tip dimension; the draft is what lets the part
    come off it. Pinching the tip instead would make the rib un-mouldable and
    the stated thickness a lie."""
    prism = ribs.rib_prism(start=(0, 0, 0), end=(.05, 0, 0), up=(0, 0, 1),
                           thickness=.002, height=.01, draft=3.0)
    base_width = math.dist(prism.section[0], prism.section[1])
    tip_width = math.dist(prism.section[2], prism.section[3])
    assert tip_width == pytest.approx(.002)
    assert base_width == pytest.approx(.002 + 2 * .01 * math.tan(math.radians(3)))


def test_no_draft_makes_a_rib_a_plain_rectangular_prism():
    prism = ribs.rib_prism(start=(0, 0, 0), end=(.05, 0, 0), thickness=.003, height=.01)
    assert math.dist(prism.section[0], prism.section[1]) == pytest.approx(.003)
    assert math.dist(prism.section[2], prism.section[3]) == pytest.approx(.003)


def test_a_rib_that_grows_along_its_own_run_is_refused():
    """There is no cross-section to sweep, so the alternative is a degenerate
    solid that fails much later with nothing pointing at the cause."""
    with pytest.raises(ValueError, match="parallel"):
        ribs.rib_prism(start=(0, 0, 0), end=(.05, 0, 0), up=(1, 0, 0))
    with pytest.raises(ValueError, match="same point"):
        ribs.rib_prism(start=(0, 0, 0), end=(0, 0, 0))


def test_every_half_space_normal_points_out_of_the_prism():
    """The field form is the intersection of these, so one flipped normal is a
    rib with a slab missing — and it would only show at one draft angle."""
    import numpy as np

    prism = ribs.rib_prism(start=(0, 0, 0), end=(.05, 0, 0), up=(0, 0, 1),
                           thickness=.002, height=.01, draft=5.0)
    section = np.asarray(prism.section)
    inside = section.mean(axis=0) + np.asarray(prism.sweep) / 2.0
    for point, normal in ribs.half_spaces(prism):
        assert float(np.dot(inside - np.asarray(point), np.asarray(normal))) < 0


def test_a_ribs_volume_matches_the_trapezoid_it_is():
    """Standing clear of the body so the union adds exactly the rib."""
    tree = FeatureTree.from_dict({"version": "v", "root": {
        "type": "RibFeature", "id": "r", "params": {
            "start": V(-.02, 0, .005), "end": V(.02, 0, .005), "up": V(0, 0, 1),
            "thickness": .002, "height": .012, "draft": 3.0,
            "children": [{"type": "BoxFeature", "id": "b", "params": {
                "size": V(.08, .06, .01), "center": V(0, 0, 0)}}]}}})
    shape = brep.brep_from_feature(tree.root) if brep.AVAILABLE else None
    tip, height, run = .002, .012, .04
    base = tip + 2 * height * math.tan(math.radians(3))
    expected = .08 * .06 * .01 + (tip + base) / 2 * height * run
    if shape is not None:
        assert brep.volume(shape) == pytest.approx(expected, rel=1e-6)
    mesh = solid_to_mesh(Kernel.evaluate_tree(tree, resolution=200))
    assert abs(mesh_volume(mesh.vertices, mesh.faces)) == pytest.approx(expected, rel=.02)


@exact_only
@pytest.mark.parametrize("params, ident", [
    ({"start": V(-.03, 0, -.005), "end": V(.03, 0, -.005), "up": V(0, 0, 1),
      "thickness": .002, "height": .012, "draft": 3.0}, "drafted-x"),
    ({"start": V(0, -.02, 0), "end": V(0, .02, 0), "up": V(0, 0, 1),
      "thickness": .003, "height": .008, "draft": 0.0}, "square-y"),
    ({"start": V(-.02, -.02, 0), "end": V(.02, .02, 0), "up": V(0, 0, 1),
      "thickness": .0025, "height": .01, "draft": 5.0}, "diagonal"),
])
def test_both_kernels_build_the_same_rib(params, ident, monkeypatch):
    """The corner points come from one function precisely so this holds."""
    tree = FeatureTree.from_dict({"version": "v", "root": {
        "type": "RibFeature", "id": "r",
        "params": {**params, "children": [{"type": "BoxFeature", "id": "b", "params": {
            "size": V(.08, .06, .01), "center": V(0, 0, -.005)}}]}}})
    exact = brep.volume(brep.brep_from_feature(tree.root))

    # The SDF is only reached with the exact kernel forced off — `evaluate_feature`
    # tries B-rep first, and a benchmark that forgot this once compared B-rep
    # against itself and concluded the two agreed perfectly.
    monkeypatch.setattr(brep, "AVAILABLE", False)
    mesh = solid_to_mesh(Kernel.evaluate_tree(
        FeatureTree.from_dict(tree.to_dict()), resolution=220))
    assert abs(mesh_volume(mesh.vertices, mesh.faces)) == pytest.approx(exact, rel=.02)


def test_add_rib_refuses_a_document_with_no_body():
    doc = docs.create_document(name="empty", owner_id="dev")
    envelope = ops.execute("add_rib", doc.id, {"start": V(0, 0, 0), "end": V(.05, 0, 0)})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "PRECONDITION"


def test_add_rib_names_the_argument_when_up_is_parallel_to_the_run():
    """Caught at the operation rather than at evaluation: `derived.error` would
    report this as a document-wide warning with nothing naming the cause."""
    doc_id = _box_doc()
    envelope = ops.execute("add_rib", doc_id,
                           {"start": V(0, 0, 0), "end": V(.05, 0, 0), "up": V(1, 0, 0)})
    assert envelope["ok"] is False
    assert envelope["error"]["arg"] == "up"


def test_add_rib_adds_material():
    doc_id = _box_doc(size=(.08, .06, .01), centre=(0, 0, 0))
    before = _mm3(doc_id)
    result = ops.dispatch("add_rib", doc_id,
                          {"start": V(-.03, 0, .005), "end": V(.03, 0, .005),
                           "thickness": .002, "height": .012, "draft": 2.0})
    assert result["draft"] == 2.0
    assert _mm3(doc_id) > before


# ========================================================= variable fillet ==

@exact_only
def test_a_constant_fillet_still_takes_the_one_argument_add():
    """The compatibility guarantee, and it is not cosmetic: OCC's radius law
    builds a different surface from the constant form even when both radii are
    equal, so a stored fillet routed through it would move a body nobody
    edited. Pinned by mesh hash, which is what `derived.mesh_url` is keyed on."""
    doc_id = _box_doc()
    ops.dispatch("fillet_edges", doc_id, {"edges": "all", "radius": .003})
    constant = docs.get_document(doc_id).derived.mesh_hash

    other = _box_doc()
    ops.dispatch("fillet_edges", other, {"edges": "all", "radius": .003,
                                         "radius_end": 0.0})
    assert docs.get_document(other).derived.mesh_hash == constant


def _vertical_edge(doc_id) -> str:
    """One of the four corner edges — 30 mm on the default test box.

    Named by measurement rather than taken as `edges[0]`, because OCC's
    variable-radius solver declines the box's four longest edges (see
    `test_occs_variable_law_declines_some_edges_and_we_say_so`) and a test that
    silently depended on exploration order would be testing that instead.
    """
    return next(e["id"] for e in ops.dispatch("list_edges", doc_id, {})["edges"]
                if e["length_mm"] == pytest.approx(30.0))


@exact_only
def test_a_variable_fillet_removes_a_different_amount_from_a_constant_one():
    doc_id = _box_doc()
    edge = _vertical_edge(doc_id)

    constant = _box_doc()
    ops.dispatch("fillet_edges", constant, {"edges": [_vertical_edge(constant)],
                                            "radius": .004})

    result = ops.execute("fillet_edges", doc_id,
                         {"edges": [edge], "radius": .004, "radius_end": .001})
    assert result["ok"], result.get("error")
    assert result["result"]["variable"] is True
    assert _mm3(doc_id) > _mm3(constant)     # less material taken at the small end


@exact_only
def test_occs_variable_law_declines_some_edges_and_we_say_so():
    """A real limitation, reported as one. OCC refuses a variable fillet on the
    four longest edges of a plain box while filleting the other eight at the
    same radii — so the constant fillet's "your radius is too big" would be the
    wrong diagnosis. Falling back to a constant radius would return `ok` for a
    body nobody asked for, which is the one thing this action space must not do.
    """
    doc_id = _box_doc()
    longest = next(e["id"] for e in ops.dispatch("list_edges", doc_id, {})["edges"]
                   if e["length_mm"] == pytest.approx(60.0))
    envelope = ops.execute("fillet_edges", doc_id,
                           {"edges": [longest], "radius": .004, "radius_end": .001})
    assert envelope["ok"] is False
    assert "variable-radius solver" in envelope["error"]["message"]
    assert "radius_end" in envelope["error"]["message"]
    # And rolled back: the body is the box it was.
    assert _mm3(doc_id) == pytest.approx(72000.0)


@exact_only
def test_a_variable_fillet_that_will_not_build_names_both_radii():
    doc_id = _box_doc(size=(.02, .02, .02))
    envelope = ops.execute("fillet_edges", doc_id,
                           {"edges": "all", "radius": .05, "radius_end": .08})
    assert envelope["ok"] is False
    assert "variable" in envelope["error"]["message"]


def test_a_negative_end_radius_is_refused_by_name():
    doc_id = _box_doc()
    envelope = ops.execute("fillet_edges", doc_id,
                           {"edges": "all", "radius": .002, "radius_end": -.001})
    assert envelope["ok"] is False
    assert envelope["error"]["arg"] == "radius_end"


# =============================================================== round trip ==

def test_the_decompiler_recovers_a_rib():
    from app.services import decompiler

    doc_id = _box_doc(size=(.08, .06, .01))
    ops.dispatch("add_rib", doc_id, {"start": V(-.03, 0, .005), "end": V(.03, 0, .005),
                                     "thickness": .002, "height": .012, "draft": 2.0})
    recovered = decompiler.decompile_document(doc_id, record=False)
    assert recovered["ok"], recovered.get("reason")
    rib = next(s for s in recovered["steps"] if s["op"] == "add_rib")
    assert rib["args"]["draft"] == pytest.approx(2.0)
    assert rib["args"]["thickness"] == pytest.approx(.002)


@exact_only
def test_the_decompiler_keeps_a_variable_fillet_variable():
    """Dropping `radius_end` replays as a constant fillet, which the mesh
    comparison reports as `geometry_mismatch` without saying which key went."""
    from app.services import decompiler

    doc_id = _box_doc()
    ops.dispatch("fillet_edges", doc_id, {"edges": [_vertical_edge(doc_id)],
                                          "radius": .004, "radius_end": .001})
    recovered = decompiler.decompile_document(doc_id, record=False)
    assert recovered["ok"], recovered.get("reason")
    fillet = next(s for s in recovered["steps"] if s["op"] == "fillet_edges")
    assert fillet["args"]["radius_end"] == pytest.approx(.001)


@exact_only
def test_the_decompiler_recovers_a_draft():
    from app.services import decompiler

    doc_id = _box_doc()
    sides = [f["id"] for f in ops.dispatch("list_faces", doc_id, {})["faces"]
             if abs(f["normal"][2]) < 0.5]
    ops.dispatch("add_draft", doc_id, {"faces": sides, "angle": 3.0})
    recovered = decompiler.decompile_document(doc_id, record=False)
    assert recovered["ok"], recovered.get("reason")
    draft = next(s for s in recovered["steps"] if s["op"] == "add_draft")
    assert draft["args"]["angle"] == pytest.approx(3.0)
