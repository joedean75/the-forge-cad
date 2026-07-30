"""The design spec: what the part was supposed to be, and whether it is.

`check_geometry` says the body is a well-formed solid. `dfm` says a machine can
make it. Neither can tell a correct bracket from a correct paperweight — and a
reward built only on validity teaches a model to emit *some* valid solid rather
than the right one. These tests pin the behaviour that closes that gap.
"""

import pytest

from app.services import cad_operations as ops
from app.services import design_spec, document_service as docs
from app.cad_kernel.evaluator import brep


def _doc(name="Bracket"):
    return docs.create_document(name=name, resolution=48, owner_id=None).id


def _plate(doc_id, x=0.08, y=0.04, z=0.01):
    ops.dispatch("add_primitive", doc_id,
                 {"shape": "box", "params": {"size": {"x": x, "y": y, "z": z}}})


def _spec(doc_id, requirements, goal=None):
    args = {"requirements": requirements}
    if goal:
        args["goal"] = goal
    return ops.dispatch("set_spec", doc_id, args)


def test_a_met_requirement_scores_and_an_unmet_one_does_not():
    doc_id = _doc()
    _plate(doc_id)
    _spec(doc_id, [
        {"id": "len", "kind": "bounding_box", "axis": "x", "value": 80, "tolerance": 0.5},
        {"id": "wide", "kind": "bounding_box", "axis": "y", "value": 60, "tolerance": 0.5},
    ])

    report = ops.dispatch("verify_against_spec", doc_id, {})

    assert report["score"] == 0.5
    assert report["met"] is False
    by_id = {c["id"]: c for c in report["checks"]}
    assert by_id["len"]["status"] == "pass"
    assert by_id["wide"]["status"] == "fail"
    assert "60" in by_id["wide"]["detail"]        # says what was wanted, not just "no"


def test_the_score_rises_as_the_part_is_corrected():
    """The property an RL loop needs: partial progress is visible."""
    doc_id = _doc()
    _plate(doc_id, z=0.02)
    _spec(doc_id, [
        {"kind": "bounding_box", "axis": "x", "value": 80, "tolerance": 0.5},
        {"kind": "bounding_box", "axis": "z", "value": 10, "tolerance": 0.5},
        {"kind": "material", "value": "al-6061"},
    ])
    before = ops.dispatch("verify_against_spec", doc_id, {})["score"]

    ops.dispatch("set_material", doc_id, {"material_id": "al-6061"})
    after = ops.dispatch("verify_against_spec", doc_id, {})

    assert after["score"] > before
    assert after["met"] is False                  # the thickness is still wrong


def test_a_fully_met_spec_scores_one():
    doc_id = _doc()
    _plate(doc_id)
    ops.dispatch("set_material", doc_id, {"material_id": "al-6061"})
    _spec(doc_id, [
        {"kind": "bounding_box", "axis": "x", "value": 80, "tolerance": 0.5},
        {"kind": "mass", "max": 500},
        {"kind": "volume", "min": 10, "max": 60},
        {"kind": "material", "value": "al-6061"},
        {"kind": "watertight"},
        {"kind": "bodies", "value": 1},
    ])

    report = ops.dispatch("verify_against_spec", doc_id, {})

    assert report["score"] == 1.0 and report["met"] is True


def test_requirements_may_be_stated_as_a_range_or_a_bound():
    doc_id = _doc()
    _plate(doc_id)
    _spec(doc_id, [
        {"id": "range", "kind": "mass", "min": 50, "max": 150},
        {"id": "ceiling", "kind": "mass", "max": 10},
        {"id": "floor", "kind": "mass", "min": 10},
    ])

    by_id = {c["id"]: c for c in
             ops.dispatch("verify_against_spec", doc_id, {})["checks"]}

    assert by_id["range"]["status"] == "pass"     # ~86 g of aluminium
    assert by_id["ceiling"]["status"] == "fail"
    assert by_id["floor"]["status"] == "pass"


def test_a_requirement_with_no_bound_is_refused_when_it_is_checked():
    doc_id = _doc()
    _plate(doc_id)
    _spec(doc_id, [{"id": "vague", "kind": "mass"}])

    check = ops.dispatch("verify_against_spec", doc_id, {})["checks"][0]

    # Fails loudly rather than passing quietly — a spec that scores 1.0 because
    # half of it was unmeasurable is the worst possible reward signal.
    assert check["status"] == "fail"
    assert "min" in check["detail"]


def test_an_unknown_requirement_kind_is_refused_at_set_time():
    doc_id = _doc()

    result = ops.execute("set_spec", doc_id,
                         {"requirements": [{"kind": "looks_nice"}]})

    assert result["ok"] is False
    assert result["error"]["code"] == "BAD_ARG"
    assert "looks_nice" in result["error"]["message"]
    for kind in ("bounding_box", "mass", "hole"):
        assert kind in result["error"]["message"]   # says what is available


def test_verifying_without_a_spec_says_to_set_one():
    doc_id = _doc()
    _plate(doc_id)

    result = ops.execute("verify_against_spec", doc_id, {})

    assert result["error"]["code"] == "PRECONDITION"
    assert "set_spec" in result["error"]["message"]


def test_requirement_ids_are_filled_in_and_must_be_unique():
    doc_id = _doc()
    _spec(doc_id, [{"kind": "watertight"}, {"kind": "bodies", "value": 1}])

    ids = [r["id"] for r in ops.dispatch("get_spec", doc_id, {})["requirements"]]
    assert ids == ["r1", "r2"]

    result = ops.execute("set_spec", doc_id, {"requirements": [
        {"id": "same", "kind": "watertight"}, {"id": "same", "kind": "watertight"}]})
    assert result["ok"] is False


def test_an_empty_requirement_list_clears_the_spec():
    doc_id = _doc()
    _spec(doc_id, [{"kind": "watertight"}])

    ops.dispatch("set_spec", doc_id, {"requirements": []})

    assert docs.get_document(doc_id).spec is None


def test_the_spec_survives_a_reload():
    doc_id = _doc()
    _spec(doc_id, [{"id": "len", "kind": "bounding_box", "axis": "x", "value": 80}],
          goal="an 80 mm plate")
    docs.DOCUMENTS.pop(doc_id, None)

    spec = ops.dispatch("get_spec", doc_id, {})
    assert spec["goal"] == "an 80 mm plate"
    assert spec["requirements"][0]["value"] == 80


@pytest.mark.skipif(not brep.AVAILABLE, reason="hole counting needs the exact kernel")
def test_holes_are_counted_from_the_body_not_the_feature_tree():
    """A bore that a later boolean swallowed is not a hole; one that arrived
    inside an imported solid is, even though no operation created it."""
    doc_id = _doc()
    _plate(doc_id)
    _spec(doc_id, [{"id": "bore", "kind": "hole", "diameter": 10, "count": 1}])
    assert ops.dispatch("verify_against_spec", doc_id, {})["checks"][0]["status"] == "fail"

    ops.dispatch("add_primitive", doc_id,
                 {"shape": "cylinder", "operation": "subtract",
                  "params": {"radius": 0.005, "height": 0.05}})

    check = ops.dispatch("verify_against_spec", doc_id, {})["checks"][0]
    assert check["status"] == "pass"
    assert check["value"] == 1.0


@pytest.mark.skipif(not brep.AVAILABLE, reason="hole counting needs the exact kernel")
def test_a_hole_of_the_wrong_size_does_not_count():
    doc_id = _doc()
    _plate(doc_id)
    ops.dispatch("add_primitive", doc_id,
                 {"shape": "cylinder", "operation": "subtract",
                  "params": {"radius": 0.004, "height": 0.05}})
    _spec(doc_id, [{"kind": "hole", "diameter": 10, "diameter_tolerance": 0.2, "count": 1}])

    assert ops.dispatch("verify_against_spec", doc_id, {})["checks"][0]["status"] == "fail"


def test_a_feature_requirement_reads_the_tree():
    doc_id = _doc()
    _plate(doc_id)
    _spec(doc_id, [{"id": "boxy", "kind": "feature", "value": "BoxFeature"},
                   {"id": "sheet", "kind": "feature", "value": "SheetMetalFeature"}])

    by_id = {c["id"]: c for c in
             ops.dispatch("verify_against_spec", doc_id, {})["checks"]}

    assert by_id["boxy"]["status"] == "pass"
    assert by_id["sheet"]["status"] == "fail"


def test_the_spec_operations_are_in_the_published_catalog():
    """An agent can only use what the catalog advertises."""
    from app.services import op_schema

    names = {t["function"]["name"] for t in op_schema.tool_definitions()}
    assert {"set_spec", "get_spec", "verify_against_spec"} <= names


def test_validate_spec_rejects_a_non_object():
    with pytest.raises(design_spec.SpecError):
        design_spec.validate_spec([{"kind": "watertight"}])
