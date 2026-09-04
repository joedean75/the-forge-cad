"""Drilling a named bolt pattern, as one feature.

A 30.5 x 30.5 flight-controller pattern used to be four hand-placed `add_hole`
calls, and swapping the board for a 20 x 20 one meant finding all four and
moving them — with nothing to say you had missed one until the board did not
bolt on. It is one `PatternFeature` now, so the swap is one call.

Three things have to hold, and the third is the one that makes the feature
worth having rather than merely convenient:

* **The holes land where the pattern says**, at the ISO 273 clearance for its
  screw — from `parts_catalog.clearance_hole`, which is the table `add_hole`
  and `clearance_hole` already drill to.
* **It is one feature.** Four features would re-create the problem inside a
  different operation.
* **The placements come from the table the *part* is built from.** A bracket
  drilled `nema17` and a NEMA 17 stepper read one bolt circle, so they cannot
  disagree. A bolt circle transcribed twice fits in CAD right up until it is
  made.
"""

import numpy as np
import pytest

from app.cad_kernel.core.feature_tree import walk_nodes
from app.services import cad_operations as ops
from app.services import decompiler, feature_ops
from app.services import document_service as docs
from app.services import parts_catalog as pc

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731


def _plate(size=(0.1, 0.1, 0.004)):
    doc = docs.create_document(name="plate", owner_id="dev", resolution=48)
    ops.dispatch("add_primitive", doc.id,
                 {"shape": "box", "params": {"size": V(*size), "center": V(0, 0, 0)}})
    return doc.id


def _bores(doc_id):
    """Every bore the document's patterns place, in millimetres."""
    doc = docs.get_document(doc_id)
    out = []
    for node in walk_nodes(doc.feature_tree):
        if node.get("type") != "PatternFeature":
            continue
        seed = (node["params"].get("children") or [None])[0]
        centre = seed["params"]["center"]
        for t in node["params"]["transforms"]:
            move = t["translate"]
            out.append(tuple(round((centre[a] + move[a]) * 1000, 3)
                             for a in "xyz"))
    return out


# --- the holes land where the pattern says -----------------------------------

def test_a_stack_pattern_is_drilled_at_its_own_spacing_and_clearance():
    doc_id = _plate()
    result = ops.dispatch("mounting_pattern", doc_id,
                          {"pattern": "fc-30.5", "center": V(0, 0, 0)})
    assert result["holes"] == 4
    assert result["screw"] == "M3"
    assert result["diameter_mm"] == pc.clearance_hole("M3")["diameter_mm"]
    assert {(x, y) for x, y, _ in _bores(doc_id)} == {
        (15.25, 15.25), (15.25, -15.25), (-15.25, 15.25), (-15.25, -15.25)}


def test_the_screw_can_be_overridden_and_the_fit_series_honoured():
    doc_id = _plate()
    loose = ops.dispatch("mounting_pattern", doc_id,
                         {"pattern": "fc-30.5", "screw": "M4", "fit": "coarse"})
    assert loose["screw"] == "M4"
    assert loose["diameter_mm"] == pc.clearance_hole("M4", "coarse")["diameter_mm"]
    assert loose["diameter_mm"] > pc.clearance_hole("M4")["diameter_mm"]


def test_the_pattern_actually_removes_material():
    doc_id = _plate()
    docs.evaluate_document(docs.get_document(doc_id))
    before = docs.get_document(doc_id).derived.volume
    ops.dispatch("mounting_pattern", doc_id, {"pattern": "fc-30.5",
                                              "center": V(0, 0, 0)})
    after = docs.get_document(doc_id).derived.volume
    # Four M3 clearance bores through 4 mm of plate.
    expected = 4 * np.pi * (0.0034 / 2) ** 2 * 0.004
    assert before - after == pytest.approx(expected, rel=0.15), (before, after)


def test_an_axis_puts_the_pattern_in_the_plane_perpendicular_to_it():
    """And deterministically: an asymmetric pattern landing a quarter turn from
    where it did last time is a re-drill that moves the holes."""
    doc_id = _plate(size=(0.004, 0.1, 0.1))
    ops.dispatch("mounting_pattern", doc_id,
                 {"pattern": "fc-30.5", "axis": V(1, 0, 0), "center": V(0, 0, 0)})
    assert {(round(y, 3), round(z, 3)) for _, y, z in _bores(doc_id)} == {
        (15.25, 15.25), (15.25, -15.25), (-15.25, 15.25), (-15.25, -15.25)}
    assert all(abs(x) < 1e-6 for x, _, _ in _bores(doc_id))


# --- it is one feature -------------------------------------------------------

def test_the_whole_pattern_is_one_feature():
    doc_id = _plate()
    result = ops.dispatch("mounting_pattern", doc_id, {"pattern": "fc-30.5"})
    patterns = [f for f in ops.dispatch("list_features", doc_id, {})["features"]
                if f["type"] == "PatternFeature"]
    assert [p["id"] for p in patterns] == [result["feature_id"]]


def test_re_drilling_moves_every_hole_and_adds_no_feature():
    """Swapping a 30.5 board for a 20 x 20 one. This is the whole point: four
    holes move together, and there is nothing left behind to find."""
    doc_id = _plate()
    first = ops.dispatch("mounting_pattern", doc_id, {"pattern": "fc-30.5",
                                                      "center": V(0, 0, 0)})
    before = len(ops.dispatch("list_features", doc_id, {})["features"])

    second = ops.dispatch("mounting_pattern", doc_id,
                          {"pattern": "fc-20", "feature": first["feature_id"]})
    assert second["feature_id"] == first["feature_id"]
    assert second["redrilled"] is True
    assert second["screw"] == "M2"
    assert len(ops.dispatch("list_features", doc_id, {})["features"]) == before
    assert {(x, y) for x, y, _ in _bores(doc_id)} == {
        (10.0, 10.0), (10.0, -10.0), (-10.0, 10.0), (-10.0, -10.0)}


def test_re_drilling_keeps_the_axis_the_pattern_was_drilled_on():
    """A re-drill changes which holes and how wide. Taking the operation's own
    +Z default here would swing a pattern drilled along +X into the XY plane
    the moment somebody swapped the board on it — a re-drill that silently
    moves the holes is worse than no re-drill."""
    doc_id = _plate(size=(0.004, 0.1, 0.1))
    first = ops.dispatch("mounting_pattern", doc_id,
                         {"pattern": "fc-30.5", "axis": V(1, 0, 0),
                          "center": V(0, 0, 0)})
    ops.dispatch("mounting_pattern", doc_id,
                 {"pattern": "fc-20", "feature": first["feature_id"]})
    assert {(round(y, 3), round(z, 3)) for _, y, z in _bores(doc_id)} == {
        (10.0, 10.0), (10.0, -10.0), (-10.0, 10.0), (-10.0, -10.0)}
    assert all(abs(x) < 1e-6 for x, _, _ in _bores(doc_id))
    # And the bores still point along +X, not down the plate.
    doc = docs.get_document(doc_id)
    seed = next(n["params"]["children"][0] for n in walk_nodes(doc.feature_tree)
                if n.get("type") == "PatternFeature")
    assert seed["params"]["axis"] == {"x": 1.0, "y": 0.0, "z": 0.0}


def test_re_drilling_keeps_the_depth_and_centre_unless_they_are_named():
    doc_id = _plate()
    first = ops.dispatch("mounting_pattern", doc_id,
                         {"pattern": "fc-30.5", "center": V(0.01, 0, 0),
                          "depth": 0.02})
    ops.dispatch("mounting_pattern", doc_id,
                 {"pattern": "fc-20", "feature": first["feature_id"]})
    doc = docs.get_document(doc_id)
    seed = next(n["params"]["children"][0] for n in walk_nodes(doc.feature_tree)
                if n.get("type") == "PatternFeature")
    assert seed["params"]["height"] == pytest.approx(0.02)
    assert seed["params"]["center"]["x"] == pytest.approx(0.01)


def test_re_drilling_something_that_is_not_a_pattern_is_refused():
    doc_id = _plate()
    body = ops.dispatch("list_features", doc_id, {})["features"][0]["id"]
    result = ops.execute("mounting_pattern", doc_id,
                         {"pattern": "fc-20", "feature": body})
    assert not result["ok"] and result["error"]["arg"] == "feature"
    missing = ops.execute("mounting_pattern", doc_id,
                          {"pattern": "fc-20", "feature": "nope"})
    assert missing["error"]["code"] == "NO_SUCH_FEATURE"


# --- one bolt circle, two readers -------------------------------------------

def test_a_bracket_drilled_for_a_stepper_matches_the_stepper():
    """The property the derivation exists for. Both read `bolt_patterns()`, so
    the bracket and the motor cannot disagree about where 31 mm is."""
    doc_id = _plate(size=(0.06, 0.06, 0.004))
    ops.dispatch("mounting_pattern", doc_id, {"pattern": "nema17",
                                              "center": V(0, 0, 0)})
    bracket = {(x, y) for x, y, _ in _bores(doc_id)}
    motor = {(round(n["params"]["center"]["x"] * 1000, 3),
              round(n["params"]["center"]["y"] * 1000, 3))
             for n in walk_nodes(pc.build_tree(pc.by_key("stepper-nema17")))
             if n.get("name") == "Mount hole"}
    assert bracket == motor


def test_a_servo_pattern_is_asymmetric_and_the_micro_one_has_two_holes():
    """A servo's ears are 49.5 by 10, which is exactly the arrangement two
    spans cannot describe and a table of placements can."""
    doc_id = _plate(size=(0.07, 0.04, 0.004))
    result = ops.dispatch("mounting_pattern", doc_id,
                          {"pattern": "servo-standard", "center": V(0, 0, 0)})
    assert result["holes"] == 4
    assert {(x, y) for x, y, _ in _bores(doc_id)} == {
        (24.75, 5.0), (24.75, -5.0), (-24.75, 5.0), (-24.75, -5.0)}

    micro = _plate(size=(0.05, 0.03, 0.004))
    assert ops.dispatch("mounting_pattern", micro,
                        {"pattern": "servo-micro"})["holes"] == 2


# --- the surface ------------------------------------------------------------

def test_the_published_enum_and_the_catalog_agree():
    """The enum is listed in `cad_operations` rather than derived, because it
    reaches the JSON Schema a constrained decoder is pointed at and a schema
    that depends on an import cycle is sometimes empty. This is what keeps the
    two lists honest."""
    published = ops.OPERATIONS["mounting_pattern"].params["pattern"]["enum"]
    assert set(published) == set(pc.bolt_patterns())


def test_drilling_a_document_with_no_body_is_refused():
    doc = docs.create_document(name="blank", owner_id="dev")
    result = ops.execute("mounting_pattern", doc.id, {"pattern": "fc-30.5"})
    assert not result["ok"]
    assert result["error"]["code"] == "PRECONDITION"


def test_an_unknown_pattern_names_what_there_is():
    doc_id = _plate()
    result = ops.execute("mounting_pattern", doc_id, {"pattern": "fc-40"})
    assert not result["ok"]
    # A refusal at the schema (the enum) or at the handler; either names it.
    assert "pattern" in str(result["error"])


def test_an_explicit_zero_depth_is_refused_rather_than_defaulted():
    """`x or default` swallows a caller's zero, which would drill through the
    body when they asked for nothing."""
    doc_id = _plate()
    result = ops.execute("mounting_pattern", doc_id,
                         {"pattern": "fc-30.5", "depth": 0})
    assert not result["ok"]


def test_the_decompiler_round_trips_a_drilled_pattern():
    """House rule: a feature the decompiler cannot emit drags every document
    containing it to 0% and takes its neighbours down with it."""
    doc_id = _plate()
    ops.dispatch("mounting_pattern", doc_id, {"pattern": "fc-30.5",
                                              "center": V(0, 0, 0)})
    report = decompiler.decompile_document(doc_id, resolution=48)
    # `ok` is only true once the replayed tree meshes to the same hash as the
    # original, which is the verification — the pattern is emitted as a
    # targeted `add_modifier` wrapping the seed cylinder in place, and a
    # counterbore under the pattern would break exactly that, which is why the
    # operation drills plain bores.
    assert report["ok"], report
    assert any(step["op"] == "add_modifier"
               and step["args"].get("modifier") == "pattern"
               for step in report["steps"]), report["steps"]
