"""Does the generated toolpath actually cut the right part?

This is the answer to "can we have CAM simulation", and the reason it looks
like this rather than like a physics engine: material removal is a geometry
problem — subtract the swept tool from the stock and compare — with no forces
in it. See `docs/CAM-SIMULATION-SOURCING.md` for why NVIDIA's Newton was
investigated and declined.

The tests that carry the weight are the ones about the verifier having *power*.
A checker that passes everything is worse than none, because it converts "we
did not look" into "we looked and it was fine".
"""

import numpy as np
import pytest

from app.cad_kernel.core.feature_tree import Feature, FeatureTree
from app.services import cad_operations as ops
from app.services import document_service as docs
from app.services.cam import verify
from app.services.cam.toolpath import Passes, ToolParams, generate_toolpaths, plan_passes

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731


def _plate(size=(.05, .04, .012), center=(0, 0, 0)) -> Feature:
    return FeatureTree.from_dict({"version": "v", "root": {
        "type": "BoxFeature", "id": "b",
        "params": {"size": V(*size), "center": V(*center)}}}).root


def _stepped() -> Feature:
    """A plate with a boss — something a 2.5-axis path has to step around."""
    return FeatureTree.from_dict({"version": "v", "root": {
        "type": "BooleanFeature", "id": "u", "params": {
            "operation": "union", "children": [
                {"type": "BoxFeature", "id": "b1", "params": {
                    "size": V(.05, .04, .008), "center": V(0, 0, 0)}},
                {"type": "BoxFeature", "id": "b2", "params": {
                    "size": V(.02, .02, .008), "center": V(0, 0, .008)}}]}}}).root


TOOL = ToolParams(diameter_mm=3.0, stepdown_mm=2.0, stepover_mm=1.2)


# ------------------------------------------- the planner is the one source --

def test_the_verifier_checks_the_passes_the_gcode_is_written_from():
    """Re-deriving the path here would be a second planner, and a verifier that
    checks a different toolpath from the one the shop receives would report a
    clean cut for a program that gouges."""
    import inspect

    assert "plan_passes" in inspect.getsource(verify.verify_toolpaths)
    assert "plan_passes" in inspect.getsource(generate_toolpaths)


def test_splitting_the_planner_out_did_not_change_the_gcode():
    """`generate_toolpaths` was one function; it is now a planner plus a
    post-processor, and the G-code it emits must be the same program."""
    result = generate_toolpaths(_plate(), TOOL)
    passes = plan_passes(_plate(), TOOL)
    assert result["gcode_url"].startswith("data:text/plain;base64,")
    assert result["estimated_minutes"] > 0
    # Read off the planner rather than assumed: the margin is widened to
    # `tool diameter + stepover` when the caller's is narrower, because a margin
    # thinner than the tool leaves no ring for the tool centre to rough in. With
    # a 3 mm cutter and 1.2 mm stepover that is 4.2 mm, not the 4 mm asked for.
    assert result["stock_size"]["x"] == pytest.approx(
        float(passes.stock_hi[0] - passes.stock_lo[0]), abs=1e-12)
    assert result["stock_size"]["x"] > .05 + 2 * .004
    assert result["stock_size"]["z"] == pytest.approx(.012, abs=1e-9)


# ------------------------------------------------------------- the reading --

def test_a_planned_path_does_not_gouge_the_part():
    """The planner only lets the tool centre sit where the field clears the
    radius, so this is the property it is supposed to have — and now the one
    that is checked rather than assumed."""
    report = verify.verify_toolpaths(_plate(), TOOL)
    assert report["gouge_mm3"] == 0.0
    assert report["checks"][0]["name"] == "Gouge"
    assert report["checks"][0]["status"] == "pass"


def test_a_stepped_part_is_machined_without_gouging_the_boss():
    report = verify.verify_toolpaths(_stepped(), TOOL)
    assert report["gouge_fraction"] <= verify.GOUGE_TOLERANCE, report["checks"]


def test_leftover_is_reported_and_is_not_zero():
    """Some is unavoidable — the scallops between raster passes are real, and a
    2.5-axis path cannot reach an undercut. A verifier claiming a perfect cut
    would be lying about the process, not praising the path."""
    report = verify.verify_toolpaths(_plate(), TOOL)
    assert report["leftover_mm3"] > 0


def test_a_coarser_stepover_leaves_more_material():
    """The reading has to *move* with the parameter it is measuring, or it is
    reporting a constant."""
    fine = verify.verify_toolpaths(_plate(), ToolParams(
        diameter_mm=3.0, stepdown_mm=2.0, stepover_mm=0.8))
    coarse = verify.verify_toolpaths(_plate(), ToolParams(
        diameter_mm=3.0, stepdown_mm=2.0, stepover_mm=2.6))
    assert coarse["leftover_mm3"] > fine["leftover_mm3"]


def test_stepdown_does_not_change_the_finished_shape():
    """Surprising until you hold the tool. A flat endmill cutting top-down
    removes everything above its lowest pass, so the intermediate levels change
    the cutting *load* and the cycle time, not the geometry that is left. The
    verifier measures geometry, so this reading is correctly insensitive to
    stepdown — and a reader who expected otherwise should find that written
    down rather than discovering it as a suspicious equality."""
    shallow = verify.verify_toolpaths(_stepped(), ToolParams(
        diameter_mm=3.0, stepdown_mm=1.0, stepover_mm=1.2))
    deep = verify.verify_toolpaths(_stepped(), ToolParams(
        diameter_mm=3.0, stepdown_mm=6.0, stepover_mm=1.2))
    assert deep["leftover_mm3"] == pytest.approx(shallow["leftover_mm3"])
    assert deep["levels"] < shallow["levels"], "the passes themselves do differ"


def test_a_bigger_cutter_cannot_reach_into_corners():
    """Stepover and diameter *do* move the reading: a fat cutter leaves fillets
    in every internal corner it cannot fit into."""
    slim = verify.verify_toolpaths(_stepped(), ToolParams(
        diameter_mm=1.5, stepdown_mm=2.0, stepover_mm=0.6))
    fat = verify.verify_toolpaths(_stepped(), ToolParams(
        diameter_mm=8.0, stepdown_mm=2.0, stepover_mm=0.6))
    assert fat["leftover_mm3"] > slim["leftover_mm3"]


# --------------------------------------------------- does it catch a gouge? --

def test_a_path_driven_through_the_part_is_caught():
    """The test that gives every other reading here its meaning. A hand-built
    pass at the bottom of the block, straight across the middle: it removes the
    part, and the verifier has to say so."""
    part = _plate()
    good = plan_passes(part, TOOL)
    lo, hi = good.stock_lo, good.stock_hi
    # One roughing pass at the floor, spanning the whole block through y = 0.
    bad = Passes(rough=[(float(lo[2]), [(float(lo[0]), float(hi[0]), 0.0)])],
                 finish=[], stock_lo=lo, stock_hi=hi,
                 safe_z=good.safe_z, tool_radius_m=good.tool_radius_m)

    original = verify.plan_passes
    try:
        verify.plan_passes = lambda *_a, **_k: bad
        report = verify.verify_toolpaths(part, TOOL)
    finally:
        verify.plan_passes = original

    assert report["gouge_mm3"] > 0
    gouge = next(c for c in report["checks"] if c["name"] == "Gouge")
    assert gouge["status"] == "fail"
    assert "scrapped part" in gouge["detail"]


def test_a_path_that_cuts_nothing_leaves_the_whole_block():
    part = _plate()
    good = plan_passes(part, TOOL)
    empty = Passes(rough=[], finish=[], stock_lo=good.stock_lo,
                   stock_hi=good.stock_hi, safe_z=good.safe_z,
                   tool_radius_m=good.tool_radius_m)
    original = verify.plan_passes
    try:
        verify.plan_passes = lambda *_a, **_k: empty
        report = verify.verify_toolpaths(part, TOOL)
    finally:
        verify.plan_passes = original

    assert report["gouge_mm3"] == 0.0                 # nothing cut, nothing gouged
    assert report["leftover_fraction"] > verify.LEFTOVER_WARN
    leftover = next(c for c in report["checks"] if c["name"] == "Leftover stock")
    assert leftover["status"] == "warn"


# ------------------------------------------------------- the floor shortcut --

def test_the_cut_volume_is_everything_above_the_lowest_pass():
    """A flat endmill cutting top-down means a point is removed exactly when it
    sits at or above the lowest level that cleared its (x, y) — one 2D array
    rather than a swept solid per segment, which is what keeps this fast."""
    lo = np.array([-.01, -.01, 0.0])
    hi = np.array([.01, .01, .01])
    passes = Passes(rough=[(0.006, [(-.01, .01, 0.0)]),
                           (0.002, [(-.01, .01, 0.0)])],
                    finish=[], stock_lo=lo, stock_hi=hi, safe_z=.02,
                    tool_radius_m=.0015)
    xs = np.linspace(lo[0], hi[0], 21)
    ys = np.linspace(lo[1], hi[1], 21)
    floor = verify._floor_map(passes, xs, ys)

    centre = floor[10, 10]
    assert centre == pytest.approx(0.002), "the lower of the two passes wins"
    assert np.isinf(floor[0, 0]), "a corner the tool never reached is uncut"


# ------------------------------------------------------------ the operation --

def _doc_with_plate():
    doc = docs.create_document(name="milled", owner_id="dev")
    ops.dispatch("add_primitive", doc.id,
                 {"shape": "box", "params": {"size": V(.05, .04, .012),
                                             "center": V(0, 0, 0)}})
    return doc.id


def test_the_operation_reports_both_numbers_and_the_sample_size():
    report = ops.dispatch("verify_toolpath", _doc_with_plate(),
                          {"diameter_mm": 3.0, "stepdown_mm": 2.0, "samples": 64})
    assert report["gouge_mm3"] == 0.0
    assert report["leftover_mm3"] > 0
    # Stated because it bounds every figure above: a feature thinner than a
    # cell is invisible to a sampled comparison.
    assert report["sample_mm"] > 0
    assert len(report["grid"]) == 3


def test_verifying_a_document_with_no_body_is_a_precondition():
    doc = docs.create_document(name="empty", owner_id="dev")
    envelope = ops.execute("verify_toolpath", doc.id, {})
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "PRECONDITION"


def test_a_zero_tool_is_refused_by_name():
    envelope = ops.execute("verify_toolpath", _doc_with_plate(), {"diameter_mm": 0})
    assert envelope["ok"] is False
    assert envelope["error"]["arg"] == "diameter_mm"


def test_it_is_a_query_and_changes_nothing():
    doc_id = _doc_with_plate()
    before = docs.get_document(doc_id).derived.mesh_hash
    ops.dispatch("verify_toolpath", doc_id, {"samples": 48})
    assert docs.get_document(doc_id).derived.mesh_hash == before
    assert ops.OPERATIONS["verify_toolpath"].kind == "query"
